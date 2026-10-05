//! User-owned Base task progress and declarative chat components.
use super::*;

const MAX_ARTIFACT_BYTES: usize = 24 * 1024;

fn artifact_key(sid: &str, kind: &str, id: &str) -> Result<String> {
    if sid.is_empty() || sid.len() > 128 || !sid.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        || id.is_empty() || id.len() > 64 || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
        return Err(anyhow!("Invalid task artifact identifier"));
    }
    match kind {
        "checkpoint" if id == "task" => Ok(format!("base_checkpoint:{sid}")),
        "component" => Ok(format!("base_component:{sid}:{id}")),
        _ => Err(anyhow!("Invalid task artifact kind")),
    }
}

impl Store {
    // The routed user database owns both records. Stop/replacement and lease
    // checks keep an old tool call from writing after recovery.
    async fn base_run_fence(&self, tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
        sid: &str, rid: &str, pending_id: Option<&str>, generation: Option<&str>) -> Result<()> {
        let session_raw = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(format!("session:{sid}")).fetch_optional(&mut **tx).await?
            .ok_or_else(|| anyhow!("Session unavailable"))?;
        let session = self.unseal(&session_raw)?;
        if session["id"] != sid || session["status"] == "recycled" { return Err(anyhow!("Session unavailable")); }
        let run_raw = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(format!("run:{rid}")).fetch_optional(&mut **tx).await?
            .ok_or_else(|| anyhow!("Run unavailable"))?;
        let run = self.unseal(&run_raw)?;
        if run["id"] != rid || run["session_id"] != sid || run["status"] != "running"
            || run["agent_id"].as_str().unwrap_or("shared_default")
                != session["agent_id"].as_str().unwrap_or("shared_default") {
            return Err(anyhow!("Task run is stopped, replaced or belongs to another session"));
        }
        if generation.is_some_and(|expected| run["generation"].as_str() != Some(expected)) {
            return Err(anyhow!("Task run generation changed"));
        }
        if let Some(pending_id) = pending_id {
            let single = run["pending"]["id"] == pending_id
                && run["pending"]["state"] == "executing";
            let parallel = run["batch"].as_array().is_some_and(|batch| batch.iter().any(|item|
                item["pending"]["id"] == pending_id && item["state"] == "executing"));
            if !single && !parallel {
                return Err(anyhow!("Task call is no longer the pending tool dispatch"));
            }
        }
        if run["lease_epoch"].as_i64().unwrap_or(0) > 0 {
            let lease = sqlx::query("SELECT lease_epoch,lease_owner,lease_expires_at FROM run_index WHERE id=?")
                .bind(rid).fetch_optional(&mut **tx).await?
                .ok_or_else(|| anyhow!("Run lease unavailable"))?;
            let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs() as i64;
            if lease.get::<i64,_>("lease_epoch") != run["lease_epoch"].as_i64().unwrap_or(-1)
                || lease.get::<Option<String>,_>("lease_owner").as_deref() != run["lease_owner"].as_str()
                || lease.get::<i64,_>("lease_expires_at") < now {
                return Err(anyhow!("Run lease changed or expired"));
            }
        }
        Ok(())
    }

    pub async fn base_task_checkpoint(&self, sid: &str, rid: &str) -> Result<Value> {
        let key = artifact_key(sid, "checkpoint", "task")?;
        let mut tx = self.pool.begin().await?;
        self.base_run_fence(&mut tx, sid, rid, None, None).await?;
        let raw = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(key).fetch_optional(&mut *tx).await?;
        let output = match raw {
            Some(raw) => self.unseal(&raw)?,
            None => json!({"schema_version":1,"revision":0,"artifact":null}),
        };
        tx.commit().await?;
        Ok(output)
    }

