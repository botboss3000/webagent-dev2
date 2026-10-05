//! Retire provider reasoning after its source prefix has been compacted.
//! The compaction transaction publishes a hash-only job; this worker removes
//! private copies in one later user-store transaction without blocking model calls.
use super::*;
use std::collections::HashMap;

pub(super) fn hashes(source: &[Value]) -> Result<Vec<String>> {
    let mut found=Vec::new();
    for item in source {
        if crate::context_compaction::has_reasoning(item) {
            found.push(recovery_item_digest(item)?);
        }
    }
    let mut hashes=found;
    hashes.sort();
    Ok(hashes)
}

fn redacted(item: &Value) -> Value {
    crate::context_compaction::without_reasoning(item)
        .unwrap_or_else(||json!({"type":"reasoning_redacted"}))
}

fn reduction(before: &Value, after: &Value) -> Result<u64> {
    Ok(serde_json::to_vec(before)?.len()
        .saturating_sub(serde_json::to_vec(after)?.len()) as u64)
}

impl Store {
    /// Aggregate only content-free counters for authenticated diagnostics.
    pub async fn reasoning_cleanup_total(&self, sid: &str) -> Result<u64> {
        let rows=sqlx::query("SELECT payload FROM events WHERE session_id=? AND kind='reasoning_cleanup_completed'")
            .bind(sid).fetch_all(&self.pool).await?;
        let mut total=0u64;
        for row in rows {
            let payload=self.unseal(&row.get::<Vec<u8>,_>("payload"))?;
            let bytes=payload["logical_bytes_removed"].as_u64()
                .ok_or_else(||anyhow!("Invalid reasoning cleanup metric"))?;
            total=total.checked_add(bytes).ok_or_else(||anyhow!("Reasoning cleanup metric overflow"))?;
        }
        Ok(total)
    }

