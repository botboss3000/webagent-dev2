//! Session metadata stays in the user-owned AEAD document, not plaintext columns.
//! V2 reference: sessions.title/pinned/status/created_at and durable read markers.
//! V3 uses a sequence cursor instead of a wall-clock read marker. Activity is
//! distinct from metadata edits; public chat publication owns timestamp updates.
use super::*;
use serde::{Deserialize, Serialize};

const MIGRATION_KEY: &str = "storage:session_metadata_v1";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SessionStatus {
    Active,
    Recycled,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionMetadata {
    pub title: String,
    pub pinned: bool,
    pub status: SessionStatus,
    pub created_at: i64,
    pub last_active_at: i64,
    pub read_through_seq: u64,
    pub recycled_at: Option<i64>,
    pub metadata_revision: u64,
    pub metadata_schema_version: u32,
}

/// Fill missing fields only. Preserve identity, checkpoints and unknown fields.
/// Existing invalid values fail closed rather than being silently replaced.
pub(super) fn with_session_metadata(value: &Value, activity: Option<i64>) -> Result<Value> {
    let mut value = value.clone();
    let object = value.as_object_mut().ok_or_else(|| anyhow!("Invalid session document"))?;
    // Zero denotes unknown legacy creation time; never fabricate old activity.
    object.entry("created_at").or_insert_with(|| json!(0));
    let created = object.get("created_at").and_then(Value::as_i64)
        .ok_or_else(|| anyhow!("Invalid session creation timestamp"))?;
    let defaults = json!({
        "title":"New chat", "pinned":false, "status":"active",
        "last_active_at":activity.unwrap_or(created).max(created),
        "read_through_seq":0, "recycled_at":null, "metadata_revision":0,
        "metadata_schema_version":1
    });
    for (key, default) in defaults.as_object().unwrap() {
        object.entry(key.clone()).or_insert_with(|| default.clone());
    }
    let metadata: SessionMetadata = serde_json::from_value(value.clone())?;
    if metadata.metadata_schema_version != 1 || metadata.created_at < 0
        || metadata.last_active_at < metadata.created_at
        || metadata.recycled_at.is_some_and(|timestamp| timestamp < 0)
    {
        return Err(anyhow!("Unsupported or invalid session metadata"));
    }
    Ok(value)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionPatch {
    pub client_request_id: String,
    pub expected_revision: u64,
    pub title: Option<String>,
    pub pinned: Option<bool>,
}

impl SessionPatch {
    pub fn validate(&self) -> std::result::Result<(), &'static str> {
        if self.client_request_id.is_empty() || self.client_request_id.len() > 128
            || self.client_request_id.trim() != self.client_request_id
            || self.client_request_id.chars().any(char::is_control) {
            return Err("Invalid request identifier");
        }
        if self.title.is_none() && self.pinned.is_none() { return Err("Empty session patch"); }
        if let Some(title) = &self.title {
            let title = title.trim();
            if title.is_empty() || title.len() > 200 || title.chars().any(char::is_control) {
                return Err("Invalid session title");
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionLifecycle {
    pub client_request_id: String,
    pub expected_revision: u64,
}

#[derive(Debug)]
pub struct RecycledSession;
impl std::fmt::Display for RecycledSession {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("Session is recycled; restore it first") }
}
impl std::error::Error for RecycledSession {}

pub enum SessionPatchOutcome {
    Saved(Value),
    NotFound,
    Conflict(&'static str),
    Invalid(&'static str),
}

/// Public projection: never return checkpoint input or other runtime documents.
pub fn public_session(value: &Value) -> Result<Value> {
    let metadata: SessionMetadata = serde_json::from_value(value.clone())?;
    let mut summary = serde_json::to_value(metadata)?;
    summary["id"] = value["id"].clone();
    summary["agent_id"] = json!(value["agent_id"].as_str().unwrap_or("shared_default"));
    Ok(summary)
}

impl Store {
    /// Called only after a newly inserted public chat row, in its transaction.
    pub(super) async fn touch_session_activity(&self, tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>, sid: &str) -> Result<()> {
        let key = format!("session:{sid}");
        let Some(raw) = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(&key).fetch_optional(&mut **tx).await? else { return Ok(()); };
        let mut session = self.unseal(&raw)?;
        let timestamp: i64 = sqlx::query_scalar("SELECT COALESCE(MAX(created_at),0) FROM chat WHERE session_id=?")
            .bind(sid).fetch_one(&mut **tx).await?;
        session["last_active_at"] = json!(timestamp.max(session["last_active_at"].as_i64().unwrap_or(0)));
        sqlx::query("UPDATE docs SET value=? WHERE key=?").bind(self.seal(&session)?).bind(&key).execute(&mut **tx).await?;
        Ok(())
    }

    pub async fn session_read_summary(&self, sid: &str) -> Result<Value> {
        let mut tx = self.pool.begin().await?;
        let raw: Vec<u8> = sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
            .bind(format!("session:{sid}")).fetch_one(&mut *tx).await?;
        let session = self.unseal(&raw)?;
        let result = self.read_summary_in(&mut tx, sid, &session).await?;
        tx.commit().await?;
        Ok(result)
    }

    async fn read_summary_in(&self, tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>, sid: &str, session: &Value) -> Result<Value> {
        let mut summary = public_session(session)?;
        let cursor = session["read_through_seq"].as_u64().ok_or_else(|| anyhow!("Invalid read cursor"))?;
        let unread: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM chat WHERE session_id=? AND role='assistant' AND seq>?")
            .bind(sid).bind(i64::try_from(cursor)?).fetch_one(&mut **tx).await?;
        summary["unread_count"] = json!(unread);
        summary["is_unread"] = json!(unread > 0);
        Ok(summary)
    }

    /// Explicit rendered-message acknowledgment. Natural max semantics make
    /// retries and older tabs harmless without mutating metadata revisions.
    pub async fn acknowledge_session_read(&self, sid: &str, through: u64) -> Result<SessionPatchOutcome> {
        let Ok(seq) = i64::try_from(through) else { return Ok(SessionPatchOutcome::Invalid("Invalid read cursor")); };
        let key = format!("session:{sid}");
        let _guard = self.context_gate.write().await;
        let mut tx = self.pool.begin().await?;
        sqlx::query("UPDATE docs SET key=key WHERE key=?").bind(&key).execute(&mut *tx).await?;
        let Some(raw) = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(&key).fetch_optional(&mut *tx).await? else { return Ok(SessionPatchOutcome::NotFound); };
        let mut session = self.unseal(&raw)?;
        if session["status"] != "active" { return Ok(SessionPatchOutcome::Conflict("Session is recycled")); }
        if seq != 0 {
            let exists: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM chat WHERE session_id=? AND seq=?")
                .bind(sid).bind(seq).fetch_one(&mut *tx).await?;
            if exists != 1 { return Ok(SessionPatchOutcome::Invalid("Read cursor is not a message in this session")); }
        }
        let previous = session["read_through_seq"].as_u64().ok_or_else(|| anyhow!("Invalid stored read cursor"))?;
        if through > previous {
            session["read_through_seq"] = json!(through);
            sqlx::query("UPDATE docs SET value=? WHERE key=?").bind(self.seal(&session)?).bind(&key).execute(&mut *tx).await?;
        }
        let result = json!({"session":self.read_summary_in(&mut tx,sid,&session).await?});
        if through > previous {
            sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?,'session_read',?)")
                .bind(sid).bind(self.seal(&result)?).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        self.cache.lock().unwrap().invalidate(&[key]);
        Ok(SessionPatchOutcome::Saved(result))
    }

    pub async fn ordered_sessions(&self) -> Result<Vec<Value>> {
        self.ordered_sessions_status(false).await
    }

    pub async fn ordered_sessions_status(&self, recycled: bool) -> Result<Vec<Value>> {
        let mut tx = self.pool.begin().await?;
        let rows = sqlx::query("SELECT value FROM docs WHERE key LIKE 'session:%' ORDER BY key").fetch_all(&mut *tx).await?;
        let mut summaries = Vec::with_capacity(rows.len());
        for row in rows {
            let session = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
            if session["status"] == if recycled { "recycled" } else { "active" } {
                let sid = session["id"].as_str().ok_or_else(|| anyhow!("Invalid session ID"))?;
                summaries.push(self.read_summary_in(&mut tx, sid, &session).await?);
            }
        }
        tx.commit().await?;
        summaries.sort_by(|a, b| b["pinned"].as_bool().cmp(&a["pinned"].as_bool())
            .then_with(|| b["last_active_at"].as_i64().cmp(&a["last_active_at"].as_i64()))
            .then_with(|| a["id"].as_str().cmp(&b["id"].as_str())));
        Ok(summaries)
    }

    /// Reserve SQLite's writer before reading. Session, event and encrypted retry
    /// receipt commit together; the request key is scoped to this user and session.
    pub async fn patch_session(&self, sid: &str, patch: &SessionPatch) -> Result<SessionPatchOutcome> {
        if let Err(message) = patch.validate() { return Ok(SessionPatchOutcome::Invalid(message)); }
        let session_key = format!("session:{sid}");
        let receipt_key = format!("receipt:session:patch:{sid}:{}", patch.client_request_id);
        let fingerprint = crate::digest(patch);
        let _guard = self.context_gate.write().await;
        let mut tx = self.pool.begin().await?;
        sqlx::query("UPDATE docs SET key=key WHERE key=?")
            .bind(&session_key).execute(&mut *tx).await?;
        if let Some(raw) = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(&receipt_key).fetch_optional(&mut *tx).await? {
            let receipt = self.unseal(&raw)?;
            if receipt["digest"] != fingerprint {
                return Ok(SessionPatchOutcome::Conflict("Request identifier reused with different input"));
            }
            tx.commit().await?;
            return Ok(SessionPatchOutcome::Saved(receipt["result"].clone()));
        }
        let Some(raw) = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(&session_key).fetch_optional(&mut *tx).await? else {
            return Ok(SessionPatchOutcome::NotFound);
        };
        let mut session = self.unseal(&raw)?;
        let metadata: SessionMetadata = serde_json::from_value(session.clone())?;
        if metadata.status != SessionStatus::Active {
            return Ok(SessionPatchOutcome::Conflict("Session is recycled"));
        }
        if metadata.metadata_revision != patch.expected_revision {
            return Ok(SessionPatchOutcome::Conflict("Session metadata revision changed"));
        }
        let mut changed = false;
        if let Some(title) = &patch.title {
            let title = title.trim();
            changed |= metadata.title != title;
            session["title"] = json!(title);
        }
        if let Some(pinned) = patch.pinned {
            changed |= metadata.pinned != pinned;
            session["pinned"] = json!(pinned);
        }
        if changed {
            let Some(revision) = metadata.metadata_revision.checked_add(1) else {
                return Ok(SessionPatchOutcome::Conflict("Session metadata revision exhausted"));
            };
            session["metadata_revision"] = json!(revision);
            self.write_document(&mut tx, &session_key, &session).await?;
        }
        let summary = self.read_summary_in(&mut tx, sid, &session).await?;
        let result = json!({"session":summary});
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)")
            .bind(&receipt_key).bind(self.seal(&json!({"digest":fingerprint,"result":result}))?)
            .execute(&mut *tx).await?;
        if changed {
            sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?, 'session_updated', ?)")
                .bind(sid).bind(self.seal(&result)?).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        self.cache.lock().unwrap().invalidate(&[session_key]);
        self.context_cache.lock().unwrap().invalidate(&self.context_cache_key(sid));
        Ok(SessionPatchOutcome::Saved(result))
    }

    /// Check under the caller's write transaction, never a cached snapshot.
    pub(super) async fn require_active_session(&self, tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>, sid: &str) -> Result<()> {
        sqlx::query("UPDATE docs SET key=key WHERE key=?").bind(format!("session:{sid}")).execute(&mut **tx).await?;
        if let Some(raw) = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(format!("session:{sid}")).fetch_optional(&mut **tx).await? {
            if self.unseal(&raw)?["status"] == "recycled" { return Err(RecycledSession.into()); }
        }
        Ok(())
    }

    /// Lifecycle, event and encrypted retry receipt commit atomically. History,
    /// pinning, activity, read cursor and agent binding are deliberately retained.
    pub async fn lifecycle_session(&self, sid: &str, body: &SessionLifecycle, restore: bool) -> Result<SessionPatchOutcome> {
        let validation = SessionPatch { client_request_id: body.client_request_id.clone(),
            expected_revision: body.expected_revision, title: None, pinned: Some(false) };
        if let Err(message) = validation.validate() { return Ok(SessionPatchOutcome::Invalid(message)); }
        let key = format!("session:{sid}");
        let receipt_key = format!("receipt:session:lifecycle:{sid}:{}",body.client_request_id);
        let fingerprint = crate::digest(&json!({"body":body,"restore":restore}));
        let _guard = self.context_gate.write().await;
        let mut tx = self.pool.begin().await?;
        sqlx::query("UPDATE docs SET key=key WHERE key=?").bind(&key).execute(&mut *tx).await?;
        if let Some(raw) = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(&receipt_key).fetch_optional(&mut *tx).await? {
            let receipt = self.unseal(&raw)?;
            if receipt["digest"] != fingerprint { return Ok(SessionPatchOutcome::Conflict("Request identifier reused with different input")); }
            tx.commit().await?;
            return Ok(SessionPatchOutcome::Saved(receipt["result"].clone()));
        }
        let Some(raw) = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(&key).fetch_optional(&mut *tx).await? else { return Ok(SessionPatchOutcome::NotFound); };
        let mut session = self.unseal(&raw)?;
        let metadata: SessionMetadata = serde_json::from_value(session.clone())?;
        if metadata.metadata_revision != body.expected_revision { return Ok(SessionPatchOutcome::Conflict("Session metadata revision changed")); }
        let target = if restore { "active" } else { "recycled" };
        let changed = session["status"] != target;
        if changed && !restore {
            let unresolved: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM run_index WHERE session_id=? AND status NOT IN ('completed','cancelled','failed')")
                .bind(sid).fetch_one(&mut *tx).await?;
            if unresolved > 0 { return Ok(SessionPatchOutcome::Conflict("Resolve the existing run before recycling")); }
        }
        if changed {
            let Some(revision) = metadata.metadata_revision.checked_add(1) else { return Ok(SessionPatchOutcome::Conflict("Session metadata revision exhausted")); };
            session["status"] = json!(target);
            session["recycled_at"] = if restore { Value::Null } else { json!(crate::now()) };
            session["metadata_revision"] = json!(revision);
            self.write_document(&mut tx,&key,&session).await?;
        }
        let result = json!({"session":self.read_summary_in(&mut tx,sid,&session).await?});
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)").bind(&receipt_key)
            .bind(self.seal(&json!({"digest":fingerprint,"result":result}))?).execute(&mut *tx).await?;
        if changed {
            sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?,?,?)").bind(sid)
                .bind(if restore { "session_restored" } else { "session_recycled" })
                .bind(self.seal(&result)?).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        self.cache.lock().unwrap().invalidate(&[key]);
        self.context_cache.lock().unwrap().invalidate(&self.context_cache_key(sid));
        Ok(SessionPatchOutcome::Saved(result))
    }

    pub(super) async fn prepare_session_document(
        &self, tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>, key: &str, value: &Value,
    ) -> Result<Value> {
        let mut next = value.clone();
        let object = next.as_object_mut().ok_or_else(|| anyhow!("Invalid session document"))?;
        let previous = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(key).fetch_optional(&mut **tx).await?;
        if let Some(raw) = previous {
            let previous = self.unseal(&raw)?;
            // A runner may carry a full session snapshot from before a metadata
            // edit. Preserve the newer metadata while accepting its checkpoint.
            let stale = previous["metadata_revision"].as_u64().unwrap_or(0)
                > object.get("metadata_revision").and_then(Value::as_u64).unwrap_or(0);
            for field in ["title", "created_at", "pinned", "status", "last_active_at",
                "read_through_seq", "recycled_at", "metadata_revision", "metadata_schema_version"] {
                if let Some(value) = previous.get(field) {
                    if stale { object.insert(field.into(), value.clone()); }
                    else { object.entry(field).or_insert_with(|| value.clone()); }
                }
            }
        } else {
            object.entry("created_at").or_insert_with(|| json!(crate::now()));
        }
        if let Some(raw) = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(key).fetch_optional(&mut **tx).await? {
            let previous = self.unseal(&raw)?;
            for field in ["last_active_at", "read_through_seq"] {
                let maximum = previous[field].as_u64().unwrap_or(0).max(next[field].as_u64().unwrap_or(0));
                next[field] = json!(maximum);
            }
        }
        with_session_metadata(&next, None)
    }

    /// Transactional, retry-safe backfill on first use of each user database.
    /// The SQLite write reservation precedes all reads, fencing other processes.
    /// A failure rolls back documents and marker together. No chat/event rows,
    /// agent binding, run state, or checkpoint sequences are modified.
    pub(super) async fn migrate_session_metadata(&self) -> Result<()> {
        if let Some(raw) = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(MIGRATION_KEY).fetch_optional(&self.pool).await?
        {
            if self.unseal(&raw)?["version"] != 1 {
                return Err(anyhow!("Unsupported session metadata migration"));
            }
            return Ok(());
        }
        let _guard = self.context_gate.write().await;
        let mut tx = self.pool.begin().await?;
        // Even when no marker exists this statement reserves the SQLite writer.
        sqlx::query("UPDATE docs SET key=key WHERE key=?")
            .bind(MIGRATION_KEY).execute(&mut *tx).await?;
        if let Some(raw) = sqlx::query_scalar::<_, Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(MIGRATION_KEY).fetch_optional(&mut *tx).await?
        {
            if self.unseal(&raw)?["version"] != 1 {
                return Err(anyhow!("Unsupported session metadata migration"));
            }
            tx.commit().await?;
            return Ok(());
        }
        let mut after = String::new();
        loop {
            let rows = sqlx::query("SELECT key,value FROM docs WHERE key LIKE 'session:%' AND key>? ORDER BY key LIMIT 128")
                .bind(&after).fetch_all(&mut *tx).await?;
            if rows.is_empty() { break; }
            for row in rows {
                let key: String = row.get("key");
                let session = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
                let activity = sqlx::query_scalar::<_, Option<i64>>("SELECT MAX(created_at) FROM chat WHERE session_id=?")
                    .bind(key.strip_prefix("session:").unwrap()).fetch_one(&mut *tx).await?;
                let upgraded = with_session_metadata(&session, activity)?;
                if upgraded != session {
                    sqlx::query("UPDATE docs SET value=? WHERE key=?")
                        .bind(self.seal(&upgraded)?).bind(&key).execute(&mut *tx).await?;
                }
                after = key;
            }
        }
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)")
            .bind(MIGRATION_KEY).bind(self.seal(&json!({"version":1}))?)
            .execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn patch(request: &str, revision: u64, title: Option<&str>, pinned: Option<bool>) -> SessionPatch {
        SessionPatch { client_request_id:request.into(), expected_revision:revision,
            title:title.map(str::to_owned), pinned }
    }

    fn lifecycle(request: &str, revision: u64) -> SessionLifecycle {
        SessionLifecycle { client_request_id: request.into(), expected_revision: revision }
    }

    #[tokio::test]
    async fn lifecycle_preserves_history_metadata_retries_and_restart() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store.put("session:s", &json!({"id":"s","title":"Keep me","pinned":true,"created_at":100,"checkpoint_input":["keep"],"checkpoint_through":42})).await.unwrap();
        let mut tx = store.pool.begin().await.unwrap();
        store.insert_chat(&mut tx,"s","r","assistant",&json!("history")).await.unwrap(); tx.commit().await.unwrap();
        let history = store.chat_snapshot("s").await.unwrap();
        let stale = store.get("session:s").await.unwrap().unwrap();
        let SessionPatchOutcome::Saved(result) = store.lifecycle_session("s",&lifecycle("delete",0),false).await.unwrap() else { panic!() };
        assert_eq!(result["session"]["status"],"recycled");
        assert_eq!(result["session"]["metadata_revision"],1);
        assert!(store.ordered_sessions().await.unwrap().is_empty());
        assert_eq!(store.ordered_sessions_status(true).await.unwrap().len(),1);
        assert_eq!(store.chat_snapshot("s").await.unwrap(),history);
        store.put("session:s",&stale).await.unwrap();
        assert_eq!(store.get("session:s").await.unwrap().unwrap()["status"],"recycled");
        let SessionPatchOutcome::Saved(retry) = store.lifecycle_session("s",&lifecycle("delete",0),false).await.unwrap() else { panic!() };
        assert_eq!(result,retry);
        assert!(matches!(store.lifecycle_session("s",&lifecycle("delete",0),true).await.unwrap(),SessionPatchOutcome::Conflict(_)));
        assert!(matches!(store.lifecycle_session("s",&lifecycle("restore-stale",0),true).await.unwrap(),SessionPatchOutcome::Conflict(_)));
        assert!(matches!(store.lifecycle_session("missing",&lifecycle("missing",0),false).await.unwrap(),SessionPatchOutcome::NotFound));
        assert!(matches!(store.lifecycle_session("s",&lifecycle(" ",1),true).await.unwrap(),SessionPatchOutcome::Invalid(_)));
        let resumed = Store::open(root.path()).await.unwrap();
        let SessionPatchOutcome::Saved(restored) = resumed.lifecycle_session("s",&lifecycle("restore",1),true).await.unwrap() else { panic!() };
        assert_eq!(restored["session"]["status"],"active");
        assert!(restored["session"]["recycled_at"].is_null());
        for field in ["title","pinned","last_active_at","read_through_seq","created_at"] { assert_eq!(restored["session"][field],stale[field]); }
        let saved = resumed.get("session:s").await.unwrap().unwrap();
        assert_eq!(saved["checkpoint_input"],stale["checkpoint_input"]);
        assert_eq!(saved["checkpoint_through"],42);
        assert_eq!(resumed.chat_snapshot("s").await.unwrap(),history);
        let raw: Vec<u8> = sqlx::query_scalar("SELECT value FROM docs WHERE key='receipt:session:lifecycle:s:delete'").fetch_one(&resumed.pool).await.unwrap();
        assert!(!String::from_utf8_lossy(&raw).contains("Keep me"));
        // Retrying an old delete after restore returns its receipt, not a new deletion.
        resumed.lifecycle_session("s",&lifecycle("delete",0),false).await.unwrap();
        assert_eq!(resumed.get("session:s").await.unwrap().unwrap()["status"],"active");
    }

    #[tokio::test]
    async fn lifecycle_blocks_all_nonterminal_states_and_rolls_back_receipt_failure() {
        let root = tempfile::tempdir().unwrap(); let store = Store::open(root.path()).await.unwrap();
        store.put("session:s",&json!({"id":"s"})).await.unwrap();
        for status in ["queued","running","waiting_approval","waiting_recovery","stopped","unknown_future_state"] {
            store.put("run:r",&json!({"id":"r","session_id":"s","status":status})).await.unwrap();
            assert!(matches!(store.lifecycle_session("s",&lifecycle("delete",0),false).await.unwrap(),SessionPatchOutcome::Conflict(_)),"{status}");
            assert_eq!(store.get("session:s").await.unwrap().unwrap()["status"],"active");
        }
        store.put("run:r",&json!({"id":"r","session_id":"s","status":"completed"})).await.unwrap();
        sqlx::query("CREATE TRIGGER reject_lifecycle BEFORE INSERT ON docs WHEN NEW.key LIKE 'receipt:session:lifecycle:%' BEGIN SELECT RAISE(ABORT,'receipt failure'); END").execute(&store.pool).await.unwrap();
        assert!(store.lifecycle_session("s",&lifecycle("delete",0),false).await.is_err());
        assert_eq!(store.get("session:s").await.unwrap().unwrap()["status"],"active");
        let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM events WHERE kind='session_recycled'").fetch_one(&store.pool).await.unwrap(); assert_eq!(count,0);
        sqlx::query("DROP TRIGGER reject_lifecycle").execute(&store.pool).await.unwrap();
        store.lifecycle_session("s",&lifecycle("delete",0),false).await.unwrap();
        assert!(store.put("run:new",&json!({"id":"new","session_id":"s","status":"queued"})).await.unwrap_err().downcast_ref::<RecycledSession>().is_some());
        let mut run = store.get("run:r").await.unwrap().unwrap(); run["status"] = json!("queued");
        assert!(store.save_run_override(&mut run,"resumed",&json!({})).await.unwrap_err().downcast_ref::<RecycledSession>().is_some());
        assert!(store.atomic_docs_event(vec![],"s","message",&json!({"role":"user","run_id":"new","content":"blocked"})).await.unwrap_err().downcast_ref::<RecycledSession>().is_some());
        assert!(store.chat_snapshot("s").await.unwrap().0.is_empty());
    }

    #[tokio::test]
    async fn lifecycle_noops_and_scheduler_claim_do_not_revive_recycled_sessions() {
        let root = tempfile::tempdir().unwrap(); let store = Store::open(root.path()).await.unwrap();
        store.put("session:s",&json!({"id":"s"})).await.unwrap();
        store.lifecycle_session("s",&lifecycle("already-active",0),true).await.unwrap();
        assert_eq!(store.get("session:s").await.unwrap().unwrap()["metadata_revision"],0);
        store.put("run:r",&json!({"id":"r","session_id":"s","status":"queued","generation":"","lease_epoch":0,"run_revision":0})).await.unwrap();
        // A legacy inconsistent fixture must not bypass scheduler admission.
        let mut session = store.get("session:s").await.unwrap().unwrap();
        session["status"] = json!("recycled"); session["metadata_revision"] = json!(1); session["recycled_at"] = json!(1);
        store.put("session:s",&session).await.unwrap();
        let mut run = store.get("run:r").await.unwrap().unwrap();
        assert!(store.claim_run(&mut run,"test-owner","test-generation").await.unwrap_err().downcast_ref::<RecycledSession>().is_some());
        assert_eq!(store.get("run:r").await.unwrap().unwrap()["status"],"queued");
        store.lifecycle_session("s",&lifecycle("already-recycled",1),false).await.unwrap();
        assert_eq!(store.get("session:s").await.unwrap().unwrap()["metadata_revision"],1);
        let count:i64 = sqlx::query_scalar("SELECT COUNT(*) FROM events WHERE kind IN ('session_recycled','session_restored')").fetch_one(&store.pool).await.unwrap();
        assert_eq!(count,0);
    }

    #[tokio::test]
    async fn lifecycle_and_admission_race_across_independent_stores() {
        let root = tempfile::tempdir().unwrap(); let a = Store::open(root.path()).await.unwrap(); let b = Store::open(root.path()).await.unwrap();
        for i in 0..12 {
            let sid = format!("race-{i}"); a.put(&format!("session:{sid}"),&json!({"id":sid})).await.unwrap();
            let request = lifecycle("delete",0);
            let docs = vec![(format!("run:r-{i}"),json!({"id":format!("r-{i}"),"session_id":sid,"status":"queued"}))];
            let payload = json!({"role":"user","content":"hello","run_id":format!("r-{i}")});
            let (deleted, admitted) = tokio::join!(a.lifecycle_session(&sid,&request,false),b.atomic_docs_event(docs,&sid,"message",&payload));
            let deleted = matches!(deleted.unwrap(),SessionPatchOutcome::Saved(_));
            assert_ne!(deleted,admitted.is_ok());
            if deleted { assert_eq!(a.chat_snapshot(&sid).await.unwrap().0.len(),0); }
            else { assert_eq!(a.get(&format!("session:{sid}")).await.unwrap().unwrap()["status"],"active"); }
        }
    }

    #[tokio::test]
    async fn publication_activity_read_cursor_and_checkpoint_preservation() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store.put("session:s", &json!({"id":"s","created_at":100,"last_active_at":100})).await.unwrap();
        store.put("session:other", &json!({"id":"other","created_at":100})).await.unwrap();
        let stale = store.get("session:s").await.unwrap().unwrap();
        let mut tx = store.pool.begin().await.unwrap();
        store.insert_chat(&mut tx,"s","r1","user",&json!("hello")).await.unwrap();
        store.insert_chat(&mut tx,"s","r1","assistant",&json!("reply")).await.unwrap();
        store.insert_chat(&mut tx,"other","r2","assistant",&json!("elsewhere")).await.unwrap();
        tx.commit().await.unwrap();
        let (messages, cursor) = store.chat_snapshot("s").await.unwrap();
        let summary = store.session_read_summary("s").await.unwrap();
        assert_eq!(summary["unread_count"],1);
        assert_eq!(summary["is_unread"],true);
        assert_eq!(summary["last_active_at"],messages[1]["created_at"]);
        assert_eq!(summary["metadata_revision"],0);
        assert!(matches!(store.acknowledge_session_read("s",cursor as u64+1).await.unwrap(),SessionPatchOutcome::Invalid(_)));
        assert!(matches!(store.acknowledge_session_read("s",u64::MAX).await.unwrap(),SessionPatchOutcome::Invalid(_)));
        assert!(matches!(store.acknowledge_session_read("missing",0).await.unwrap(),SessionPatchOutcome::NotFound));
        let SessionPatchOutcome::Saved(read) = store.acknowledge_session_read("s",cursor as u64).await.unwrap() else { panic!() };
        assert_eq!(read["session"]["unread_count"],0);
        store.acknowledge_session_read("s",messages[0]["seq"].as_u64().unwrap()).await.unwrap();
        store.acknowledge_session_read("s",cursor as u64).await.unwrap();
        store.put("session:s", &stale).await.unwrap();
        let after = store.session_read_summary("s").await.unwrap();
        assert_eq!(after["read_through_seq"],cursor);
        assert_eq!(after["last_active_at"],summary["last_active_at"]);
        assert_eq!(after["metadata_revision"],0);
        let mut tx = store.pool.begin().await.unwrap();
        store.insert_chat(&mut tx,"s","r1","assistant",&json!("reply")).await.unwrap();
        tx.commit().await.unwrap();
        assert_eq!(store.chat_snapshot("s").await.unwrap().0.len(),2);
        assert_eq!(store.session_read_summary("s").await.unwrap(),after);
        let events:i64 = sqlx::query_scalar("SELECT COUNT(*) FROM events WHERE kind='session_read'").fetch_one(&store.pool).await.unwrap();
        assert_eq!(events,1);
        let reopened = Store::open(root.path()).await.unwrap();
        assert_eq!(reopened.session_read_summary("s").await.unwrap(),after);
    }

    #[tokio::test]
    async fn read_ack_cross_store_race_preserves_maximum_and_new_unread() {
        let root = tempfile::tempdir().unwrap();
        let first = Store::open(root.path()).await.unwrap();
        first.put("session:s", &json!({"id":"s","created_at":1})).await.unwrap();
        let mut tx = first.pool.begin().await.unwrap();
        first.insert_chat(&mut tx,"s","r1","assistant",&json!("one")).await.unwrap();
        first.insert_chat(&mut tx,"s","r2","assistant",&json!("two")).await.unwrap();
        tx.commit().await.unwrap();
        let (messages, cursor) = first.chat_snapshot("s").await.unwrap();
        let second = Store::open(root.path()).await.unwrap();
        let (a,b) = tokio::join!(first.acknowledge_session_read("s",cursor as u64),
            second.acknowledge_session_read("s",messages[0]["seq"].as_u64().unwrap()));
        assert!(matches!(a.unwrap(),SessionPatchOutcome::Saved(_)));
        assert!(matches!(b.unwrap(),SessionPatchOutcome::Saved(_)));
        assert_eq!(first.session_read_summary("s").await.unwrap()["read_through_seq"],cursor);
        let mut tx = first.pool.begin().await.unwrap();
        first.insert_chat(&mut tx,"s","r3","assistant",&json!("three")).await.unwrap();
        tx.commit().await.unwrap();
        first.acknowledge_session_read("s",cursor as u64).await.unwrap();
        let summary = first.session_read_summary("s").await.unwrap();
        assert_eq!(summary["unread_count"],1);
        assert_eq!(summary["is_unread"],true);
        assert_eq!(summary["read_through_seq"],cursor);
        let SessionPatchOutcome::Saved(patched) = first.patch_session("s",&patch("rename",0,Some("renamed"),Some(true))).await.unwrap() else { panic!() };
        assert_eq!(patched["session"]["unread_count"],1);
        assert_eq!(patched["session"]["last_active_at"],summary["last_active_at"]);
        assert_eq!(patched["session"]["read_through_seq"],cursor);
    }

    #[tokio::test]
    async fn activity_and_read_event_failures_roll_back() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store.put("session:s", &json!({"id":"s","created_at":1})).await.unwrap();
        let mut tx = store.pool.begin().await.unwrap();
        store.insert_chat(&mut tx,"s","r","assistant",&json!("reply")).await.unwrap();
        tx.rollback().await.unwrap();
        assert_eq!(store.chat_snapshot("s").await.unwrap().0.len(),0);
        assert_eq!(store.session_read_summary("s").await.unwrap()["last_active_at"],1);
        let mut tx = store.pool.begin().await.unwrap();
        store.insert_chat(&mut tx,"s","r","assistant",&json!("reply")).await.unwrap();
        tx.commit().await.unwrap();
        let cursor = store.chat_snapshot("s").await.unwrap().1;
        sqlx::query("CREATE TRIGGER fail_read BEFORE INSERT ON events WHEN NEW.kind='session_read' BEGIN SELECT RAISE(ABORT,'test failure'); END").execute(&store.pool).await.unwrap();
        assert!(store.acknowledge_session_read("s",cursor as u64).await.is_err());
        assert_eq!(store.session_read_summary("s").await.unwrap()["read_through_seq"],0);
    }

    #[test]
    fn session_patch_validation() {
        for title in ["", "   ", "bad\nname", &"é".repeat(101)] {
            assert!(patch("r", 0, Some(title), None).validate().is_err());
        }
        assert!(patch("r", 0, Some(&"é".repeat(100)), None).validate().is_ok());
        assert!(patch("r", 0, None, Some(false)).validate().is_ok());
        assert!(patch("r", 0, None, None).validate().is_err());
        assert!(patch(" r", 0, None, Some(true)).validate().is_err());
        assert!(serde_json::from_value::<SessionPatch>(json!({"client_request_id":"r",
            "expected_revision":0,"pinned":true,"agent_id":"other"})).is_err());
    }

    #[tokio::test]
    async fn session_patch_atomic_retry_and_stale_checkpoint() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store.put("session:s", &json!({"id":"s","title":"Old","created_at":100,
            "last_active_at":150,"read_through_seq":7,"checkpoint_through":42})).await.unwrap();
        let mut stale = store.get("session:s").await.unwrap().unwrap();
        let request = patch("request", 0, Some("  Private renamed title  "), Some(true));
        let SessionPatchOutcome::Saved(result) = store.patch_session("s", &request).await.unwrap()
            else { panic!("patch failed") };
        assert_eq!(result["session"]["title"], "Private renamed title");
        assert_eq!(result["session"]["metadata_revision"], 1);
        assert_eq!(result["session"]["last_active_at"], 150);
        assert_eq!(result["session"]["read_through_seq"], 7);
        assert!(result["session"].get("checkpoint_through").is_none());
        let SessionPatchOutcome::Saved(retry) = store.patch_session("s", &request).await.unwrap()
            else { panic!("retry failed") };
        assert_eq!(retry, result);
        assert!(matches!(store.patch_session("s", &patch("request", 0, Some("Different"), None)).await.unwrap(),
            SessionPatchOutcome::Conflict(_)));
        assert!(matches!(store.patch_session("s", &patch("stale", 0, None, Some(false))).await.unwrap(),
            SessionPatchOutcome::Conflict(_)));
        stale["checkpoint_through"] = json!(99);
        store.put("session:s", &stale).await.unwrap();
        let saved = store.get("session:s").await.unwrap().unwrap();
        assert_eq!(saved["title"], "Private renamed title");
        assert_eq!(saved["pinned"], true);
        assert_eq!(saved["metadata_revision"], 1);
        assert_eq!(saved["checkpoint_through"], 99);
        let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM events WHERE kind='session_updated'")
            .fetch_one(&store.pool).await.unwrap();
        assert_eq!(count, 1);
        // A no-op is receipted but creates no new revision/event.
        assert!(matches!(store.patch_session("s", &patch("noop", 1, None, Some(true))).await.unwrap(),
            SessionPatchOutcome::Saved(_)));
        assert_eq!(store.get("session:s").await.unwrap().unwrap()["metadata_revision"], 1);
        let raws: Vec<Vec<u8>> = sqlx::query_scalar("SELECT value FROM docs")
            .fetch_all(&store.pool).await.unwrap();
        assert!(raws.iter().all(|raw| !String::from_utf8_lossy(raw).contains("Private renamed title")));
        let reopened = Store::open(root.path()).await.unwrap();
        assert_eq!(reopened.get("session:s").await.unwrap().unwrap(), saved);
        assert!(matches!(reopened.patch_session("missing", &request).await.unwrap(), SessionPatchOutcome::NotFound));
    }

    #[tokio::test]
    async fn session_patch_ordering_isolation_and_cross_process_conflict() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        for (id, pinned, active) in [("a",false,200),("b",false,200),("p",true,100),("new",false,300)] {
            store.put(&format!("session:{id}"), &json!({"id":id,"pinned":pinned,"created_at":100,
                "last_active_at":active,"checkpoint_input":["secret"]})).await.unwrap();
        }
        store.put("session:recycled", &json!({"id":"recycled","status":"recycled","pinned":true})).await.unwrap();
        let list = store.ordered_sessions().await.unwrap();
        assert_eq!(list.iter().map(|s|s["id"].as_str().unwrap()).collect::<Vec<_>>(), vec!["p","new","a","b"]);
        assert!(list.iter().all(|s|s.get("checkpoint_input").is_none()));
        assert!(matches!(store.patch_session("recycled", &patch("r",0,None,Some(false))).await.unwrap(),SessionPatchOutcome::Conflict(_)));
        sqlx::query("INSERT INTO user_accounts(user_id,username,value) VALUES('other','other',?)")
            .bind(store.seal(&json!({"user_id":"other","username":"other"})).unwrap())
            .execute(&store.app_pool).await.unwrap();
        let other = store.for_user("other").await.unwrap();
        assert!(other.ordered_sessions().await.unwrap().is_empty());
        assert!(matches!(other.patch_session("a", &patch("r",0,None,Some(true))).await.unwrap(),SessionPatchOutcome::NotFound));
        // Independently opened Store instances do not share the in-process gate.
        let independent = Store::open(root.path()).await.unwrap();
        let first = patch("one",0,Some("Winner one"),None);
        let second = patch("two",0,Some("Winner two"),None);
        let (one,two) = tokio::join!(store.patch_session("a",&first), independent.patch_session("a",&second));
        let outcomes = [one.unwrap(),two.unwrap()];
        assert_eq!(outcomes.iter().filter(|o| matches!(o,SessionPatchOutcome::Saved(_))).count(),1);
        assert_eq!(outcomes.iter().filter(|o| matches!(o,SessionPatchOutcome::Conflict(_))).count(),1);
        assert_eq!(store.get("session:a").await.unwrap().unwrap()["metadata_revision"],1);
    }

    #[tokio::test]
    async fn session_patch_receipt_failure_rolls_back_everything() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store.put("session:s", &json!({"id":"s","title":"Original"})).await.unwrap();
        let original = store.get("session:s").await.unwrap().unwrap();
        sqlx::query("CREATE TRIGGER reject_patch_receipt BEFORE INSERT ON docs WHEN NEW.key LIKE 'receipt:session:patch:%' BEGIN SELECT RAISE(ABORT,'fixture receipt failure'); END")
            .execute(&store.pool).await.unwrap();
        let request = patch("atomic",0,Some("Changed"),Some(true));
        assert!(store.patch_session("s",&request).await.is_err());
        assert_eq!(store.get("session:s").await.unwrap().unwrap(),original);
        let count:i64 = sqlx::query_scalar("SELECT COUNT(*) FROM events WHERE kind='session_updated'")
            .fetch_one(&store.pool).await.unwrap();
        assert_eq!(count,0);
        sqlx::query("DROP TRIGGER reject_patch_receipt").execute(&store.pool).await.unwrap();
        assert!(matches!(store.patch_session("s",&request).await.unwrap(),SessionPatchOutcome::Saved(_)));
    }

    #[test]
    fn session_metadata_defaults_and_preservation() {
        let original = json!({"id":"s", "agent_id":"a", "title":"Original",
            "created_at":100, "checkpoint_input":[{"text":"keep"}], "checkpoint_through":42});
        let upgraded = with_session_metadata(&original, Some(150)).unwrap();
        assert_eq!(upgraded["last_active_at"], 150);
        assert_eq!(upgraded["pinned"], false);
        assert_eq!(upgraded["status"], "active");
        assert_eq!(upgraded["read_through_seq"], 0);
        for key in ["id", "agent_id", "title", "created_at", "checkpoint_input", "checkpoint_through"] {
            assert_eq!(original[key], upgraded[key]);
        }
        assert_eq!(with_session_metadata(&upgraded, Some(999)).unwrap(), upgraded);
        let mut edited = upgraded.clone();
        edited["pinned"] = json!(true);
        edited["status"] = json!("recycled");
        edited["recycled_at"] = json!(200);
        edited["read_through_seq"] = json!(17);
        edited["metadata_revision"] = json!(3);
        assert_eq!(with_session_metadata(&edited, None).unwrap(), edited);
        assert_eq!(with_session_metadata(&original, None).unwrap()["last_active_at"], 100);
        let mut invalid = original;
        invalid["pinned"] = json!("yes");
        assert!(with_session_metadata(&invalid, None).is_err());
    }

    #[tokio::test]
    async fn session_metadata_partial_writes_preserve_existing_values() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        let original = json!({"id":"s", "title":"Keep title", "created_at":100,
            "pinned":true, "status":"recycled", "recycled_at":160,
            "last_active_at":150, "read_through_seq":17, "metadata_revision":3});
        store.put("session:s", &original).await.unwrap();
        store.put("session:s", &json!({"id":"s", "checkpoint_input":[{"text":"updated"}], "checkpoint_through":42}))
            .await.unwrap();
        let saved = store.get("session:s").await.unwrap().unwrap();
        for field in ["title", "created_at", "pinned", "status", "recycled_at", "last_active_at", "read_through_seq", "metadata_revision"] {
            assert_eq!(saved[field], original[field]);
        }
        assert_eq!(saved["checkpoint_through"], 42);
    }

    #[tokio::test]
    async fn session_metadata_migration_is_user_scoped_and_rolls_back() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        sqlx::query("INSERT INTO user_accounts(user_id,username,value) VALUES('other','other',?)")
            .bind(store.seal(&json!({"user_id":"other", "username":"other"})).unwrap())
            .execute(&store.app_pool).await.unwrap();
        let other = store.for_user("other").await.unwrap();
        let app_marker: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM docs WHERE key=?")
            .bind(MIGRATION_KEY).fetch_one(&store.app_pool).await.unwrap();
        assert_eq!(app_marker, 0);
        sqlx::query("DELETE FROM docs WHERE key=?").bind(MIGRATION_KEY).execute(&other.pool).await.unwrap();
        let valid = json!({"id":"a", "created_at":100, "title":"Untouched until commit"});
        let invalid = json!({"id":"z", "created_at":100, "pinned":"invalid"});
        for (key, value) in [("session:a", &valid), ("session:z", &invalid)] {
            sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)")
                .bind(key).bind(store.seal(value).unwrap()).execute(&other.pool).await.unwrap();
        }
        assert!(other.migrate_session_metadata().await.is_err());
        assert_eq!(other.get("session:a").await.unwrap().unwrap(), valid);
        let marker: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM docs WHERE key=?")
            .bind(MIGRATION_KEY).fetch_one(&other.pool).await.unwrap();
        assert_eq!(marker, 0);
        sqlx::query("DELETE FROM docs WHERE key='session:z'").execute(&other.pool).await.unwrap();
        other.migrate_session_metadata().await.unwrap();
        assert_eq!(other.get("session:a").await.unwrap().unwrap()["last_active_at"], 100);
        assert!(store.get("session:a").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn session_metadata_migration_persistence_and_atomic_creation() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        // Seed an old document through SQL only in this disposable test database.
        let original = json!({"id":"old", "agent_id":"a", "title":"Secret legacy title",
            "created_at":100, "checkpoint_input":[], "checkpoint_through":7});
        sqlx::query("DELETE FROM docs WHERE key=?").bind(MIGRATION_KEY).execute(&store.pool).await.unwrap();
        sqlx::query("INSERT INTO docs(key,value) VALUES('session:old',?)")
            .bind(store.seal(&original).unwrap()).execute(&store.pool).await.unwrap();
        sqlx::query("INSERT INTO chat(id,session_id,run_id,role,content,created_at) VALUES('m','old','r','assistant',?,150)")
            .bind(store.seal(&json!("Secret message")).unwrap()).execute(&store.pool).await.unwrap();
        let (_, cursor) = store.chat_snapshot("old").await.unwrap();
        store.migrate_session_metadata().await.unwrap();
        let migrated = store.get("session:old").await.unwrap().unwrap();
        assert_eq!(migrated["last_active_at"], 150);
        assert_eq!(migrated["checkpoint_through"], 7);
        assert_eq!(store.chat_snapshot("old").await.unwrap().1, cursor);
        let raw: Vec<u8> = sqlx::query_scalar("SELECT value FROM docs WHERE key='session:old'")
            .fetch_one(&store.pool).await.unwrap();
        assert!(!String::from_utf8_lossy(&raw).contains("Secret legacy title"));
        store.migrate_session_metadata().await.unwrap();
        let again: Vec<u8> = sqlx::query_scalar("SELECT value FROM docs WHERE key='session:old'")
            .fetch_one(&store.pool).await.unwrap();
        assert_eq!(raw, again);
        store.atomic_docs_event(vec![("session:new".into(), json!({"id":"new", "title":"New", "created_at":200}))],
            "new", "session_created", &json!({})).await.unwrap();
        assert_eq!(store.get("session:new").await.unwrap().unwrap()["metadata_revision"], 0);
        store.put("session:put", &json!({"id":"put", "created_at":300})).await.unwrap();
        assert_eq!(store.get("session:put").await.unwrap().unwrap()["last_active_at"], 300);
        let reopened = Store::open(root.path()).await.unwrap();
        assert_eq!(reopened.get("session:old").await.unwrap().unwrap(), migrated);
    }
}