    // Stable pending-call receipts make recovery retries harmless even after
    // the run completes. The receipt, artifact and event commit together.
    pub async fn base_write_artifact(&self, sid: &str, rid: &str, pending_id: &str,
        generation: &str,
        kind: &str, id: &str, expected_revision: u64, artifact: &Value) -> Result<Value> {
        let key = artifact_key(sid, kind, id)?;
        if generation.is_empty() || generation.len()>128
            || rid.is_empty() || rid.len() > 128 || pending_id.is_empty() || pending_id.len() > 128
            || !rid.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
            || !pending_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
            || serde_json::to_vec(artifact)?.len() > MAX_ARTIFACT_BYTES {
            return Err(anyhow!("Task artifact exceeds bounds"));
        }
        let receipt_key = format!("receipt:base:{sid}:{rid}:{pending_id}");
        let fingerprint = crate::digest(&json!([kind,id,expected_revision,artifact]));
        let _guard = self.context_gate.write().await;
        let mut tx = self.pool.begin().await?;
        sqlx::query("UPDATE docs SET key=key WHERE key=?")
            .bind(format!("session:{sid}")).execute(&mut *tx).await?;
        if let Some(raw) = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(&receipt_key).fetch_optional(&mut *tx).await? {
            let receipt = self.unseal(&raw)?;
            if receipt["fingerprint"] != fingerprint { return Err(anyhow!("Task call ID reused with different input")); }
            let output = receipt["output"].clone();
            tx.commit().await?;
            return Ok(output);
        }
        self.base_run_fence(&mut tx, sid, rid, Some(pending_id), Some(generation)).await?;
        let prior = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(&key).fetch_optional(&mut *tx).await?
            .map(|raw| self.unseal(&raw)).transpose()?;
        let revision = prior.as_ref().and_then(|value| value["revision"].as_u64()).unwrap_or(0);
        if revision != expected_revision { return Err(anyhow!("Task artifact revision changed; reload before editing")); }
        let next = revision.checked_add(1).ok_or_else(|| anyhow!("Task revision exhausted"))?;
        let document = json!({"schema_version":1,"revision":next,"kind":kind,
            "id":id,"session_id":sid,"updated_by_run":rid,"artifact":artifact});
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(&key).bind(self.encode_user_record(&document)?).execute(&mut *tx).await?;
        let output = json!({"revision":next,"kind":kind,"id":id,"artifact":artifact});
        let receipt = json!({"fingerprint":fingerprint,"output":output});
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)")
            .bind(&receipt_key).bind(self.encode_user_record(&receipt)?).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?,'base_task_artifact',?)")
            .bind(sid).bind(self.encode_user_record(&json!({"kind":kind,"id":id,"revision":next}))?)
            .execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(output)
    }

    // Safe API projection: only previously validated declarative specs.
    pub async fn base_chat_components(&self, sid: &str) -> Result<Vec<Value>> {
        artifact_key(sid, "checkpoint", "task")?;
        let mut tx = self.pool.begin().await?;
        let session_raw = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(format!("session:{sid}")).fetch_optional(&mut *tx).await?
            .ok_or_else(|| anyhow!("Session unavailable"))?;
        if self.unseal(&session_raw)?["id"] != sid { return Err(anyhow!("Session unavailable")); }
        let rows = sqlx::query("SELECT value FROM docs WHERE key LIKE ? ORDER BY key LIMIT 65")
            .bind(format!("base_component:{sid}:%")).fetch_all(&mut *tx).await?;
        if rows.len() > 64 { return Err(anyhow!("Chat component count exceeds limit")); }
        let mut components = Vec::with_capacity(rows.len());
        for row in rows {
            let document = self.unseal(&row.get::<Vec<u8>,_>("value"))?;
            if document["schema_version"] != 1 || document["session_id"] != sid
                || document["kind"] != "component" { return Err(anyhow!("Invalid stored chat component")); }
            components.push(json!({"revision":document["revision"],"component":document["artifact"]}));
        }
        tx.commit().await?;
        Ok(components)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn checkpoint_cas_replay_and_run_fences() {
        let (_root,app)=crate::runtime_tests::fixture_uninitialized().await;
        let store=&app.store;
        for sid in ["owned","foreign"] {
            store.put(&format!("session:{sid}"),&json!({"id":sid,"agent_id":"shared_default",
                "status":"active","created_at":1})).await.unwrap();
        }
        let running=|sid:&str,state:&str|json!({"id":"r","session_id":sid,
            "agent_id":"shared_default","status":state,"generation":"g",
            "lease_epoch":0,"pending":{"id":"call-1","state":"executing"}});
        store.put("run:r",&running("owned","running")).await.unwrap();
        let artifact=json!({"summary":"Progress reported by agent","provenance":"agent_claim"});
        let first=store.base_write_artifact("owned","r","call-1","g","checkpoint","task",0,&artifact).await.unwrap();
        assert_eq!(first["revision"],1);
        assert_eq!(store.base_task_checkpoint("owned","r").await.unwrap()["revision"],1);
        assert_eq!(store.base_write_artifact("owned","r","call-1","g","checkpoint","task",0,&artifact).await.unwrap(),first);
        assert!(store.base_write_artifact("owned","r","call-1","g","checkpoint","task",0,&json!({"different":true})).await.is_err());
        // The next dispatched call has a fresh ID but must supply revision 1.
        let mut next_run=running("owned","running");
        next_run["pending"]["id"]=json!("call-2");
        store.put("run:r",&next_run).await.unwrap();
        assert!(store.base_write_artifact("owned","r","call-2","g","checkpoint","task",0,&artifact).await.is_err());
        assert!(store.base_write_artifact("owned","r","call-2","old-generation","checkpoint","task",1,&artifact).await.is_err());
        assert!(store.base_task_checkpoint("foreign","r").await.is_err());
        store.put("run:r",&running("owned","completed")).await.unwrap();
        // A committed write replays after completion; a new write is fenced.
        assert_eq!(store.base_write_artifact("owned","r","call-1","g","checkpoint","task",0,&artifact).await.unwrap(),first);
        assert!(store.base_write_artifact("owned","r","call-2","g","checkpoint","task",1,&artifact).await.is_err());
    }

    #[tokio::test]
    async fn component_projection_is_user_and_session_scoped() {
        let (_root,app)=crate::runtime_tests::fixture_uninitialized().await;
        let owner=app.store.create_user_account("base-owner","long-fixture-hash-placeholder").await.unwrap();
        let other=app.store.create_user_account("base-other","long-fixture-hash-placeholder").await.unwrap();
        let first=app.store.for_user(owner["user_id"].as_str().unwrap()).await.unwrap();
        let second=app.store.for_user(other["user_id"].as_str().unwrap()).await.unwrap();
        for store in [&first,&second] {
            store.put("session:s",&json!({"id":"s","status":"active","agent_id":"shared_default","created_at":1})).await.unwrap();
            store.put("run:r",&json!({"id":"r","session_id":"s","agent_id":"shared_default",
                "status":"running","generation":"g","lease_epoch":0,"pending":{"id":"call-1","state":"executing"}})).await.unwrap();
        }
        let display=json!({"id":"plan-overview","type":"status","title":"Plan",
            "data":{"sections":[{"label":"Goal","value":"Private"}]},"authority":"display_only"});
        first.base_write_artifact("s","r","call-1","g","component","plan-overview",0,&display).await.unwrap();
        let projected=first.base_chat_components("s").await.unwrap();
        assert_eq!(projected.len(),1);
        assert_eq!(projected[0]["component"]["data"]["sections"][0]["value"],"Private");
        assert!(second.base_chat_components("s").await.unwrap().is_empty());
        assert!(first.base_chat_components("missing").await.is_err());
    }
}
