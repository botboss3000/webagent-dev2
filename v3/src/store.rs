use crate::cache::Cache;
use aes_gcm::{
    aead::{Aead, KeyInit},
    Aes256Gcm, Nonce,
};
use anyhow::{anyhow, Result};
use rand::RngCore;
use serde_json::{json, Value};
use sqlx::{
    sqlite::{SqliteConnectOptions, SqliteJournalMode, SqliteSynchronous},
    Row, SqlitePool,
};
use std::sync::{Arc, Mutex};
use std::{fs::OpenOptions, os::unix::fs::OpenOptionsExt, path::Path, str::FromStr};

#[derive(Clone)]
pub struct Store {
    pub pool: SqlitePool,
    cipher: Aes256Gcm,
    cache: Arc<Mutex<Cache>>,
}

impl Store {
    pub async fn open(root: &Path) -> Result<Self> {
        std::fs::create_dir_all(root)?;
        let keypath = root.join("encryption.key");
        let key = match std::fs::read(&keypath) {
            Ok(k) => k,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                let mut k = vec![0; 32];
                rand::thread_rng().fill_bytes(&mut k);
                use std::io::Write;
                OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(&keypath)?
                    .write_all(&k)?;
                k
            }
            Err(e) => return Err(e.into()),
        };
        if key.len() != 32 {
            return Err(anyhow!("Invalid storage encryption key"));
        }
        let options = SqliteConnectOptions::from_str(&format!(
            "sqlite://{}",
            root.join("runtime.sqlite").display()
        ))?
        .create_if_missing(true)
        .foreign_keys(true)
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Full)
        .busy_timeout(std::time::Duration::from_secs(5));
        let pool = sqlx::sqlite::SqlitePoolOptions::new()
            .max_connections(4)
            .connect_with(options)
            .await?;
        sqlx::raw_sql("CREATE TABLE IF NOT EXISTS docs (key TEXT PRIMARY KEY, value BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, kind TEXT NOT NULL, payload BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS events_session ON events(session_id,seq);
        CREATE TABLE IF NOT EXISTS run_index(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,status TEXT NOT NULL,updated_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS runs_status ON run_index(status,updated_at);
        CREATE INDEX IF NOT EXISTS runs_session ON run_index(session_id,updated_at);").execute(&pool).await?;
        Ok(Self {
            pool,
            cipher: Aes256Gcm::new_from_slice(&key)
                .map_err(|_| anyhow!("Invalid encryption key"))?,
            cache: Arc::new(Mutex::new(Cache::new(64 * 1024 * 1024))),
        })
    }
    pub fn seal(&self, value: &Value) -> Result<Vec<u8>> {
        let mut nonce = [0; 12];
        rand::thread_rng().fill_bytes(&mut nonce);
        let mut data = nonce.to_vec();
        data.extend(
            self.cipher
                .encrypt(
                    Nonce::from_slice(&nonce),
                    serde_json::to_vec(value)?.as_ref(),
                )
                .map_err(|_| anyhow!("Encryption failed"))?,
        );
        Ok(data)
    }
    pub fn unseal(&self, data: &[u8]) -> Result<Value> {
        if data.len() < 28 {
            return Err(anyhow!("Invalid encrypted record"));
        }
        let plain = self
            .cipher
            .decrypt(Nonce::from_slice(&data[..12]), &data[12..])
            .map_err(|_| anyhow!("Storage authentication failed"))?;
        Ok(serde_json::from_slice(&plain)?)
    }
    pub async fn get(&self, key: &str) -> Result<Option<Value>> {
        let cacheable = key.starts_with("run:") || key.starts_with("session:");
        let epoch = {
            let mut cache = self.cache.lock().unwrap();
            if cacheable {
                if let Some(value) = cache.get(key) {
                    return Ok(Some(value));
                }
            }
            cache.epoch
        };
        let row = sqlx::query("SELECT value FROM docs WHERE key=?")
            .bind(key)
            .fetch_optional(&self.pool)
            .await?;
        let result = row
            .map(|r| self.unseal(r.get::<Vec<u8>, _>("value").as_slice()))
            .transpose()?;
        if cacheable {
            if let Some(value) = &result {
                self.cache
                    .lock()
                    .unwrap()
                    .insert(key.into(), value.clone(), epoch);
            }
        }
        Ok(result)
    }
    pub async fn put(&self, key: &str, value: &Value) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(key).bind(self.seal(value)?).execute(&mut *tx).await?;
        if key.starts_with("run:") {
            self.index_run(&mut tx, value).await?;
        }
        tx.commit().await?;
        self.cache.lock().unwrap().invalidate(&[key.into()]);
        Ok(())
    }
    async fn index_run(
        &self,
        tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
        run: &Value,
    ) -> Result<()> {
        sqlx::query("INSERT INTO run_index(id,session_id,status,updated_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at")
            .bind(run["id"].as_str().ok_or_else(||anyhow!("Invalid run"))?).bind(run["session_id"].as_str().unwrap_or(""))
            .bind(run["status"].as_str().unwrap_or("failed")).bind(run["updated_at"].as_i64().unwrap_or(0)).execute(&mut **tx).await?;
        let view = serde_json::json!({"id":run["id"],"session_id":run["session_id"],"status":run["status"],"error":run["error"],
            "pending":run["pending"],"uncertain":run["uncertain"],"turns":run["turns"],"phase":run["phase"],"updated_at":run["updated_at"]});
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(format!("run_view:{}",run["id"].as_str().unwrap())).bind(self.seal(&view)?).execute(&mut **tx).await?;
        Ok(())
    }
    pub async fn active_runs(&self) -> Result<Vec<Value>> {
        let rows=sqlx::query("SELECT d.value FROM run_index r JOIN docs d ON d.key='run_view:'||r.id WHERE r.status NOT IN ('completed','cancelled','failed') ORDER BY r.updated_at LIMIT 200")
            .fetch_all(&self.pool).await?;
        rows.iter()
            .map(|r| self.unseal(&r.get::<Vec<u8>, _>("value")))
            .collect()
    }
    pub async fn recent_runs(&self, sid: Option<&str>) -> Result<Vec<Value>> {
        let rows = if let Some(sid) = sid {
            sqlx::query("SELECT d.value FROM run_index r JOIN docs d ON d.key='run_view:'||r.id WHERE r.session_id=? ORDER BY r.updated_at DESC LIMIT 100").bind(sid).fetch_all(&self.pool).await?
        } else {
            sqlx::query("SELECT d.value FROM run_index r JOIN docs d ON d.key='run_view:'||r.id WHERE r.status='failed' ORDER BY r.updated_at DESC LIMIT 50").fetch_all(&self.pool).await?
        };
        rows.iter()
            .map(|r| self.unseal(&r.get::<Vec<u8>, _>("value")))
            .collect()
    }
    pub async fn delete(&self, key: &str) -> Result<()> {
        sqlx::query("DELETE FROM docs WHERE key=?")
            .bind(key)
            .execute(&self.pool)
            .await?;
        self.cache.lock().unwrap().invalidate(&[key.into()]);
        Ok(())
    }
    pub async fn list(&self, prefix: &str) -> Result<Vec<Value>> {
        let rows = sqlx::query("SELECT value FROM docs WHERE key LIKE ? ORDER BY key")
            .bind(format!("{prefix}%"))
            .fetch_all(&self.pool)
            .await?;
        rows.iter()
            .map(|r| self.unseal(r.get::<Vec<u8>, _>("value").as_slice()))
            .collect()
    }
    pub async fn event(&self, sid: &str, kind: &str, payload: &Value) -> Result<()> {
        sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?,?,?)")
            .bind(sid)
            .bind(kind)
            .bind(self.seal(payload)?)
            .execute(&self.pool)
            .await?;
        Ok(())
    }
    pub async fn save_run(&self, run: &Value, kind: &str, payload: &Value) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(format!("run:{}",run["id"].as_str().unwrap())).bind(self.seal(run)?).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?,?,?)")
            .bind(run["session_id"].as_str().unwrap())
            .bind(kind)
            .bind(self.seal(payload)?)
            .execute(&mut *tx)
            .await?;
        if let Some(row) = sqlx::query("SELECT value FROM docs WHERE key=?")
            .bind(format!("session:{}", run["session_id"].as_str().unwrap()))
            .fetch_optional(&mut *tx)
            .await?
        {
            let mut session = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
            session["context"] = run["input"].clone();
            sqlx::query("UPDATE docs SET value=? WHERE key=?")
                .bind(self.seal(&session)?)
                .bind(format!("session:{}", run["session_id"].as_str().unwrap()))
                .execute(&mut *tx)
                .await?;
        }
        self.index_run(&mut tx, run).await?;
        tx.commit().await?;
        self.cache.lock().unwrap().invalidate(&[
            format!("run:{}", run["id"].as_str().unwrap()),
            format!("session:{}", run["session_id"].as_str().unwrap()),
        ]);
        Ok(())
    }
    pub async fn events(&self, sid: &str, after: i64) -> Result<Vec<Value>> {
        let rows=sqlx::query("SELECT seq,kind,payload FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT 200")
            .bind(sid).bind(after).fetch_all(&self.pool).await?;
        rows.iter().map(|r|Ok(json!({"seq":r.get::<i64,_>("seq"),"kind":r.get::<String,_>("kind"),"payload":self.unseal(&r.get::<Vec<u8>,_>("payload"))?}))).collect()
    }
    pub async fn atomic_docs_event(
        &self,
        docs: Vec<(String, Value)>,
        sid: &str,
        kind: &str,
        payload: &Value,
    ) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        let touched = docs.iter().map(|(key, _)| key.clone()).collect::<Vec<_>>();
        for (key, value) in docs {
            sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
                .bind(&key).bind(self.seal(&value)?).execute(&mut *tx).await?;
            if key.starts_with("run:") {
                self.index_run(&mut tx, &value).await?;
            }
        }
        sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?,?,?)")
            .bind(sid)
            .bind(kind)
            .bind(self.seal(payload)?)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        self.cache.lock().unwrap().invalidate(&touched);
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn encrypted_records_survive_reopen_and_tampering_is_rejected() {
        let root = tempfile::tempdir().unwrap();
        let s = Store::open(root.path()).await.unwrap();
        s.put("secret", &json!({"content":"private text"}))
            .await
            .unwrap();
        let raw: Vec<u8> = sqlx::query_scalar("SELECT value FROM docs WHERE key='secret'")
            .fetch_one(&s.pool)
            .await
            .unwrap();
        assert!(!raw.windows(12).any(|w| w == b"private text"));
        let mut corrupt = raw.clone();
        corrupt[15] ^= 1;
        assert!(s.unseal(&corrupt).is_err());
        s.pool.close().await;
        let reopened = Store::open(root.path()).await.unwrap();
        assert_eq!(
            reopened.get("secret").await.unwrap().unwrap()["content"],
            "private text"
        );
    }
    #[tokio::test]
    async fn replay_cursor_has_no_duplicate_events() {
        let root = tempfile::tempdir().unwrap();
        let s = Store::open(root.path()).await.unwrap();
        s.event("a", "message", &json!({"text":"one"}))
            .await
            .unwrap();
        let events = s.events("a", 0).await.unwrap();
        assert_eq!(events.len(), 1);
        assert!(s
            .events("a", events[0]["seq"].as_i64().unwrap())
            .await
            .unwrap()
            .is_empty());
        assert!(s.events("b", 0).await.unwrap().is_empty());
    }
}