    /// Process one durable job. A failure rolls back all edits and leaves the
    /// marker for restart recovery; no model-visible checkpoint is touched.
    pub async fn cleanup_one_reasoning(&self) -> Result<bool> {
        let Some(row)=sqlx::query("SELECT segment_id,session_id,payload FROM reasoning_cleanup ORDER BY rowid LIMIT 1")
            .fetch_optional(&self.pool).await? else { return Ok(false) };
        let segment_id:String=row.get("segment_id");
        let sid:String=row.get("session_id");
        let job=self.unseal(&row.get::<Vec<u8>,_>("payload"))?;
        if job["segment_id"]!=segment_id || job["session_id"]!=sid {
            return Err(anyhow!("Reasoning cleanup identity conflict"));
        }
        let listed=job["hashes"].as_array().ok_or_else(||anyhow!("Invalid reasoning cleanup hashes"))?;
        if listed.len()>4096 { return Err(anyhow!("Reasoning cleanup hash limit exceeded")); }
        let mut hashes=HashMap::<String,usize>::new();
        for value in listed {
            let hash=value.as_str().ok_or_else(||anyhow!("Invalid reasoning cleanup hash"))?;
            if hash.len()!=64 || !hash.bytes().all(|b|b.is_ascii_hexdigit()) {
                return Err(anyhow!("Invalid reasoning cleanup hash"));
            }
            *hashes.entry(hash.to_owned()).or_default()+=1;
        }
        let mut tx=self.pool.begin().await?;
        let segment_raw:Vec<u8>=sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
            .bind(format!("compaction_segment:{sid}:{segment_id}"))
            .fetch_one(&mut *tx).await?;
        let segment=self.unseal(&segment_raw)?;
        if segment["schema_version"]!=2 || segment["session_id"]!=sid
            || segment["segment_id"]!=segment_id
            || segment["original_source_digest"]!=job["source_digest"]
            || segment["source_digest"]!=crate::digest(&segment["source"]) {
            return Err(anyhow!("Reasoning cleanup segment provenance conflict"));
        }
        // A single loop worker owns jobs. Recheck the marker under the write
        // transaction so a stale worker cannot retire another job's content.
        let current:Option<Vec<u8>>=sqlx::query_scalar("SELECT payload FROM reasoning_cleanup WHERE segment_id=? AND session_id=?")
            .bind(&segment_id).bind(&sid).fetch_optional(&mut *tx).await?;
        if current.as_deref()!=Some(row.get::<Vec<u8>,_>("payload").as_slice()) {
            return Err(anyhow!("Reasoning cleanup job changed"));
        }
        let mut interaction_items=0u64;
        let mut recovery_items=0u64;
        let mut interaction_bytes=0u64;
        let mut recovery_bytes=0u64;
        let mut cursor=0i64;
        loop {
            let interactions=sqlx::query("SELECT seq,item FROM interactions WHERE session_id=? AND seq>? ORDER BY seq LIMIT 128")
                .bind(&sid).bind(cursor).fetch_all(&mut *tx).await?;
            if interactions.is_empty() { break; }
            for row in interactions {
                cursor=row.get::<i64,_>("seq");
                let old=self.unseal(&row.get::<Vec<u8>,_>("item"))?;
                if !crate::context_compaction::has_reasoning(&old) { continue; }
                let digest=recovery_item_digest(&old)?;
                let Some(remaining)=hashes.get_mut(&digest) else { continue; };
                if *remaining==0 { continue; }
                let clean=redacted(&old);
                interaction_bytes+=reduction(&old,&clean)?;
                sqlx::query("UPDATE interactions SET item=? WHERE seq=? AND session_id=?")
                    .bind(self.encode_user_record(&clean)?).bind(cursor)
                    .bind(&sid).execute(&mut *tx).await?;
                *remaining-=1;
                interaction_items+=1;
            }
        }
        let dropped=job["dropped_items"].as_u64().ok_or_else(||anyhow!("Invalid cleanup item count"))? as usize;
        let mut run_cursor=String::new();
        loop {
            let runs=sqlx::query("SELECT d.key,d.value FROM docs d JOIN run_index r ON d.key='run:'||r.id WHERE r.session_id=? AND r.status IN ('completed','cancelled','failed') AND d.key>? ORDER BY d.key LIMIT 32")
                .bind(&sid).bind(&run_cursor).fetch_all(&mut *tx).await?;
            if runs.is_empty() { break; }
            for row in runs {
                let key:String=row.get("key");
                run_cursor=key.clone();
                let mut run=self.decode_document(&mut tx,&row.get::<Vec<u8>,_>("value")).await?;
                let mut changed=false;
                if let Some(input)=run["input"].as_array_mut() {
                    for item in input.iter_mut().take(dropped) {
                        if !crate::context_compaction::has_reasoning(item)
                            || !hashes.contains_key(&recovery_item_digest(item)?) { continue; }
                        let clean=redacted(item);
                        recovery_bytes+=reduction(item,&clean)?;
                        *item=clean;
                        recovery_items+=1;
                        changed=true;
                    }
                }
                if changed {
                    run["run_revision"]=json!(run["run_revision"].as_u64().unwrap_or(0)+1);
                    self.write_document(&mut tx,&key,&run).await?;
                    self.index_run(&mut tx,&run).await?;
                }
            }
        }
        let segment_saved=job["segment_bytes_avoided"].as_u64().unwrap_or(0);
        let event_saved=job["event_bytes_avoided"].as_u64().unwrap_or(0);
        let report=json!({"segment_id":segment_id,"interaction_items":interaction_items,
            "recovery_items":recovery_items,"interaction_bytes_removed":interaction_bytes,
            "recovery_bytes_removed":recovery_bytes,"segment_bytes_avoided":segment_saved,
            "event_bytes_avoided":event_saved,
            "logical_bytes_removed":interaction_bytes+recovery_bytes+segment_saved+event_saved});
        sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?,?,?)")
            .bind(&sid).bind("reasoning_cleanup_completed")
            .bind(self.encode_user_record(&report)?).execute(&mut *tx).await?;
        sqlx::query("DELETE FROM reasoning_cleanup WHERE segment_id=? AND session_id=?")
            .bind(&segment_id).bind(&sid).execute(&mut *tx).await?;
        tx.commit().await?;
        self.cache.lock().unwrap().invalidate(&[format!("session:{sid}")]);
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn cleanup_failure_rolls_back_and_keeps_the_job() {
        let root=tempfile::tempdir().unwrap();
        let store=Store::open(root.path()).await.unwrap();
        store.put("session:s",&json!({"id":"s","checkpoint_input":[],"checkpoint_through":0}))
            .await.unwrap();
        let reason=json!({"type":"reasoning","id":"old","encrypted_content":"secret"});
        store.put("run:old",&json!({"id":"old","session_id":"s","status":"completed",
            "input":[reason],"run_revision":0,"updated_at":1})).await.unwrap();
        let mut tx=store.pool.begin().await.unwrap();
        store.insert_interaction(&mut tx,"s","old","model:1:0",&reason).await.unwrap();
        tx.commit().await.unwrap();
        let source=vec![json!({"role":"user","content":"old"}),reason.clone()];
        let mut run=json!({"id":"active","session_id":"s","status":"running",
            "input":[{"role":"user","content":"summary"}],"compactions":1,"updated_at":2});
        store.save_run(&mut run,"context_compacted",&json!({"run_id":"active",
            "segment_id":"active-1","source_digest":crate::digest(&source),
            "source":source,"dropped_items":2})).await.unwrap();
        let sealed:Vec<u8>=sqlx::query_scalar("SELECT item FROM recovery_input WHERE run_id='old' AND ordinal=0")
            .fetch_one(&store.pool).await.unwrap();
        sqlx::query("UPDATE recovery_input SET item=x'00' WHERE run_id='old' AND ordinal=0")
            .execute(&store.pool).await.unwrap();
        assert!(store.cleanup_one_reasoning().await.is_err());
        let raw:Vec<u8>=sqlx::query_scalar("SELECT item FROM interactions WHERE session_id='s'")
            .fetch_one(&store.pool).await.unwrap();
        assert_eq!(store.unseal(&raw).unwrap(),reason);
        let queued:i64=sqlx::query_scalar("SELECT COUNT(*) FROM reasoning_cleanup")
            .fetch_one(&store.pool).await.unwrap();
        assert_eq!(queued,1);
        sqlx::query("UPDATE recovery_input SET item=? WHERE run_id='old' AND ordinal=0")
            .bind(sealed).execute(&store.pool).await.unwrap();
        assert!(store.cleanup_one_reasoning().await.unwrap());
        assert_eq!(store.get("run:old").await.unwrap().unwrap()["input"][0]["type"],"reasoning_redacted");
    }

    #[tokio::test]
    async fn compacted_reasoning_is_retired_after_reopen_without_changing_tail() {
        let root=tempfile::tempdir().unwrap();
        let store=Store::open(root.path()).await.unwrap();
        store.put("session:s",&json!({"id":"s","checkpoint_input":[],"checkpoint_through":0}))
            .await.unwrap();
        let reason=json!({"type":"reasoning","id":"rs-old","encrypted_content":"opaque-secret"});
        let deepseek=json!({"type":"message","role":"assistant","content":[{"type":"output_text","text":"answer"}],
            "deepseek_reasoning":"private-thought"});
        let source=vec![json!({"role":"user","content":"old"}),reason.clone(),deepseek.clone()];
        let tail=json!({"type":"reasoning","id":"rs-new","encrypted_content":"keep-me"});
        for rid in ["old-1","old-2"] {
            store.put(&format!("run:{rid}"),&json!({"id":rid,"session_id":"s","status":"completed",
                "input":source,"run_revision":0,"updated_at":1})).await.unwrap();
        }
        let mut with_duplicate=source.clone();
        with_duplicate.push(reason.clone());
        store.put("run:old-dup",&json!({"id":"old-dup","session_id":"s","status":"completed",
            "input":with_duplicate,"run_revision":0,"updated_at":1})).await.unwrap();
        let mut tx=store.pool.begin().await.unwrap();
        store.insert_interaction(&mut tx,"s","old-2","model:1:0",&reason).await.unwrap();
        store.insert_interaction(&mut tx,"s","old-2","model:1:1",&deepseek).await.unwrap();
        store.insert_interaction(&mut tx,"s","old-dup","model:1:0",&reason).await.unwrap();
        tx.commit().await.unwrap();
        let mut run=json!({"id":"active","session_id":"s","status":"running","input":[
            {"role":"user","content":"summary"},tail],"compactions":1,"updated_at":2});
        store.save_run(&mut run,"context_compacted",&json!({"run_id":"active",
            "segment_id":"active-1","source_digest":crate::digest(&source),
            "source":source,"dropped_items":3})).await.unwrap();
        let segment=store.get("compaction_segment:s:active-1").await.unwrap().unwrap();
        assert_eq!(segment["schema_version"],2);
        assert_eq!(segment["source"].as_array().unwrap().len(),2);
        assert!(!segment.to_string().contains("opaque-secret"));
        assert!(!segment.to_string().contains("private-thought"));
        let events=store.events("s",0).await.unwrap();
        assert!(events.iter().any(|event| event["kind"]=="context_compacted"));
        assert!(!json!(events).to_string().contains("opaque-secret"));
        drop(store);
        let reopened=Store::open(root.path()).await.unwrap();
        assert!(reopened.cleanup_one_reasoning().await.unwrap());
        assert!(!reopened.cleanup_one_reasoning().await.unwrap());
        for rid in ["old-1","old-2"] {
            let saved=reopened.get(&format!("run:{rid}")).await.unwrap().unwrap();
            assert_eq!(saved["input"][1]["type"],"reasoning_redacted");
            assert!(saved["input"][2].get("deepseek_reasoning").is_none());
        }
        let duplicate=reopened.get("run:old-dup").await.unwrap().unwrap();
        assert_eq!(duplicate["input"][1]["type"],"reasoning_redacted");
        assert_eq!(duplicate["input"][3]["encrypted_content"],"opaque-secret");
        let active=reopened.get("run:active").await.unwrap().unwrap();
        assert_eq!(active["input"][1]["encrypted_content"],"keep-me");
        let items=sqlx::query("SELECT item FROM interactions WHERE session_id='s' ORDER BY seq")
            .fetch_all(&reopened.pool).await.unwrap();
        assert_eq!(reopened.unseal(&items[0].get::<Vec<u8>,_>("item")).unwrap()["type"],"reasoning_redacted");
        assert!(reopened.unseal(&items[1].get::<Vec<u8>,_>("item")).unwrap().get("deepseek_reasoning").is_none());
        assert_eq!(reopened.unseal(&items[2].get::<Vec<u8>,_>("item")).unwrap()["encrypted_content"],"opaque-secret");
        let events=reopened.events("s",0).await.unwrap();
        let done=events.iter().find(|event|event["kind"]=="reasoning_cleanup_completed").unwrap();
        assert_eq!(done["payload"]["interaction_items"],2);
        assert_eq!(done["payload"]["recovery_items"],6);
        assert!(done["payload"]["logical_bytes_removed"].as_u64().unwrap()>0);
        assert_eq!(reopened.reasoning_cleanup_total("s").await.unwrap(),
            done["payload"]["logical_bytes_removed"].as_u64().unwrap());
        assert!(!json!(events).to_string().contains("opaque-secret"));
        assert!(!json!(events).to_string().contains("private-thought"));
    }
}
