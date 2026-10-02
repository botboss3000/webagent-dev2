//! Durable storage for the three owners: application, agent, and user.
//! A run revision and a time-limited lease stop stale processes from replacing
//! newer progress. See docs/code-tour.md before following the SQL below.
use crate::access;
use crate::cache::{Cache, ContextCache};
use aes_gcm::{
    aead::{Aead, KeyInit},
    Aes256Gcm, Nonce,
};
use anyhow::{anyhow, Result};
use rand::RngCore;
use serde_json::{json, Value};
use sqlx::{
    sqlite::{SqliteConnectOptions, SqliteJournalMode, SqliteRow, SqliteSynchronous},
    Row, SqlitePool,
};
use std::{collections::HashMap, sync::{Arc, Mutex}};
use std::{
    fs::{File, OpenOptions},
    io::Write,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    str::FromStr,
};
use tokio::sync::RwLock;

const PROMPT_SLOT_NAME_MAX: usize = 128;
const PROMPT_SLOT_CONTENT_MAX: usize = 16 * 1024;
const PROMPT_SLOT_COUNT_MAX: usize = 32;
const PROMPT_SLOT_TOTAL_MAX: usize = 64 * 1024;

#[derive(Clone)]
pub struct Store {
    pub app_pool: SqlitePool,
    pub pool: SqlitePool,
    pub agent_pool: SqlitePool,
    pub(crate) root: PathBuf,
    user_id: String,
    agent_id: String,
    user_states: Arc<RwLock<HashMap<String, UserState>>>,
    agent_states: Arc<RwLock<HashMap<String, SqlitePool>>>,
    cache_budgets: Arc<Mutex<(usize,usize)>>,
    cipher: Aes256Gcm,
    cache: Arc<Mutex<Cache>>,
    context_cache: Arc<Mutex<ContextCache>>,
    context_gate: Arc<RwLock<()>>,
}

#[derive(Clone)]
struct UserState {
    pool: SqlitePool,
    cache: Arc<Mutex<Cache>>,
    context_cache: Arc<Mutex<ContextCache>>,
    context_gate: Arc<RwLock<()>>,
}
impl UserState {
    fn new(pool: SqlitePool) -> Self {
        Self {pool, cache:Arc::new(Mutex::new(Cache::new(64*1024*1024))),
            context_cache:Arc::new(Mutex::new(ContextCache::new(16*1024*1024))),
            context_gate:Arc::new(RwLock::new(()))}
    }
}

fn load_or_create_key(root: &Path) -> Result<Vec<u8>> {
    let keypath = root.join("encryption.key");
    let key = match std::fs::read(&keypath) {
        Ok(key) => key,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            if [
                "runtime.sqlite",
                "runtime.sqlite-wal",
                "runtime.sqlite-shm",
                "db/app.db",
                "user_data/admin/admin.db",
                "agent_data/shared_default/shared_default.db",
            ]
            .iter()
            .any(|name| root.join(name).exists())
            {
                return Err(anyhow!(
                    "Storage encryption key is missing for existing database"
                ));
            }
            let mut key = vec![0; 32];
            rand::thread_rng().fill_bytes(&mut key);
            let temp_path = root.join(format!(".encryption.key.{}.tmp", uuid::Uuid::new_v4()));
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&temp_path)?;
            let published = (|| -> Result<bool> {
                file.write_all(&key)?;
                file.sync_all()?;
                match std::fs::hard_link(&temp_path, &keypath) {
                    Ok(()) => {
                        File::open(root)?.sync_all()?;
                        Ok(true)
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
                    Err(e) => Err(e.into()),
                }
            })();
            drop(file);
            let _ = std::fs::remove_file(&temp_path);
            if published? {
                key
            } else {
                std::fs::read(&keypath)?
            }
        }
        Err(e) => return Err(e.into()),
    };
    if key.len() != 32 {
        return Err(anyhow!("Invalid storage encryption key"));
    }
    Ok(key)
}

pub(crate) async fn open_pool(path: &Path, events: bool, runs: bool) -> Result<SqlitePool> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("Database path has no parent"))?;
    std::fs::create_dir_all(parent)?;
    std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))?;
    let options = SqliteConnectOptions::from_str(&format!("sqlite://{}", path.display()))?
        .create_if_missing(true)
        .foreign_keys(true)
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Full)
        .busy_timeout(std::time::Duration::from_secs(5));
    let pool = sqlx::sqlite::SqlitePoolOptions::new()
        .max_connections(4)
        .connect_with(options)
        .await?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    sqlx::query("CREATE TABLE IF NOT EXISTS docs (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
        .execute(&pool)
        .await?;
    if events {
        sqlx::raw_sql("CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, kind TEXT NOT NULL, payload BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id,seq);
            CREATE TABLE IF NOT EXISTS legacy_event_map(legacy_seq INTEGER PRIMARY KEY,new_seq INTEGER NOT NULL UNIQUE);")
            .execute(&pool).await?;
    }
    if events && !runs {
        sqlx::raw_sql("CREATE TABLE IF NOT EXISTS loop_heartbeat(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),owner TEXT NOT NULL,
            protocol_version INTEGER NOT NULL CHECK(protocol_version=1),updated_at INTEGER NOT NULL)")
            .execute(&pool).await?;
    }
    if runs {
        sqlx::raw_sql("CREATE TABLE IF NOT EXISTS run_index(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,status TEXT NOT NULL,updated_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS runs_status ON run_index(status,updated_at);
            CREATE INDEX IF NOT EXISTS runs_session ON run_index(session_id,updated_at);
            CREATE TABLE IF NOT EXISTS interactions(seq INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,run_id TEXT NOT NULL,step TEXT NOT NULL,item BLOB NOT NULL,created_at INTEGER NOT NULL,UNIQUE(run_id,step));
            CREATE INDEX IF NOT EXISTS interactions_session ON interactions(session_id,seq);
            CREATE TABLE IF NOT EXISTS chat(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,session_id TEXT NOT NULL,run_id TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN (\'user\',\'assistant\')),content BLOB NOT NULL,created_at INTEGER NOT NULL,terminal_run_id TEXT UNIQUE);
            CREATE INDEX IF NOT EXISTS chat_session ON chat(session_id,seq);
            CREATE TABLE IF NOT EXISTS loop_commands(seq INTEGER PRIMARY KEY AUTOINCREMENT,command_id TEXT NOT NULL UNIQUE,run_id TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version=1),kind TEXT NOT NULL,payload BLOB NOT NULL,created_at INTEGER NOT NULL,acknowledged_at INTEGER);
            CREATE INDEX IF NOT EXISTS loop_commands_run ON loop_commands(run_id,seq);
            CREATE TABLE IF NOT EXISTS loop_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version=1),kind TEXT NOT NULL,payload BLOB NOT NULL,created_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS loop_events_run ON loop_events(run_id,seq);
            CREATE TABLE IF NOT EXISTS publication_outbox(run_id TEXT PRIMARY KEY,schema_version INTEGER NOT NULL CHECK(schema_version=1),payload BLOB NOT NULL,created_at INTEGER NOT NULL);")
            .execute(&pool).await?;
        let columns = sqlx::query("PRAGMA table_info(run_index)").fetch_all(&pool).await?;
        if !columns.iter().any(|r| r.get::<String,_>("name") == "enqueue_seq") {
            sqlx::query("ALTER TABLE run_index ADD COLUMN enqueue_seq INTEGER").execute(&pool).await?;
        }
        sqlx::query("UPDATE run_index SET enqueue_seq=rowid WHERE enqueue_seq IS NULL").execute(&pool).await?;
        for (name, declaration) in [
            ("lease_epoch", "INTEGER NOT NULL DEFAULT 0"),
            ("lease_owner", "TEXT"),
            ("lease_expires_at", "INTEGER NOT NULL DEFAULT 0"),
        ] {
            if !columns.iter().any(|r| r.get::<String,_>("name") == name) {
                sqlx::query(&format!("ALTER TABLE run_index ADD COLUMN {name} {declaration}")).execute(&pool).await?;
            }
        }
    }
    Ok(pool)
}

impl Store {
    pub async fn open(root: &Path) -> Result<Self> {
        std::fs::create_dir_all(root)?;
        let key = load_or_create_key(root)?;
        // Keep the administrator's original data path during migration.
        let admin_state = UserState::new(open_pool(&root.join("user_data/admin/admin.db"),true,true).await?);
        let mut user_states = HashMap::new();
        user_states.insert("admin".to_owned(),admin_state.clone());
        let shared_agent_pool = open_pool(&root.join("agent_data/shared_default/shared_default.db"),false,false).await?;
        let mut agent_states = HashMap::new();
        agent_states.insert("shared_default".to_owned(),shared_agent_pool.clone());
        let store = Self {
            app_pool: open_pool(&root.join("db/app.db"), true, false).await?,
            pool: admin_state.pool.clone(),
            agent_pool: shared_agent_pool,
            root: root.to_owned(),
            user_id: "admin".to_owned(),
            agent_id: "shared_default".to_owned(),
            user_states: Arc::new(RwLock::new(user_states)),
            agent_states: Arc::new(RwLock::new(agent_states)),
            cache_budgets: Arc::new(Mutex::new((67108864,16777216))),
            cipher: Aes256Gcm::new_from_slice(&key)
                .map_err(|_| anyhow!("Invalid encryption key"))?,
            cache: admin_state.cache,
            context_cache: admin_state.context_cache,
            context_gate: admin_state.context_gate,
        };
        sqlx::raw_sql(
            "CREATE TABLE IF NOT EXISTS prompt_slots (
                slot_name TEXT PRIMARY KEY, order_index INTEGER NOT NULL,
                locked INTEGER NOT NULL CHECK(locked IN (0,1)),
                merge_mode TEXT NOT NULL CHECK(merge_mode IN ('replace','append')),
                content BLOB NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0),
                updated_at INTEGER NOT NULL);
             CREATE INDEX IF NOT EXISTS prompt_slots_order ON prompt_slots(order_index,slot_name);",
        )
        .execute(&store.agent_pool)
        .await?;
        store.migrate_legacy(root).await?;
        store.init_accounts().await?;
        store.init_agent_tables().await?;
        store.ensure_default_agent_directory().await?;
        Ok(store)
    }
    pub async fn for_user(&self, user_id:&str) -> Result<Self> {
        if !access::valid_id(user_id) {return Err(anyhow!("Invalid account identity"));}
        if self.account_by_id(user_id).await?.is_none() {return Err(anyhow!("Account not found"));}
        let existing = self.user_states.read().await.get(user_id).cloned();
        let state = match existing {Some(state)=>state,None=>{
            let mut states=self.user_states.write().await;
            if let Some(state)=states.get(user_id) {state.clone()} else {
                let path=self.root.join("user_data").join(user_id).join(format!("{user_id}.db"));
                let mut state=UserState::new(open_pool(&path,true,true).await?);
                let (documents,contexts)=*self.cache_budgets.lock().unwrap();
                state.cache=Arc::new(Mutex::new(Cache::new(documents)));
                state.context_cache=Arc::new(Mutex::new(ContextCache::new(contexts)));
                states.insert(user_id.to_owned(),state.clone()); state
            }
        }};
        Ok(Self {app_pool:self.app_pool.clone(),pool:state.pool,
            agent_pool:self.agent_pool.clone(),root:self.root.clone(),
            user_id:user_id.to_owned(),agent_id:self.agent_id.clone(),
            user_states:self.user_states.clone(),agent_states:self.agent_states.clone(),cache_budgets:self.cache_budgets.clone(),
            cipher:self.cipher.clone(),cache:state.cache,
            context_cache:state.context_cache,context_gate:state.context_gate})
    }
    pub async fn for_agent(&self, agent_id:&str) -> Result<Self> {
        if !access::valid_agent_id(agent_id) {
            return Err(anyhow!("Invalid agent identity"));
        }
        if agent_id!="shared_default" {
            let found:Option<String>=sqlx::query_scalar(
                "SELECT id FROM agent_directory WHERE id=?")
                .bind(agent_id).fetch_optional(&self.app_pool).await?;
            if found.is_none() {return Err(anyhow!("Agent not found"));}
        }
        let existing=self.agent_states.read().await.get(agent_id).cloned();
        let pool=match existing {Some(pool)=>pool,None=>{
            let mut states=self.agent_states.write().await;
            if let Some(pool)=states.get(agent_id) {pool.clone()} else {
                let pool=crate::agent_storage::open_agent_pool(&self.root,agent_id).await?;
                states.insert(agent_id.to_owned(),pool.clone()); pool
            }
        }};
        Ok(Self {app_pool:self.app_pool.clone(),pool:self.pool.clone(),
            agent_pool:pool,root:self.root.clone(),
            user_id:self.user_id.clone(),agent_id:agent_id.to_owned(),
            user_states:self.user_states.clone(),agent_states:self.agent_states.clone(),
            cache_budgets:self.cache_budgets.clone(),cipher:self.cipher.clone(),
            cache:self.cache.clone(),context_cache:self.context_cache.clone(),
            context_gate:self.context_gate.clone()})
    }
    pub fn current_agent_id(&self)->&str {&self.agent_id}
    pub fn current_user_id(&self)->&str {&self.user_id}
    async fn init_agent_tables(&self) -> Result<()> {
        sqlx::raw_sql("CREATE TABLE IF NOT EXISTS agents(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
            metadata BLOB NOT NULL, revision INTEGER NOT NULL,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_connections(
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
            connection_type TEXT NOT NULL, section TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), config BLOB NOT NULL,
            revision INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS agent_connections_agent ON agent_connections(agent_id,section);
            CREATE TABLE IF NOT EXISTS agent_prompts(
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
            slot_name TEXT NOT NULL, order_index INTEGER NOT NULL,
            lock INTEGER NOT NULL CHECK(lock IN (0,1)),
            merge_mode TEXT NOT NULL CHECK(merge_mode IN ('replace','append')),
            content BLOB NOT NULL, revision INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            UNIQUE(agent_id,slot_name));
            CREATE INDEX IF NOT EXISTS agent_prompts_order ON agent_prompts(agent_id,order_index,slot_name);")
            .execute(&self.agent_pool).await?;
        let mut tx = self.agent_pool.begin().await?;
        let exists: Option<String> = sqlx::query_scalar("SELECT id FROM agents WHERE id='shared_default'")
            .fetch_optional(&mut *tx).await?;
        if exists.is_none() {
            let model_blob: Option<Vec<u8>> = sqlx::query_scalar("SELECT value FROM docs WHERE key='agent:shared_default:model'")
                .fetch_optional(&mut *tx).await?;
            let old_model = model_blob.map(|blob| self.unseal(&blob)).transpose()?
                .unwrap_or(json!({"model":"gpt-6-luna"}));
            let ability_blob: Option<Vec<u8>> = sqlx::query_scalar("SELECT value FROM docs WHERE key='agent_abilities:shared_default'")
                .fetch_optional(&mut *tx).await?;
            let old_abilities = ability_blob.map(|blob| self.unseal(&blob)).transpose()?
                .unwrap_or(json!({"revision":0,"visibility":{},"skill_visibility":{}}));
            let mut modes = serde_json::Map::new();
            if let Some(old) = old_abilities["visibility"].as_object() {
                for (id, mode) in old { if mode != "off" { modes.insert(id.clone(),mode.clone()); } }
            }
            let metadata = json!({"discovery_default":"discoverable","ability_modes":modes,
                "skill_modes":old_abilities["skill_visibility"],"tool_modes":{},"ability_access":{},
                "ability_revision":old_abilities["revision"].as_i64().unwrap_or(0).max(0)});
            let now: i64 = sqlx::query_scalar("SELECT CAST(strftime('%s','now') AS INTEGER)")
                .fetch_one(&mut *tx).await?;
            sqlx::query("INSERT INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)")
                .bind("shared_default").bind("WebAgent").bind("")
                .bind(old_model["provider"].as_str().unwrap_or("openai_subscription"))
                .bind(old_model["model"].as_str().unwrap_or("gpt-6-luna"))
                .bind("active").bind(self.seal(&metadata)?)
                .bind(old_abilities["revision"].as_i64().unwrap_or(0).max(0))
                .bind(now).bind(now).execute(&mut *tx).await?;
            if let Some(old) = old_abilities["visibility"].as_object() {
                for (id, mode) in old {
                    sqlx::query("INSERT INTO agent_connections(id,agent_id,connection_type,section,enabled,config,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)")
                        .bind(format!("ability:{id}")).bind("shared_default").bind(id).bind("ability")
                        .bind((mode != "off") as i64).bind(self.seal(&json!({}))?)
                        .bind(1_i64).bind(now).bind(now).execute(&mut *tx).await?;
                }
            }
            sqlx::query("INSERT INTO agent_prompts(id,agent_id,slot_name,order_index,lock,merge_mode,content,revision,updated_at)
                SELECT 'shared_default:'||slot_name,'shared_default',slot_name,order_index,locked,merge_mode,content,revision,updated_at FROM prompt_slots")
                .execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(())
    }
    pub async fn ensure_agent_connections(&self, ability_ids: &[String]) -> Result<()> {
        let mut tx = self.agent_pool.begin().await?;
        for id in ability_ids {
            sqlx::query("INSERT OR IGNORE INTO agent_connections(
                id,agent_id,connection_type,section,enabled,config,revision,created_at,updated_at)
                VALUES(?,?,?,?,1,?,1,strftime('%s','now'),strftime('%s','now'))")
                .bind(format!("ability:{id}")).bind(&self.agent_id).bind(id).bind("ability")
                .bind(self.seal(&json!({}))?).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(())
    }
    pub async fn agent_model(&self) -> Result<Value> {
        let row = sqlx::query("SELECT provider,model FROM agents WHERE id=?")
            .bind(&self.agent_id).fetch_one(&self.agent_pool).await?;
        Ok(json!({"provider":row.get::<String,_>("provider"),"model":row.get::<String,_>("model")}))
    }
    pub async fn save_agent_model(&self, value: &Value) -> Result<()> {
        sqlx::query("UPDATE agents SET provider=?,model=?,revision=revision+1,updated_at=strftime('%s','now') WHERE id=?")
            .bind(value["provider"].as_str().ok_or_else(||anyhow!("Missing provider"))?)
            .bind(value["model"].as_str().ok_or_else(||anyhow!("Missing model"))?)
            .bind(&self.agent_id)
            .execute(&self.agent_pool).await?;
        Ok(())
    }
    pub async fn agent_abilities(&self) -> Result<Value> {
        self.agent_abilities_for(&self.agent_id).await
    }
    pub async fn agent_abilities_for(&self, agent_id: &str) -> Result<Value> {
        if agent_id != self.agent_id {return Err(anyhow!("Agent view mismatch"));}
        let row = sqlx::query("SELECT metadata,revision FROM agents WHERE id=?")
            .bind(agent_id).fetch_one(&self.agent_pool).await?;
        let metadata = self.unseal(&row.get::<Vec<u8>,_>("metadata"))?;
        let mut visibility = metadata["ability_modes"].as_object().cloned().unwrap_or_default();
        let rows = sqlx::query("SELECT connection_type,enabled FROM agent_connections WHERE agent_id=? AND section='ability'")
            .bind(agent_id).fetch_all(&self.agent_pool).await?;
        for conn in rows {
            if conn.get::<i64,_>("enabled")==0 {
                visibility.insert(conn.get::<String,_>("connection_type"),json!("off"));
            }
        }
        Ok(json!({"revision":metadata["ability_revision"].as_i64().unwrap_or(0),"visibility":visibility,
            "skill_visibility":metadata["skill_modes"],"tool_modes":metadata["tool_modes"]}))
    }
    pub async fn save_agent_abilities(&self, value: &Value, expected_revision: u64) -> Result<()> {
        let mut tx = self.agent_pool.begin().await?;
        let row = sqlx::query("SELECT metadata,revision FROM agents WHERE id=?")
            .bind(&self.agent_id).fetch_one(&mut *tx).await?;
        let mut metadata = self.unseal(&row.get::<Vec<u8>,_>("metadata"))?;
        if metadata["ability_revision"].as_u64().unwrap_or(0) != expected_revision {
            return Err(anyhow!("Agent ability revision changed"));
        }
        let modes = value["visibility"].as_object().ok_or_else(||anyhow!("Invalid ability modes"))?;
        let mut active = serde_json::Map::new();
        for (id,mode) in modes {
            if mode != "off" { active.insert(id.clone(),mode.clone()); }
            sqlx::query("INSERT INTO agent_connections(id,agent_id,connection_type,section,enabled,config,revision,created_at,updated_at)
                VALUES(?,?,?,?,?,?,1,strftime('%s','now'),strftime('%s','now'))
                ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,revision=agent_connections.revision+1,updated_at=excluded.updated_at")
                .bind(format!("ability:{id}")).bind(&self.agent_id).bind(id).bind("ability")
                .bind((mode != "off") as i64).bind(self.seal(&json!({}))?)
                .execute(&mut *tx).await?;
        }
        metadata["ability_modes"]=Value::Object(active);
        metadata["skill_modes"]=value["skill_visibility"].clone();
        metadata["tool_modes"]=value["tool_modes"].clone();
        metadata["ability_revision"]=json!(expected_revision + 1);
        sqlx::query("UPDATE agents SET metadata=?,revision=revision+1,updated_at=strftime('%s','now') WHERE id=? AND revision=?")
            .bind(self.seal(&metadata)?).bind(&self.agent_id)
            .bind(row.get::<i64,_>("revision"))
            .execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }
    fn validate_agent_doc_key(&self,key:&str) -> Result<()> {
        if let Some(rest)=key.strip_prefix("agent:") {
            if rest!=self.agent_id && !rest.starts_with(&format!("{}:",self.agent_id)) {
                return Err(anyhow!("Agent view mismatch"));
            }
        }
        if let Some(rest)=key.strip_prefix("agent_abilities:") {
            if rest!=self.agent_id {
                return Err(anyhow!("Agent view mismatch"));
            }
        }
        Ok(())
    }
    fn pool_for(&self, key: &str) -> &SqlitePool {
        if key.starts_with("agent:") || key.starts_with("agent_abilities:") {
            &self.agent_pool
        } else if key.starts_with("session:")
            || key.starts_with("session_mode:")
            || key.starts_with("session_ability_state:")
            || key.starts_with("run:")
            || key.starts_with("run_view:")
            || key.starts_with("attachment:")
            || key.starts_with("memory:")
            || key.starts_with("promotion:")
            || key.starts_with("receipt:session:")
            || key.starts_with("receipt:message:")
            || key.starts_with("receipt:run:")
            || key.starts_with("receipt:approval:")
        {
            &self.pool
        } else {
            &self.app_pool
        }
    }
    fn validate_prompt_slot_name(name: &str) -> Result<()> {
        if name.is_empty()
            || name.len() > PROMPT_SLOT_NAME_MAX
            || name.trim() != name
            || name.chars().any(char::is_control)
            || name == "__skills__"
        {
            return Err(anyhow!("Invalid prompt slot name"));
        }
        Ok(())
    }
    fn prompt_slot_from_row(&self, row: &SqliteRow) -> Result<Value> {
        let content = self.unseal(&row.get::<Vec<u8>, _>("content"))?;
        let content = content
            .as_str()
            .ok_or_else(|| anyhow!("Invalid encrypted prompt slot content"))?;
        Ok(json!({
            "slot_name": row.get::<String, _>("slot_name"),
            "order_index": row.get::<i64, _>("order_index"),
            "lock": row.get::<i64, _>("locked") != 0,
            "merge_mode": row.get::<String, _>("merge_mode"),
            "content": content,
            "revision": row.get::<i64, _>("revision"),
            "updated_at": row.get::<i64, _>("updated_at")
        }))
    }
    pub async fn list_prompt_slots(&self) -> Result<Vec<Value>> {
        self.list_agent_prompt_slots(&self.agent_id).await
    }
    pub async fn list_agent_prompt_slots(&self, agent_id: &str) -> Result<Vec<Value>> {
        if agent_id != self.agent_id {return Err(anyhow!("Agent view mismatch"));}
        let rows = sqlx::query(
            "SELECT slot_name,order_index,lock AS locked,merge_mode,content,revision,updated_at
             FROM agent_prompts WHERE agent_id=? ORDER BY order_index,slot_name",
        )
        .bind(agent_id)
        .fetch_all(&self.agent_pool)
        .await?;
        if rows.len() > PROMPT_SLOT_COUNT_MAX {
            return Err(anyhow!("Prompt slot count exceeds limit"));
        }
        let mut total = 0usize;
        let mut slots = Vec::with_capacity(rows.len());
        for row in &rows {
            let slot = self.prompt_slot_from_row(row)?;
            let length = slot["content"].as_str().unwrap().len();
            if length > PROMPT_SLOT_CONTENT_MAX {
                return Err(anyhow!("Prompt slot content exceeds limit"));
            }
            total = total
                .checked_add(length)
                .ok_or_else(|| anyhow!("Prompt slot content total exceeds limit"))?;
            if total > PROMPT_SLOT_TOTAL_MAX {
                return Err(anyhow!("Prompt slot content total exceeds limit"));
            }
            slots.push(slot);
        }
        Ok(slots)
    }
    pub async fn get_prompt_slot(&self, name: &str) -> Result<Option<Value>> {
        Self::validate_prompt_slot_name(name)?;
        let row = sqlx::query(
            "SELECT slot_name,order_index,lock AS locked,merge_mode,content,revision,updated_at
             FROM agent_prompts WHERE agent_id=? AND slot_name=?",
        )
        .bind(&self.agent_id).bind(name)
        .fetch_optional(&self.agent_pool)
        .await?;
        row.as_ref()
            .map(|row| self.prompt_slot_from_row(row))
            .transpose()
    }
    pub async fn upsert_prompt_slot(
        &self,
        name: &str,
        content: &str,
        order_index: Option<i64>,
        lock: Option<bool>,
        merge_mode: Option<&str>,
        expected_revision: i64,
    ) -> Result<Value> {
        Self::validate_prompt_slot_name(name)?;
        if content.len() > PROMPT_SLOT_CONTENT_MAX {
            return Err(anyhow!("Prompt slot content exceeds limit"));
        }
        if expected_revision < 0 {
            return Err(anyhow!("Invalid prompt slot revision"));
        }
        if order_index.is_some_and(|value| !(0..=1_000_000).contains(&value)) {
            return Err(anyhow!("Invalid prompt slot order"));
        }
        if merge_mode.is_some_and(|value| value != "replace" && value != "append") {
            return Err(anyhow!("Invalid prompt slot merge mode"));
        }
        let mut tx = self.agent_pool.begin().await?;
        // Reserve the writer before capacity reads; concurrent creates cannot
        // both pass the count and assembled-size checks.
        sqlx::query("UPDATE agent_prompts SET revision=revision WHERE 0")
            .execute(&mut *tx)
            .await?;
        let rows = sqlx::query(
            "SELECT slot_name,order_index,lock AS locked,merge_mode,content,revision,updated_at
             FROM agent_prompts WHERE agent_id=?",
        )
        .bind(&self.agent_id).fetch_all(&mut *tx)
        .await?;
        let existing = rows
            .iter()
            .find(|row| row.get::<String, _>("slot_name") == name);
        match (existing, expected_revision) {
            (None, 0) => {}
            (None, _) => return Err(anyhow!("Prompt slot not found")),
            (Some(row), revision) if revision > 0 && row.get::<i64, _>("revision") == revision => {}
            _ => return Err(anyhow!("Prompt slot revision mismatch")),
        }
        if existing.is_none() && rows.len() >= PROMPT_SLOT_COUNT_MAX {
            return Err(anyhow!("Prompt slot count exceeds limit"));
        }
        let mut total = content.len();
        for row in &rows {
            if row.get::<String, _>("slot_name") == name {
                continue;
            }
            let old = self.prompt_slot_from_row(row)?;
            let length = old["content"].as_str().unwrap().len();
            if length > PROMPT_SLOT_CONTENT_MAX {
                return Err(anyhow!("Prompt slot content exceeds limit"));
            }
            total = total
                .checked_add(length)
                .ok_or_else(|| anyhow!("Prompt slot content total exceeds limit"))?;
        }
        if total > PROMPT_SLOT_TOTAL_MAX {
            return Err(anyhow!("Prompt slot content total exceeds limit"));
        }
        let order = match order_index {
            Some(order) => order,
            None => match existing {
                Some(row) => row.get::<i64, _>("order_index"),
                None => rows
                    .iter()
                    .map(|row| row.get::<i64, _>("order_index"))
                    .max()
                    .unwrap_or(0)
                    .checked_add(10)
                    .ok_or_else(|| anyhow!("Prompt slot order exceeds limit"))?,
            },
        };
        if !(0..=1_000_000).contains(&order) {
            return Err(anyhow!("Invalid prompt slot order"));
        }
        let locked = lock
            .or_else(|| existing.map(|row| row.get::<i64, _>("locked") != 0))
            .unwrap_or(false);
        let mode = merge_mode
            .map(str::to_owned)
            .or_else(|| existing.map(|row| row.get::<String, _>("merge_mode")))
            .unwrap_or_else(|| "replace".to_owned());
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_secs() as i64;
        let sealed = self.seal(&json!(content))?;
        let revision = expected_revision
            .checked_add(1)
            .ok_or_else(|| anyhow!("Invalid prompt slot revision"))?;
        if existing.is_some() {
            let changed = sqlx::query(
                "UPDATE agent_prompts SET order_index=?,lock=?,merge_mode=?,content=?,revision=?,updated_at=?
                 WHERE agent_id=? AND slot_name=? AND revision=?",
            )
            .bind(order).bind(locked as i64).bind(&mode).bind(sealed)
            .bind(revision).bind(now).bind(&self.agent_id).bind(name).bind(expected_revision)
            .execute(&mut *tx).await?.rows_affected();
            if changed != 1 {
                return Err(anyhow!("Prompt slot revision mismatch"));
            }
        } else {
            sqlx::query(
                "INSERT INTO agent_prompts(id,agent_id,slot_name,order_index,lock,merge_mode,content,revision,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?)",
            )
            .bind(format!("{}:{name}",self.agent_id)).bind(&self.agent_id).bind(name).bind(order).bind(locked as i64).bind(&mode).bind(sealed)
            .bind(revision).bind(now).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(json!({"slot_name":name,"order_index":order,"lock":locked,
            "merge_mode":mode,"content":content,"revision":revision,"updated_at":now}))
    }
    pub async fn delete_prompt_slot(&self, name: &str, expected_revision: i64) -> Result<bool> {
        Self::validate_prompt_slot_name(name)?;
        if expected_revision <= 0 {
            return Err(anyhow!("Invalid prompt slot revision"));
        }
        let mut tx = self.agent_pool.begin().await?;
        let actual: Option<i64> =
            sqlx::query_scalar("SELECT revision FROM agent_prompts WHERE agent_id=? AND slot_name=?")
                .bind(&self.agent_id).bind(name)
                .fetch_optional(&mut *tx)
                .await?;
        match actual {
            None => return Err(anyhow!("Prompt slot not found")),
            Some(revision) if revision != expected_revision => {
                return Err(anyhow!("Prompt slot revision mismatch"));
            }
            Some(_) => {}
        }
        let changed = sqlx::query("DELETE FROM agent_prompts WHERE agent_id=? AND slot_name=? AND revision=?")
            .bind(&self.agent_id).bind(name)
            .bind(expected_revision)
            .execute(&mut *tx)
            .await?
            .rows_affected();
        if changed != 1 {
            return Err(anyhow!("Prompt slot revision mismatch"));
        }
        tx.commit().await?;
        Ok(true)
    }
    async fn migrate_legacy_chat(&self, sid: &str, session: &Value) -> Result<()> {
        let Some(messages) = session["messages"].as_array() else {
            return Ok(());
        };
        for (ordinal, message) in messages.iter().enumerate() {
            let role = message["role"]
                .as_str()
                .ok_or_else(|| anyhow!("Legacy chat role is missing"))?;
            if role != "user" && role != "assistant" {
                return Err(anyhow!("Unsupported legacy chat role"));
            }
            let content = message
                .get("content")
                .ok_or_else(|| anyhow!("Legacy chat content is missing"))?;
            let id = format!("legacy:{sid}:{ordinal}");
            let run_id = message["run_id"].as_str().unwrap_or("");
            let created_at = message["created_at"]
                .as_i64()
                .or_else(|| session["created_at"].as_i64())
                .unwrap_or(0);
            sqlx::query("INSERT OR IGNORE INTO chat(id,session_id,run_id,role,content,created_at,terminal_run_id) VALUES(?,?,?,?,?,?,NULL)")
                .bind(&id).bind(sid).bind(run_id).bind(role).bind(self.seal(content)?)
                .bind(created_at).execute(&self.pool).await?;
            let row = sqlx::query(
                "SELECT session_id,run_id,role,content,created_at FROM chat WHERE id=?",
            )
            .bind(&id)
            .fetch_one(&self.pool)
            .await?;
            if row.get::<String, _>("session_id") != sid
                || row.get::<String, _>("run_id") != run_id
                || row.get::<String, _>("role") != role
                || row.get::<i64, _>("created_at") != created_at
                || self.unseal(&row.get::<Vec<u8>, _>("content"))? != *content
            {
                return Err(anyhow!("Conflicting migrated chat record"));
            }
        }
        Ok(())
    }
    async fn migrate_legacy_event(
        &self,
        destination: &SqlitePool,
        legacy_seq: i64,
        sid: &str,
        kind: &str,
        raw: &[u8],
        upgrading_v1: bool,
    ) -> Result<()> {
        let value = self.unseal(raw)?;
        if !upgrading_v1 {
            sqlx::query(
                "INSERT OR IGNORE INTO events(seq,session_id,kind,payload) VALUES(?,?,?,?)",
            )
            .bind(legacy_seq)
            .bind(sid)
            .bind(kind)
            .bind(raw)
            .execute(destination)
            .await?;
            let existing = sqlx::query("SELECT session_id,kind,payload FROM events WHERE seq=?")
                .bind(legacy_seq)
                .fetch_one(destination)
                .await?;
            if existing.get::<String, _>("session_id") != sid
                || existing.get::<String, _>("kind") != kind
                || self.unseal(&existing.get::<Vec<u8>, _>("payload"))? != value
            {
                return Err(anyhow!("Conflicting migrated event: {legacy_seq}"));
            }
            return Ok(());
        }
        let mut tx = destination.begin().await?;
        let mapped =
            sqlx::query_scalar::<_, i64>("SELECT new_seq FROM legacy_event_map WHERE legacy_seq=?")
                .bind(legacy_seq)
                .fetch_optional(&mut *tx)
                .await?;
        let new_seq = if let Some(seq) = mapped {
            seq
        } else {
            let sequence_claimed = sqlx::query_scalar::<_, i64>(
                "SELECT legacy_seq FROM legacy_event_map WHERE new_seq=?",
            )
            .bind(legacy_seq)
            .fetch_optional(&mut *tx)
            .await?
            .is_some();
            let same_seq = sqlx::query("SELECT session_id,kind,payload FROM events WHERE seq=?")
                .bind(legacy_seq)
                .fetch_optional(&mut *tx)
                .await?;
            let already_copied = if let Some(row) = same_seq {
                !sequence_claimed
                    && row.get::<String, _>("session_id") == sid
                    && row.get::<String, _>("kind") == kind
                    && self.unseal(&row.get::<Vec<u8>, _>("payload"))? == value
            } else {
                false
            };
            let seq = if already_copied {
                legacy_seq
            } else {
                sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?,?,?)")
                    .bind(sid)
                    .bind(kind)
                    .bind(raw)
                    .execute(&mut *tx)
                    .await?
                    .last_insert_rowid()
            };
            sqlx::query("INSERT INTO legacy_event_map(legacy_seq,new_seq) VALUES(?,?)")
                .bind(legacy_seq)
                .bind(seq)
                .execute(&mut *tx)
                .await?;
            seq
        };
        let existing = sqlx::query("SELECT session_id,kind,payload FROM events WHERE seq=?")
            .bind(new_seq)
            .fetch_one(&mut *tx)
            .await?;
        if existing.get::<String, _>("session_id") != sid
            || existing.get::<String, _>("kind") != kind
            || self.unseal(&existing.get::<Vec<u8>, _>("payload"))? != value
        {
            return Err(anyhow!("Conflicting migrated event mapping: {legacy_seq}"));
        }
        tx.commit().await?;
        Ok(())
    }
    async fn migrate_legacy(&self, root: &Path) -> Result<()> {
        let legacy_path: PathBuf = root.join("runtime.sqlite");
        let marker_version = self
            .get("storage:split_migrated")
            .await?
            .and_then(|marker| marker["version"].as_u64())
            .unwrap_or(0);
        if !legacy_path.exists() || marker_version >= 2 {
            return Ok(());
        }
        let upgrading_v1 = marker_version == 1;
        let legacy = sqlx::sqlite::SqlitePoolOptions::new()
            .max_connections(1)
            .connect_with(
                SqliteConnectOptions::from_str(&format!("sqlite://{}", legacy_path.display()))?
                    .read_only(true),
            )
            .await?;
        let result = async {
            // Each row is idempotent. The marker is written only after all three
            // authorities have received and verified their legacy records.
            let mut last_key: Option<String> = None;
            loop {
                let rows = sqlx::query("SELECT key,value FROM docs WHERE (? IS NULL OR key>?) ORDER BY key LIMIT 128")
                    .bind(&last_key).bind(&last_key).fetch_all(&legacy).await?;
                if rows.is_empty() { break; }
                for row in rows {
                    let key: String = row.get("key");
                    last_key = Some(key.clone());
                    if key == "storage:split_migrated" { continue; }
                    let raw: Vec<u8> = row.get("value");
                    let mut value = self.unseal(&raw)?;
                    if key.starts_with("session:") && value["checkpoint_input"].is_null() {
                        if let Some(context) = value["context"].as_array() {
                            value["checkpoint_input"] = json!(context);
                            value["checkpoint_through"] = json!(0);
                        }
                    }
                    let destination = self.pool_for(&key);
                    sqlx::query("INSERT OR IGNORE INTO docs(key,value) VALUES(?,?)")
                        .bind(&key).bind(self.seal(&value)?).execute(destination).await?;
                    let existing: Vec<u8> = sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
                        .bind(&key).fetch_one(destination).await?;
                    if self.unseal(&existing)? != value {
                        if upgrading_v1 && (std::ptr::eq(destination, &self.app_pool)
                            || std::ptr::eq(destination, &self.agent_pool)) {
                            continue;
                        }
                        return Err(anyhow!("Conflicting migrated document: {key}"));
                    }
                    if key.starts_with("session:") {
                        let sid = key.strip_prefix("session:").unwrap();
                        self.migrate_legacy_chat(sid, &value).await?;
                    }
                }
            }
            let mut last_seq = 0i64;
            loop {
                let rows = sqlx::query("SELECT seq,session_id,kind,payload FROM events WHERE seq>? ORDER BY seq LIMIT 128")
                    .bind(last_seq).fetch_all(&legacy).await?;
                if rows.is_empty() { break; }
                for row in rows {
                    let seq: i64 = row.get("seq");
                    let sid: String = row.get("session_id");
                    let kind: String = row.get("kind");
                    let raw: Vec<u8> = row.get("payload");
                    self.unseal(&raw)?;
                    last_seq = seq;
                    let destination = if sid.is_empty() { &self.app_pool } else { &self.pool };
                    self.migrate_legacy_event(destination, seq, &sid, &kind, &raw, upgrading_v1).await?;
                }
            }
            let mut last_id: Option<String> = None;
            loop {
                let rows = sqlx::query("SELECT id,session_id,status,updated_at FROM run_index WHERE (? IS NULL OR id>?) ORDER BY id LIMIT 128")
                    .bind(&last_id).bind(&last_id).fetch_all(&legacy).await?;
                if rows.is_empty() { break; }
                for row in rows {
                    let id: String = row.get("id");
                    let sid: String = row.get("session_id");
                    let status: String = row.get("status");
                    let updated_at: i64 = row.get("updated_at");
                    last_id = Some(id.clone());
                    sqlx::query("INSERT OR IGNORE INTO run_index(id,session_id,status,updated_at) VALUES(?,?,?,?)")
                        .bind(&id).bind(&sid).bind(&status).bind(updated_at).execute(&self.pool).await?;
                    let existing = sqlx::query("SELECT session_id,status,updated_at FROM run_index WHERE id=?")
                        .bind(&id).fetch_one(&self.pool).await?;
                    if existing.get::<String,_>("session_id") != sid
                        || existing.get::<String,_>("status") != status
                        || existing.get::<i64,_>("updated_at") != updated_at
                    {
                        return Err(anyhow!("Conflicting migrated run index: {id}"));
                    }
                }
            }
            self.put("storage:split_migrated", &json!({"version":2})).await
        }.await;
        legacy.close().await;
        result
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
    pub fn configure_cache(&self, documents: usize, contexts: usize) {
        *self.cache_budgets.lock().unwrap()=(documents,contexts);
        *self.cache.lock().unwrap() = Cache::new(documents);
        *self.context_cache.lock().unwrap() = ContextCache::new(contexts);
    }
    pub async fn context(&self, sid: &str) -> Result<Vec<Value>> {
        // Checkpoint writers hold the exclusive side of this gate through commit
        // and cache invalidation, so a reader never combines two checkpoints.
        let _guard = self.context_gate.read().await;
        let session = self
            .get(&format!("session:{sid}"))
            .await?
            .ok_or_else(|| anyhow!("Session not found"))?;
        let through = session["checkpoint_through"].as_i64().unwrap_or(0);
        let cached = self.context_cache.lock().unwrap().get(sid, through);
        let (mut input, mut cursor) = cached.unwrap_or_else(|| {
            (
                session["checkpoint_input"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default(),
                through,
            )
        });
        let rows = sqlx::query(
            "SELECT seq,item FROM interactions WHERE session_id=? AND seq>? ORDER BY seq",
        )
        .bind(sid)
        .bind(cursor)
        .fetch_all(&self.pool)
        .await?;
        for row in rows {
            input.push(self.unseal(&row.get::<Vec<u8>, _>("item"))?);
            cursor = row.get::<i64, _>("seq");
        }
        self.context_cache
            .lock()
            .unwrap()
            .insert(sid.to_owned(), through, cursor, input.clone());
        Ok(input)
    }
    pub async fn chat_snapshot(&self, sid: &str) -> Result<(Vec<Value>, i64)> {
        let mut tx = self.pool.begin().await?;
        let cursor = sqlx::query_scalar::<_, i64>(
            "SELECT COALESCE(MAX(seq),0) FROM chat WHERE session_id=?",
        )
        .bind(sid)
        .fetch_one(&mut *tx)
        .await?;
        let rows = sqlx::query("SELECT seq,id,run_id,role,content,created_at FROM chat WHERE session_id=? AND seq<=? ORDER BY seq")
            .bind(sid).bind(cursor).fetch_all(&mut *tx).await?;
        tx.commit().await?;
        let messages = rows
            .iter()
            .map(|row| {
                Ok(json!({
                    "seq":row.get::<i64,_>("seq"),"id":row.get::<String,_>("id"),
                    "run_id":row.get::<String,_>("run_id"),"role":row.get::<String,_>("role"),
                    "content":self.unseal(&row.get::<Vec<u8>,_>("content"))?,
                    "created_at":row.get::<i64,_>("created_at")
                }))
            })
            .collect::<Result<Vec<_>>>()?;
        Ok((messages, cursor))
    }
    pub async fn session_summary(&self, sid: &str) -> Result<Value> {
        let mut tx = self.pool.begin().await?;
        let row = sqlx::query("SELECT value FROM docs WHERE key=?")
            .bind(format!("session:{sid}"))
            .fetch_optional(&mut *tx)
            .await?
            .ok_or_else(|| anyhow!("Session not found"))?;
        let counts = sqlx::query(
            "SELECT COUNT(*) AS total, COALESCE(MAX(seq),0) AS cursor FROM chat WHERE session_id=?",
        )
        .bind(sid)
        .fetch_one(&mut *tx)
        .await?;
        tx.commit().await?;
        let session = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
        Ok(json!({
            "id": sid,
            "title": session["title"],
            "created_at": session["created_at"],
            "chat_count": counts.get::<i64, _>("total"),
            "chat_cursor": counts.get::<i64, _>("cursor"),
        }))
    }
    pub async fn chat_page(&self, sid: &str, after_seq: i64, limit: i64) -> Result<Value> {
        if after_seq < 0 || !(1..=50).contains(&limit) {
            return Err(anyhow!("Invalid chat page bounds"));
        }
        if self.get(&format!("session:{sid}")).await?.is_none() {
            return Err(anyhow!("Session not found"));
        }
        let rows = sqlx::query(
            "SELECT seq,id,run_id,role,content,created_at FROM chat WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
        )
        .bind(sid).bind(after_seq).bind(limit + 1)
        .fetch_all(&self.pool).await?;
        let has_more = rows.len() > limit as usize;
        let mut messages = Vec::with_capacity(rows.len().min(limit as usize));
        let mut next_seq = after_seq;
        for row in rows.into_iter().take(limit as usize) {
            next_seq = row.get::<i64, _>("seq");
            messages.push(json!({
                "seq": next_seq,
                "id": row.get::<String, _>("id"),
                "run_id": row.get::<String, _>("run_id"),
                "role": row.get::<String, _>("role"),
                "content": self.unseal(&row.get::<Vec<u8>, _>("content"))?,
                "created_at": row.get::<i64, _>("created_at"),
            }));
        }
        Ok(json!({"messages": messages, "next_seq": next_seq, "has_more": has_more}))
    }
    pub async fn run_summaries(&self, sid: &str, limit: i64) -> Result<Vec<Value>> {
        if !(1..=50).contains(&limit) {
            return Err(anyhow!("Invalid run summary limit"));
        }
        if self.get(&format!("session:{sid}")).await?.is_none() {
            return Err(anyhow!("Session not found"));
        }
        let rows = sqlx::query(
            "SELECT r.id,r.status,r.updated_at,d.value FROM run_index r JOIN docs d ON d.key='run_view:'||r.id WHERE r.session_id=? ORDER BY r.updated_at DESC,r.id DESC LIMIT ?",
        )
        .bind(sid).bind(limit).fetch_all(&self.pool).await?;
        rows.iter()
            .map(|row| {
                let view = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
                Ok(json!({
                    "id": row.get::<String, _>("id"),
                    "status": row.get::<String, _>("status"),
                    "error": view["error"],
                    "uncertain": view["uncertain"],
                    "turns": view["turns"],
                    "updated_at": row.get::<i64, _>("updated_at"),
                }))
            })
            .collect()
    }
    pub async fn chat_events(&self, sid: &str, after: i64) -> Result<Vec<Value>> {
        let rows = sqlx::query("SELECT seq,id,run_id,role,content,created_at FROM chat WHERE session_id=? AND seq>? ORDER BY seq LIMIT 200")
            .bind(sid).bind(after).fetch_all(&self.pool).await?;
        rows.iter()
            .map(|row| {
                Ok(json!({
                    "seq":row.get::<i64,_>("seq"),"id":row.get::<String,_>("id"),
                    "run_id":row.get::<String,_>("run_id"),"role":row.get::<String,_>("role"),
                    "content":self.unseal(&row.get::<Vec<u8>,_>("content"))?,
                    "created_at":row.get::<i64,_>("created_at")
                }))
            })
            .collect()
    }
    async fn insert_interaction(
        &self,
        tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
        sid: &str,
        run_id: &str,
        step: &str,
        item: &Value,
    ) -> Result<()> {
        sqlx::query("INSERT OR IGNORE INTO interactions(session_id,run_id,step,item,created_at) VALUES(?,?,?,?,strftime('%s','now'))")
            .bind(sid).bind(run_id).bind(step).bind(self.seal(item)?)
            .execute(&mut **tx).await?;
        Ok(())
    }
    async fn insert_chat(
        &self,
        tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
        sid: &str,
        run_id: &str,
        role: &str,
        content: &Value,
    ) -> Result<()> {
        let id = format!("{role}:{run_id}");
        let terminal = if role == "assistant" {
            Some(run_id)
        } else {
            None
        };
        sqlx::query("INSERT OR IGNORE INTO chat(id,session_id,run_id,role,content,created_at,terminal_run_id) VALUES(?,?,?,?,?,strftime('%s','now'),?)")
            .bind(id).bind(sid).bind(run_id).bind(role)
            .bind(self.seal(content)?).bind(terminal).execute(&mut **tx).await?;
        Ok(())
    }
    pub async fn get(&self, key: &str) -> Result<Option<Value>> {
        self.validate_agent_doc_key(key)?;
        // These documents are shared with the independent loop process.
        let cacheable = false;
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
            .fetch_optional(self.pool_for(key))
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
        self.validate_agent_doc_key(key)?;
        let session_id = key.strip_prefix("session:");
        let _context_guard = if session_id.is_some() {
            Some(self.context_gate.write().await)
        } else {
            None
        };
        let mut tx = self.pool_for(key).begin().await?;
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(key).bind(self.seal(value)?).execute(&mut *tx).await?;
        if key.starts_with("run:") {
            self.index_run(&mut tx, value).await?;
        }
        tx.commit().await?;
        self.cache.lock().unwrap().invalidate(&[key.into()]);
        if let Some(sid) = session_id {
            self.context_cache.lock().unwrap().invalidate(sid);
        }
        Ok(())
    }
    async fn index_run(
        &self,
        tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
        run: &Value,
    ) -> Result<()> {
        sqlx::query("INSERT INTO run_index(id,session_id,status,updated_at,enqueue_seq) VALUES(?,?,?,?,(SELECT COALESCE(MAX(enqueue_seq),0)+1 FROM run_index)) ON CONFLICT(id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,enqueue_seq=COALESCE(run_index.enqueue_seq,excluded.enqueue_seq)")
            .bind(run["id"].as_str().ok_or_else(||anyhow!("Invalid run"))?).bind(run["session_id"].as_str().unwrap_or(""))
            .bind(run["status"].as_str().unwrap_or("failed")).bind(run["updated_at"].as_i64().unwrap_or(0)).execute(&mut **tx).await?;
        let view = serde_json::json!({"id":run["id"],"session_id":run["session_id"],"status":run["status"],"error":run["error"],
            "pending":run["pending"],"uncertain":run["uncertain"],"turns":run["turns"],"phase":run["phase"],"updated_at":run["updated_at"]});
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(format!("run_view:{}",run["id"].as_str().unwrap())).bind(self.seal(&view)?).execute(&mut **tx).await?;
        Ok(())
    }
    pub async fn active_runs(&self) -> Result<Vec<Value>> {
        let rows=sqlx::query("SELECT d.value FROM run_index r JOIN docs d ON d.key='run_view:'||r.id WHERE r.status NOT IN ('completed','cancelled','failed') ORDER BY COALESCE(r.enqueue_seq,r.rowid) LIMIT 200")
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
        self.validate_agent_doc_key(key)?;
        let session_id = key.strip_prefix("session:");
        let _context_guard = if session_id.is_some() {
            Some(self.context_gate.write().await)
        } else {
            None
        };
        sqlx::query("DELETE FROM docs WHERE key=?")
            .bind(key)
            .execute(self.pool_for(key))
            .await?;
        self.cache.lock().unwrap().invalidate(&[key.into()]);
        if let Some(sid) = session_id {
            self.context_cache.lock().unwrap().invalidate(sid);
        }
        Ok(())
    }
    pub async fn list(&self, prefix: &str) -> Result<Vec<Value>> {
        self.validate_agent_doc_key(prefix)?;
        let rows = sqlx::query("SELECT value FROM docs WHERE key LIKE ? ORDER BY key")
            .bind(format!("{prefix}%"))
            .fetch_all(self.pool_for(prefix))
            .await?;
        rows.iter()
            .map(|r| self.unseal(r.get::<Vec<u8>, _>("value").as_slice()))
            .collect()
    }
    pub async fn event(&self, sid: &str, kind: &str, payload: &Value) -> Result<()> {
        let destination = if sid.is_empty() {
            &self.app_pool
        } else {
            &self.pool
        };
        sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?,?,?)")
            .bind(sid)
            .bind(kind)
            .bind(self.seal(payload)?)
            .execute(destination)
            .await?;
        Ok(())
    }
    // Save one checkpoint only if the caller still has the revision and, for
    // loop execution, the live lease it read. The override below is reserved
    // for deliberate recovery or access shutdown and still checks revision.
    pub async fn save_run(&self, run: &mut Value, kind: &str, payload: &Value) -> Result<()> {
        self.save_run_inner(run,kind,payload,false).await
    }
    pub async fn save_run_override(&self, run:&mut Value, kind:&str, payload:&Value) -> Result<()> {
        self.save_run_inner(run,kind,payload,true).await
    }
    async fn save_run_inner(&self, run:&mut Value, kind:&str, payload:&Value,
        allow_expired:bool) -> Result<()> {
        let rid = run["id"].as_str().ok_or_else(||anyhow!("Run has no id"))?.to_owned();
        let _context_guard = if kind == "context_compacted" {
            Some(self.context_gate.write().await)
        } else {
            None
        };
        let mut tx = self.pool.begin().await?;
        let expected=run["run_revision"].as_u64().unwrap_or(0);
        if let Some(raw)=sqlx::query_scalar::<_,Vec<u8>>("SELECT value FROM docs WHERE key=?")
            .bind(format!("run:{rid}")).fetch_optional(&mut *tx).await? {
            let previous=self.unseal(&raw)?;
            if previous["generation"] != run["generation"]
                || previous["lease_epoch"] != run["lease_epoch"]
                || previous["run_revision"].as_u64().unwrap_or(0)!=expected
                || (matches!(previous["status"].as_str(),Some("completed"|"cancelled"|"failed"))
                    && previous!=*run)
                || (previous["status"]!="running" && run["status"]=="running"
                    && previous["lease_epoch"].as_i64().unwrap_or(0)>0)
            {
                return Err(anyhow!("Run fence rejected stale transition"));
            }
            if previous==*run && run["status"]=="completed" {
                return Ok(());
            }
            if !allow_expired && previous["status"]=="running"
                && previous["lease_epoch"].as_i64().unwrap_or(0)>0 {
                let lease=sqlx::query("SELECT lease_epoch,lease_owner,lease_expires_at FROM run_index WHERE id=?")
                    .bind(&rid).fetch_one(&mut *tx).await?;
                let until:i64=lease.get("lease_expires_at");
                let lease_epoch:i64=lease.get("lease_epoch");
                let lease_owner:Option<String>=lease.get("lease_owner");
                let now=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs() as i64;
                if until<now || lease_epoch!=run["lease_epoch"].as_i64().unwrap_or(0)
                    || lease_owner.as_deref()!=run["lease_owner"].as_str() {
                    return Err(anyhow!("Run lease expired or changed"));
                }
            }
        } else if expected!=0 {
            return Err(anyhow!("Run revision missing"));
        }
        let mut persisted=run.clone();
        persisted["run_revision"]=json!(expected+1);
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(format!("run:{}",run["id"].as_str().unwrap())).bind(self.seal(&persisted)?).execute(&mut *tx).await?;
        // Large model and tool outputs live in interactions and the run checkpoint.
        if kind != "model_output" && kind != "tool_result" {
            sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?,?,?)")
                .bind(run["session_id"].as_str().unwrap())
                .bind(kind)
                .bind(self.seal(payload)?)
                .execute(&mut *tx)
                .await?;
        }
        let sid = run["session_id"]
            .as_str()
            .ok_or_else(|| anyhow!("Run has no session"))?;
        let rid = run["id"].as_str().ok_or_else(|| anyhow!("Run has no id"))?;
        if let Some(item) = run["input"].as_array().and_then(|items| items.last()) {
            if item["type"] == "function_call_output" {
                let call_id = item["call_id"]
                    .as_str()
                    .ok_or_else(|| anyhow!("Tool output has no call ID"))?;
                self.insert_interaction(&mut tx, sid, rid, &format!("tool:{call_id}"), item)
                    .await?;
            }
        }
        if kind == "model_output" {
            let output = payload["output"]
                .as_array()
                .ok_or_else(|| anyhow!("Model output is missing"))?;
            let turn = run["turns"]
                .as_u64()
                .ok_or_else(|| anyhow!("Run turn is missing"))?;
            for (ordinal, item) in output.iter().enumerate() {
                self.insert_interaction(
                    &mut tx,
                    sid,
                    rid,
                    &format!("model:{turn}:{ordinal}"),
                    item,
                )
                .await?;
            }
            if run["status"] == "completed" {
                let text = payload["terminal_text"]
                    .as_str()
                    .ok_or_else(|| anyhow!("Terminal reply is missing"))?;
                if text.trim().is_empty() {
                    return Err(anyhow!("Terminal reply is empty"));
                }
                self.insert_chat(&mut tx, sid, rid, "assistant", &json!(text))
                    .await?;
            }
        }
        if kind == "context_compacted" {
            let session_key = format!("session:{sid}");
            let row = sqlx::query("SELECT value FROM docs WHERE key=?")
                .bind(&session_key)
                .fetch_one(&mut *tx)
                .await?;
            let mut session = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
            let through = sqlx::query_scalar::<_, i64>(
                "SELECT COALESCE(MAX(seq),0) FROM interactions WHERE session_id=?",
            )
            .bind(sid)
            .fetch_one(&mut *tx)
            .await?;
            session["checkpoint_input"] = run["input"].clone();
            session["checkpoint_through"] = json!(through);
            sqlx::query("UPDATE docs SET value=? WHERE key=?")
                .bind(self.seal(&session)?)
                .bind(&session_key)
                .execute(&mut *tx)
                .await?;
        }
        self.index_run(&mut tx, &persisted).await?;
        sqlx::query("INSERT INTO loop_events(run_id,schema_version,kind,payload,created_at) VALUES(?,1,?,?,strftime('%s','now'))")
            .bind(rid).bind(kind).bind(self.seal(&json!({"schema_version":1,"run_id":rid,
                "generation":run["generation"],"lease_epoch":run["lease_epoch"],
                "status":run["status"],"kind":kind}))?)
            .execute(&mut *tx).await?;
        if run["status"]=="completed" {
            sqlx::query("INSERT OR IGNORE INTO publication_outbox(run_id,schema_version,payload,created_at) VALUES(?,1,?,strftime('%s','now'))")
                .bind(rid).bind(self.seal(&json!({"schema_version":1,"run_id":rid,
                    "generation":run["generation"],"status":"completed"}))?)
                .execute(&mut *tx).await?;
        }
        tx.commit().await?;
        self.cache.lock().unwrap().invalidate(&[
            format!("run:{}", run["id"].as_str().unwrap()),
            format!("session:{}", run["session_id"].as_str().unwrap()),
        ]);
        if kind == "context_compacted" {
            self.context_cache.lock().unwrap().invalidate(sid);
        }
        run["run_revision"]=json!(expected+1);
        Ok(())
    }
    // Take temporary ownership of queued work in one transaction. A new
    // generation, lease epoch, and revision fence out a previous loop worker.
    pub async fn claim_run(&self, run: &mut Value, owner: &str, generation: &str) -> Result<bool> {
        let rid=run["id"].as_str().ok_or_else(||anyhow!("Run has no id"))?.to_owned();
        let mut tx=self.pool.begin().await?;
        let raw:Option<Vec<u8>>=sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
            .bind(format!("run:{rid}")).fetch_optional(&mut *tx).await?;
        let Some(raw)=raw else {return Ok(false)};
        let previous=self.unseal(&raw)?;
        if previous["status"]!="queued" || previous["generation"]!=run["generation"]
            || previous["lease_epoch"]!=run["lease_epoch"]
            || previous["run_revision"]!=run["run_revision"] {return Ok(false);}
        let epoch:i64=sqlx::query_scalar("SELECT lease_epoch FROM run_index WHERE id=?")
            .bind(&rid).fetch_one(&mut *tx).await?;
        let mut claimed=run.clone();
        claimed["lease_epoch"]=json!(epoch+1);
        claimed["lease_owner"]=json!(owner);
        claimed["generation"]=json!(generation);
        claimed["status"]=json!("running");
        claimed["run_revision"]=json!(previous["run_revision"].as_u64().unwrap_or(0)+1);
        let until=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs() as i64+30;
        sqlx::query("UPDATE docs SET value=? WHERE key=?")
            .bind(self.seal(&claimed)?).bind(format!("run:{rid}")).execute(&mut *tx).await?;
        self.index_run(&mut tx,&claimed).await?;
        sqlx::query("UPDATE run_index SET lease_epoch=?,lease_owner=?,lease_expires_at=? WHERE id=?")
            .bind(epoch+1).bind(owner).bind(until).bind(&rid).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO loop_events(run_id,schema_version,kind,payload,created_at) VALUES(?,1,'claimed',?,strftime('%s','now'))")
            .bind(&rid).bind(self.seal(&json!({"schema_version":1,"run_id":rid,
                "generation":generation,"lease_epoch":epoch+1,"kind":"claimed"}))?)
            .execute(&mut *tx).await?;
        tx.commit().await?;
        *run=claimed;
        self.cache.lock().unwrap().invalidate(&[format!("run:{rid}")]);
        Ok(true)
    }
    pub async fn renew_run_lease(&self, rid:&str, generation:&str, owner:&str, epoch:i64) -> Result<bool> {
        let mut tx=self.pool.begin().await?;
        let raw:Option<Vec<u8>>=sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
            .bind(format!("run:{rid}")).fetch_optional(&mut *tx).await?;
        let Some(raw)=raw else {return Ok(false)};
        let run=self.unseal(&raw)?;
        if run["generation"]!=generation || run["status"]!="running"
            || run["lease_epoch"]!=epoch {return Ok(false);}
        let changed=sqlx::query("UPDATE run_index SET lease_expires_at=? WHERE id=? AND lease_owner=? AND lease_epoch=?")
            .bind(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs() as i64+30).bind(rid).bind(owner).bind(epoch)
            .execute(&mut *tx).await?.rows_affected();
        tx.commit().await?;
        Ok(changed==1)
    }
    pub async fn write_loop_heartbeat(&self, owner:&str) -> Result<()> {
        sqlx::query("INSERT INTO loop_heartbeat(singleton,owner,protocol_version,updated_at) VALUES(1,?,1,strftime('%s','now')) ON CONFLICT(singleton) DO UPDATE SET owner=excluded.owner,protocol_version=1,updated_at=excluded.updated_at")
            .bind(owner).execute(&self.app_pool).await?;
        Ok(())
    }
    pub async fn loop_fresh(&self, seconds:i64) -> Result<bool> {
        let age:Option<i64>=sqlx::query_scalar(
            "SELECT CAST(strftime('%s','now') AS INTEGER)-updated_at FROM loop_heartbeat WHERE singleton=1 AND protocol_version=1")
            .fetch_optional(&self.app_pool).await?;
        Ok(age.is_some_and(|age|age>=0 && age<=seconds))
    }
    pub async fn clear_loop_heartbeat(&self) -> Result<()> {
        sqlx::query("UPDATE loop_heartbeat SET updated_at=0 WHERE singleton=1")
            .execute(&self.app_pool).await?;
        Ok(())
    }
    // Validate and mark accepted commands as seen. The queued run remains the
    // authority for execution, so acknowledging a command does not erase work.
    pub async fn acknowledge_loop_commands(&self) -> Result<usize> {
        let mut tx=self.pool.begin().await?;
        let rows=sqlx::query("SELECT seq,run_id,schema_version,kind,payload FROM loop_commands WHERE acknowledged_at IS NULL ORDER BY seq LIMIT 128")
            .fetch_all(&mut *tx).await?;
        for row in &rows {
            let seq:i64=row.get("seq");
            let run_id:String=row.get("run_id");
            let version:i64=row.get("schema_version");
            let kind:String=row.get("kind");
            let payload=self.unseal(&row.get::<Vec<u8>,_>("payload"))?;
            if version!=1 || payload["schema_version"]!=1 || payload["run_id"]!=run_id
                || payload["kind"]!=kind {
                return Err(anyhow!("Unsupported or malformed loop command"));
            }
            sqlx::query("UPDATE loop_commands SET acknowledged_at=strftime('%s','now') WHERE seq=? AND acknowledged_at IS NULL")
                .bind(seq).execute(&mut *tx).await?;
            sqlx::query("INSERT INTO loop_events(run_id,schema_version,kind,payload,created_at) VALUES(?,1,'command_ack',?,strftime('%s','now'))")
                .bind(&run_id).bind(self.seal(&json!({"schema_version":1,
                    "run_id":run_id,"command_seq":seq,"kind":"command_ack"}))?)
                .execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(rows.len())
    }
    pub async fn events(&self, sid: &str, after: i64) -> Result<Vec<Value>> {
        let rows=sqlx::query("SELECT seq,kind,payload FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT 200")
            .bind(sid).bind(after).fetch_all(&self.pool).await?;
        rows.iter().map(|r|Ok(json!({"seq":r.get::<i64,_>("seq"),"kind":r.get::<String,_>("kind"),"payload":self.unseal(&r.get::<Vec<u8>,_>("payload"))?}))).collect()
    }
    // Save related documents and their event together. For message admission,
    // this includes the queued run and retry receipt: either all exist or none do.
    pub async fn atomic_docs_event(
        &self,
        mut docs: Vec<(String, Value)>,
        sid: &str,
        kind: &str,
        payload: &Value,
    ) -> Result<()> {
        let destination = if sid.is_empty() {
            &self.app_pool
        } else {
            &self.pool
        };
        if docs
            .iter()
            .any(|(key, _)| !std::ptr::eq(self.pool_for(key), destination))
        {
            return Err(anyhow!("Atomic documents cross storage authorities"));
        }
        let touched = docs.iter().map(|(key, _)| key.clone()).collect::<Vec<_>>();
        let touches_session = touched.iter().any(|key| key.starts_with("session:"));
        let _context_guard = if touches_session {
            Some(self.context_gate.write().await)
        } else {
            None
        };
        let mut tx = destination.begin().await?;
        if let Some((_, run))=docs.iter_mut().find(|(key,_)|key.starts_with("run:")) {
            let rid=run["id"].as_str().ok_or_else(||anyhow!("Run has no id"))?.to_owned();
            let expected=run["run_revision"].as_u64().unwrap_or(0);
            if let Some(raw)=sqlx::query_scalar::<_,Vec<u8>>("SELECT value FROM docs WHERE key=?")
                .bind(format!("run:{rid}")).fetch_optional(&mut *tx).await? {
                let previous=self.unseal(&raw)?;
                if previous["generation"]!=run["generation"]
                    || previous["lease_epoch"]!=run["lease_epoch"]
                    || previous["run_revision"].as_u64().unwrap_or(0)!=expected
                    || (matches!(previous["status"].as_str(),Some("completed"|"cancelled"|"failed"))
                        && previous!=*run)
                {
                    return Err(anyhow!("Run fence rejected stale command"));
                }
            } else if expected!=0 {
                return Err(anyhow!("Run revision missing"));
            }
            run["run_revision"]=json!(expected+1);
        }
        let command_id=docs.iter().find(|(key,_)|key.starts_with("receipt:"))
            .map(|(key,_)|key.clone());
        for (key, value) in docs {
            sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
                .bind(&key).bind(self.seal(&value)?).execute(&mut *tx).await?;
            if key.starts_with("run:") {
                self.index_run(&mut tx, &value).await?;
            }
        }
        if kind == "message" && payload["role"] == "user" {
            let run_id = payload["run_id"]
                .as_str()
                .ok_or_else(|| anyhow!("User message has no run ID"))?;
            let text = payload["content"]
                .as_str()
                .ok_or_else(|| anyhow!("User message has no content"))?;
            self.insert_chat(&mut tx, sid, run_id, "user", &json!(text))
                .await?;
            // Chat shows the author's message; model context also includes
            // the validated attachment text, committed in this same transaction.
            let context_content = payload["context_content"].as_str().unwrap_or(text);
            self.insert_interaction(
                &mut tx,
                sid,
                run_id,
                "user",
                &json!({"role":"user","content":context_content}),
            )
            .await?;
        }
        if kind != "message" {
            sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES(?,?,?)")
                .bind(sid)
                .bind(kind)
                .bind(self.seal(payload)?)
                .execute(&mut *tx)
                .await?;
        }
        if !sid.is_empty() && (kind=="message" || kind=="run_status") {
            let run_id=payload["run_id"].as_str().or_else(||payload["id"].as_str())
                .ok_or_else(||anyhow!("Run command has no id"))?;
            let command_id=command_id.unwrap_or_else(||format!("{kind}:{run_id}"));
            sqlx::query("INSERT INTO loop_commands(command_id,run_id,schema_version,kind,payload,created_at) VALUES(?,?,1,?,?,strftime('%s','now'))")
                .bind(command_id).bind(run_id).bind(kind)
                .bind(self.seal(&json!({"schema_version":1,"run_id":run_id,
                    "kind":kind,"status":payload["status"]}))?)
                .execute(&mut *tx).await?;
            sqlx::query("INSERT INTO loop_events(run_id,schema_version,kind,payload,created_at) VALUES(?,1,?,?,strftime('%s','now'))")
                .bind(run_id).bind(kind).bind(self.seal(&json!({"schema_version":1,
                    "run_id":run_id,"kind":kind}))?).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        self.cache.lock().unwrap().invalidate(&touched);
        if touches_session {
            let mut context_cache = self.context_cache.lock().unwrap();
            for key in &touched {
                if let Some(sid) = key.strip_prefix("session:") {
                    context_cache.invalidate(sid);
                }
            }
        }
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    #[tokio::test]
    async fn legacy_agent_docs_and_prompt_slots_migrate_into_canonical_tables() {
        let root = tempfile::tempdir().unwrap();
        let store = super::Store::open(root.path()).await.unwrap();
        store.put("agent:shared_default:model",
            &serde_json::json!({"provider":"openai_subscription","model":"gpt-6-sol"}))
            .await.unwrap();
        store.put("agent_abilities:shared_default",
            &serde_json::json!({"revision":3,"visibility":{"codebase_admin":"off"},
                "skill_visibility":{"codebase_admin":"discoverable"}}))
            .await.unwrap();
        sqlx::query("INSERT INTO prompt_slots(slot_name,order_index,locked,merge_mode,content,revision,updated_at) VALUES(?,?,?,?,?,?,?)")
            .bind("legacy").bind(10_i64).bind(1_i64).bind("replace")
            .bind(store.seal(&serde_json::json!("saved prompt")).unwrap())
            .bind(2_i64).bind(100_i64).execute(&store.agent_pool).await.unwrap();
        sqlx::query("DELETE FROM agents WHERE id='shared_default'")
            .execute(&store.agent_pool).await.unwrap();
        store.app_pool.close().await;
        store.pool.close().await;
        store.agent_pool.close().await;
        let migrated = super::Store::open(root.path()).await.unwrap();
        assert_eq!(migrated.agent_model().await.unwrap()["model"], "gpt-6-sol");
        assert_eq!(migrated.agent_abilities().await.unwrap()["visibility"]["codebase_admin"], "off");
        assert_eq!(migrated.agent_abilities().await.unwrap()["skill_visibility"]["codebase_admin"], "discoverable");
        assert_eq!(migrated.get_prompt_slot("legacy").await.unwrap().unwrap()["content"], "saved prompt");
        let enabled: i64 = sqlx::query_scalar("SELECT enabled FROM agent_connections WHERE id='ability:codebase_admin'")
            .fetch_one(&migrated.agent_pool).await.unwrap();
        assert_eq!(enabled, 0);
    }
    use super::*;
    #[tokio::test]
    async fn ability_controls_use_encrypted_authorities_and_survive_reopen() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        let agent = json!({"revision":1,"visibility":{"codebase_admin":"visible"}});
        let session = json!({"revision":1,"active":[],"suppressed":["codebase_admin"]});
        store
            .put("agent_abilities:shared_default", &agent)
            .await
            .unwrap();
        store
            .put("session_ability_state:s1", &session)
            .await
            .unwrap();
        let agent_blob: Vec<u8> = sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
            .bind("agent_abilities:shared_default")
            .fetch_one(&store.agent_pool)
            .await
            .unwrap();
        let session_blob: Vec<u8> = sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
            .bind("session_ability_state:s1")
            .fetch_one(&store.pool)
            .await
            .unwrap();
        assert!(!agent_blob
            .windows(b"codebase_admin".len())
            .any(|part| part == b"codebase_admin"));
        assert!(!session_blob
            .windows(b"codebase_admin".len())
            .any(|part| part == b"codebase_admin"));
        let wrong_agent: Option<Vec<u8>> = sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
            .bind("agent_abilities:shared_default")
            .fetch_optional(&store.pool)
            .await
            .unwrap();
        let wrong_session: Option<Vec<u8>> =
            sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
                .bind("session_ability_state:s1")
                .fetch_optional(&store.agent_pool)
                .await
                .unwrap();
        assert!(wrong_agent.is_none() && wrong_session.is_none());
        store.app_pool.close().await;
        store.pool.close().await;
        store.agent_pool.close().await;
        let reopened = Store::open(root.path()).await.unwrap();
        assert_eq!(
            reopened
                .get("agent_abilities:shared_default")
                .await
                .unwrap(),
            Some(agent)
        );
        assert_eq!(
            reopened.get("session_ability_state:s1").await.unwrap(),
            Some(session)
        );
    }

    #[tokio::test]
    async fn session_mode_is_encrypted_in_user_storage_and_survives_reopen() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store
            .put("session:s1", &json!({"id":"s1","title":"Test session"}))
            .await
            .unwrap();
        let mode = json!({"mode":"coding","revision":1});
        store.put("session_mode:s1", &mode).await.unwrap();
        assert_eq!(store.list("session:").await.unwrap().len(), 1);
        assert_eq!(
            store.list("session_mode:").await.unwrap(),
            vec![mode.clone()]
        );
        let sealed: Vec<u8> = sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
            .bind("session_mode:s1")
            .fetch_one(&store.pool)
            .await
            .unwrap();
        assert!(!sealed
            .windows(b"coding".len())
            .any(|part| part == b"coding"));
        let app_copy: Option<Vec<u8>> = sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
            .bind("session_mode:s1")
            .fetch_optional(&store.app_pool)
            .await
            .unwrap();
        assert!(app_copy.is_none());
        store.app_pool.close().await;
        store.pool.close().await;
        store.agent_pool.close().await;

        let reopened = Store::open(root.path()).await.unwrap();
        assert_eq!(reopened.get("session_mode:s1").await.unwrap(), Some(mode));
        assert_eq!(reopened.list("session:").await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn prompt_slots_are_ordered_revisioned_encrypted_and_durable() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        let first = store
            .upsert_prompt_slot("core", "private prompt", None, None, None, 0)
            .await
            .unwrap();
        assert_eq!(first["revision"], 1);
        assert_eq!(first["order_index"], 10);
        assert_eq!(first["lock"], false);
        assert_eq!(first["merge_mode"], "replace");
        let second = store
            .upsert_prompt_slot("style", "formal", Some(5), Some(true), Some("append"), 0)
            .await
            .unwrap();
        assert_eq!(second["order_index"], 5);
        assert_eq!(
            store
                .list_prompt_slots()
                .await
                .unwrap()
                .iter()
                .map(|slot| slot["slot_name"].as_str().unwrap().to_owned())
                .collect::<Vec<_>>(),
            vec!["style", "core"]
        );
        let updated = store
            .upsert_prompt_slot("style", "brief", None, None, None, 1)
            .await
            .unwrap();
        assert_eq!(updated["revision"], 2);
        assert_eq!(updated["order_index"], 5);
        assert_eq!(updated["lock"], true);
        assert_eq!(updated["merge_mode"], "append");
        assert_eq!(
            store.get_prompt_slot("style").await.unwrap().unwrap(),
            updated
        );
        assert_eq!(
            store
                .upsert_prompt_slot("style", "stale", None, None, None, 1)
                .await
                .unwrap_err()
                .to_string(),
            "Prompt slot revision mismatch"
        );
        assert_eq!(
            store
                .delete_prompt_slot("style", 1)
                .await
                .unwrap_err()
                .to_string(),
            "Prompt slot revision mismatch"
        );
        assert_eq!(
            store
                .delete_prompt_slot("absent", 1)
                .await
                .unwrap_err()
                .to_string(),
            "Prompt slot not found"
        );
        let encrypted: Vec<u8> =
            sqlx::query_scalar("SELECT content FROM agent_prompts WHERE slot_name='core'")
                .fetch_one(&store.agent_pool)
                .await
                .unwrap();
        assert!(!encrypted
            .windows(b"private prompt".len())
            .any(|window| window == b"private prompt"));
        store.app_pool.close().await;
        store.pool.close().await;
        store.agent_pool.close().await;
        let reopened = Store::open(root.path()).await.unwrap();
        assert_eq!(
            reopened.get_prompt_slot("core").await.unwrap().unwrap()["content"],
            "private prompt"
        );
        assert_eq!(
            reopened.get_prompt_slot("style").await.unwrap().unwrap()["revision"],
            2
        );
        assert!(reopened.delete_prompt_slot("style", 2).await.unwrap());
        assert!(reopened.get_prompt_slot("style").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn prompt_slots_enforce_name_size_count_total_and_metadata_bounds() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        for name in ["", " bad", "bad\nname", "__skills__"] {
            assert!(store
                .upsert_prompt_slot(name, "", None, None, None, 0)
                .await
                .is_err());
        }
        assert!(store
            .upsert_prompt_slot(&"x".repeat(129), "", None, None, None, 0)
            .await
            .is_err());
        assert!(store
            .upsert_prompt_slot(
                "bad-content",
                &"x".repeat(16 * 1024 + 1),
                None,
                None,
                None,
                0
            )
            .await
            .is_err());
        assert!(store
            .upsert_prompt_slot("bad-mode", "", None, None, Some("prepend"), 0)
            .await
            .is_err());
        assert!(store
            .upsert_prompt_slot("bad-order", "", Some(-1), None, None, 0)
            .await
            .is_err());
        assert!(store
            .upsert_prompt_slot("bad-revision", "", None, None, None, -1)
            .await
            .is_err());
        for i in 0..4 {
            store
                .upsert_prompt_slot(
                    &format!("large-{i}"),
                    &"x".repeat(16 * 1024),
                    None,
                    None,
                    None,
                    0,
                )
                .await
                .unwrap();
        }
        assert!(store
            .upsert_prompt_slot("over-total", "x", None, None, None, 0)
            .await
            .unwrap_err()
            .to_string()
            .contains("total exceeds limit"));
        for i in 4..32 {
            store
                .upsert_prompt_slot(&format!("slot-{i}"), "", None, None, None, 0)
                .await
                .unwrap();
        }
        assert_eq!(store.list_prompt_slots().await.unwrap().len(), 32);
        assert!(store
            .upsert_prompt_slot("over-count", "", None, None, None, 0)
            .await
            .unwrap_err()
            .to_string()
            .contains("count exceeds limit"));
        assert_eq!(store.list_prompt_slots().await.unwrap().len(), 32);
    }

    #[tokio::test]
    async fn context_cache_appends_only_new_interactions() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store
            .put(
                "session:s",
                &json!({"id":"s","checkpoint_input":[],"checkpoint_through":0}),
            )
            .await
            .unwrap();
        store
            .atomic_docs_event(
                vec![],
                "s",
                "message",
                &json!({"role":"user","content":"first","run_id":"r1"}),
            )
            .await
            .unwrap();
        assert_eq!(store.context("s").await.unwrap()[0]["content"], "first");
        let first_cursor = store.context_cache.lock().unwrap().get("s", 0).unwrap().1;
        store
            .atomic_docs_event(
                vec![],
                "s",
                "message",
                &json!({"role":"user","content":"second","run_id":"r2"}),
            )
            .await
            .unwrap();
        let input = store.context("s").await.unwrap();
        assert_eq!(input.len(), 2);
        assert_eq!(input[0]["content"], "first");
        assert_eq!(input[1]["content"], "second");
        let second_cursor = store.context_cache.lock().unwrap().get("s", 0).unwrap().1;
        assert!(second_cursor > first_cursor);
    }

    #[tokio::test]
    async fn context_cache_invalidates_same_cursor_checkpoint_and_rebuilds_after_reopen() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store
            .put(
                "session:s",
                &json!({"id":"s","checkpoint_input":[{"role":"user","content":"old"}],"checkpoint_through":0}),
            )
            .await
            .unwrap();
        assert_eq!(store.context("s").await.unwrap()[0]["content"], "old");
        let summary = json!({"role":"user","content":"summary"});
        let mut checkpoint=json!({"id":"r1","session_id":"s","status":"running",
            "input":[summary.clone()],"updated_at":1});
        store
            .save_run(
                &mut checkpoint,
                "context_compacted",
                &json!({"run_id":"r1"}),
            )
            .await
            .unwrap();
        assert_eq!(store.context("s").await.unwrap(), vec![summary.clone()]);
        store
            .atomic_docs_event(
                vec![],
                "s",
                "message",
                &json!({"role":"user","content":"new","run_id":"r2"}),
            )
            .await
            .unwrap();
        assert_eq!(store.context("s").await.unwrap().len(), 2);
        store.pool.close().await;
        store.app_pool.close().await;
        store.agent_pool.close().await;
        let reopened = Store::open(root.path()).await.unwrap();
        let rebuilt = reopened.context("s").await.unwrap();
        assert_eq!(rebuilt.len(), 2);
        assert_eq!(rebuilt[0], summary);
        assert_eq!(rebuilt[1]["content"], "new");
    }

    #[tokio::test]
    async fn concurrent_context_readers_cannot_restore_precheckpoint_cache() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store
            .put(
                "session:s",
                &json!({"id":"s","checkpoint_input":[{"role":"user","content":"old"}],"checkpoint_through":0}),
            )
            .await
            .unwrap();
        store.context("s").await.unwrap();
        let mut readers = Vec::new();
        for _ in 0..16 {
            let clone = store.clone();
            readers.push(tokio::spawn(
                async move { clone.context("s").await.unwrap() },
            ));
        }
        let mut checkpoint=json!({"id":"r1","session_id":"s","status":"running",
            "input":[],"updated_at":1});
        checkpoint["input"]=json!([{"role":"user","content":"new checkpoint"}]);
        store
            .save_run(
                &mut checkpoint,
                "context_compacted",
                &json!({"run_id":"r1"}),
            )
            .await
            .unwrap();
        for reader in readers {
            reader.await.unwrap();
        }
        for _ in 0..4 {
            assert_eq!(
                store.context("s").await.unwrap()[0]["content"],
                "new checkpoint"
            );
        }
    }

    #[tokio::test]
    async fn split_storage_preserves_legacy_history_and_retries_version_one_marker() {
        let root = tempfile::tempdir().unwrap();
        let first = Store::open(root.path()).await.unwrap();
        first
            .put("settings", &json!({"model":"new"}))
            .await
            .unwrap();
        first
            .put("storage:split_migrated", &json!({"version":1}))
            .await
            .unwrap();
        first.put("policy", &json!({"version":4})).await.unwrap();
        first
            .put("provider_tokens", &json!({"access_token":"new"}))
            .await
            .unwrap();
        first
            .put("agent:shared_default:model", &json!({"model":"new-agent"}))
            .await
            .unwrap();
        for ordinal in 1..=4 {
            first
                .event("old", "split_event", &json!({"ordinal":ordinal}))
                .await
                .unwrap();
        }
        sqlx::query(
            "INSERT INTO events(seq,session_id,kind,payload) VALUES(9,'','split_policy',?)",
        )
        .bind(first.seal(&json!({"version":4})).unwrap())
        .execute(&first.app_pool)
        .await
        .unwrap();
        let legacy_path = root.path().join("runtime.sqlite");
        let legacy = SqlitePool::connect(&format!("sqlite://{}?mode=rwc", legacy_path.display()))
            .await
            .unwrap();
        sqlx::raw_sql("CREATE TABLE docs(key TEXT PRIMARY KEY,value BLOB NOT NULL);
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,kind TEXT NOT NULL,payload BLOB NOT NULL);
            CREATE TABLE run_index(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,status TEXT NOT NULL,updated_at INTEGER NOT NULL);")
            .execute(&legacy).await.unwrap();
        for (key, value) in [
            ("", json!({"empty":"key"})),
            ("admin", json!({"username":"admin"})),
            ("provider_tokens", json!({"access_token":"old"})),
            ("settings", json!({"model":"chosen"})),
            ("policy", json!({"version":3})),
            ("control", json!({"shutoff":false})),
            (
                "agent:shared_default:model",
                json!({"model":"chosen-agent"}),
            ),
            ("receipt:message:old:req", json!({"result":{"run_id":"r1"}})),
            (
                "session:old",
                json!({
                    "id":"old","created_at":10,
                    "messages":[
                        {"role":"user","content":"hello","run_id":"r1"},
                        {"role":"assistant","content":"world","run_id":"r1"}
                    ],
                    "context":[{"role":"user","content":"hello"},{"role":"assistant","content":"world"}]
                }),
            ),
            (
                "run:r1",
                json!({"id":"r1","session_id":"old","status":"completed","updated_at":11,"input":[]}),
            ),
            (
                "run_view:r1",
                json!({"id":"r1","session_id":"old","status":"completed","updated_at":11}),
            ),
        ] {
            sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)")
                .bind(key)
                .bind(first.seal(&value).unwrap())
                .execute(&legacy)
                .await
                .unwrap();
        }
        for ordinal in 0..130 {
            sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)")
                .bind(format!("extra:{ordinal:03}"))
                .bind(first.seal(&json!({"ordinal":ordinal})).unwrap())
                .execute(&legacy)
                .await
                .unwrap();
        }
        for (seq, sid, kind, payload) in [
            (
                4,
                "old",
                "message",
                json!({"role":"user","content":"hello","run_id":"r1"}),
            ),
            (
                7,
                "old",
                "run_status",
                json!({"run_id":"r1","status":"completed"}),
            ),
            (9, "", "policy_changed", json!({"version":3})),
        ] {
            sqlx::query("INSERT INTO events(seq,session_id,kind,payload) VALUES(?,?,?,?)")
                .bind(seq)
                .bind(sid)
                .bind(kind)
                .bind(first.seal(&payload).unwrap())
                .execute(&legacy)
                .await
                .unwrap();
        }
        sqlx::query("INSERT INTO run_index(id,session_id,status,updated_at) VALUES('r1','old','completed',11)")
            .execute(&legacy).await.unwrap();
        legacy.close().await;
        first.app_pool.close().await;
        first.pool.close().await;
        first.agent_pool.close().await;

        let migrated = Store::open(root.path()).await.unwrap();
        assert_eq!(migrated.get("").await.unwrap().unwrap()["empty"], "key");
        assert_eq!(
            migrated.get("extra:129").await.unwrap().unwrap()["ordinal"],
            129
        );
        assert_eq!(
            migrated.get("settings").await.unwrap().unwrap()["model"],
            "new"
        );
        assert_eq!(
            migrated
                .get("agent:shared_default:model")
                .await
                .unwrap()
                .unwrap()["model"],
            "new-agent"
        );
        assert_eq!(
            migrated
                .get("receipt:message:old:req")
                .await
                .unwrap()
                .unwrap()["result"]["run_id"],
            "r1"
        );
        assert_eq!(
            migrated.get("run:r1").await.unwrap().unwrap()["status"],
            "completed"
        );
        assert_eq!(migrated.context("old").await.unwrap().len(), 2);
        let (messages, cursor) = migrated.chat_snapshot("old").await.unwrap();
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0]["content"], "hello");
        assert_eq!(messages[1]["content"], "world");
        assert_eq!(migrated.recent_runs(Some("old")).await.unwrap().len(), 1);
        let events = migrated.events("old", 0).await.unwrap();
        assert_eq!(
            events
                .iter()
                .map(|event| event["seq"].as_i64().unwrap())
                .collect::<Vec<_>>(),
            vec![1, 2, 3, 4, 5, 6]
        );
        assert_eq!(migrated.events("old", 4).await.unwrap().len(), 2);
        assert_eq!(events[4]["payload"]["content"], "hello");
        let mapped: i64 =
            sqlx::query_scalar("SELECT new_seq FROM legacy_event_map WHERE legacy_seq=4")
                .fetch_one(&migrated.pool)
                .await
                .unwrap();
        assert_eq!(mapped, 5);
        let app_event: Vec<u8> = sqlx::query_scalar("SELECT payload FROM events WHERE seq=10")
            .fetch_one(&migrated.app_pool)
            .await
            .unwrap();
        assert_eq!(migrated.unseal(&app_event).unwrap()["version"], 3);
        assert_eq!(migrated.get("policy").await.unwrap().unwrap()["version"], 4);
        assert_eq!(
            migrated.get("provider_tokens").await.unwrap().unwrap()["access_token"],
            "new"
        );
        assert_eq!(
            migrated
                .get("storage:split_migrated")
                .await
                .unwrap()
                .unwrap()["version"],
            2
        );
        migrated.app_pool.close().await;
        migrated.pool.close().await;
        migrated.agent_pool.close().await;
        let reopened = Store::open(root.path()).await.unwrap();
        assert_eq!(reopened.chat_snapshot("old").await.unwrap().1, cursor);
        assert_eq!(reopened.events("old", 0).await.unwrap().len(), 6);
        assert!(legacy_path.exists());
    }
    #[tokio::test]
    async fn split_storage_rejects_conflicting_destination_without_completion_marker() {
        let root = tempfile::tempdir().unwrap();
        let first = Store::open(root.path()).await.unwrap();
        first
            .put("settings", &json!({"model":"new"}))
            .await
            .unwrap();
        let legacy_path = root.path().join("runtime.sqlite");
        let legacy = SqlitePool::connect(&format!("sqlite://{}?mode=rwc", legacy_path.display()))
            .await
            .unwrap();
        sqlx::raw_sql("CREATE TABLE docs(key TEXT PRIMARY KEY,value BLOB NOT NULL);
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,kind TEXT NOT NULL,payload BLOB NOT NULL);
            CREATE TABLE run_index(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,status TEXT NOT NULL,updated_at INTEGER NOT NULL);")
            .execute(&legacy).await.unwrap();
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)")
            .bind("settings")
            .bind(first.seal(&json!({"model":"old"})).unwrap())
            .execute(&legacy)
            .await
            .unwrap();
        legacy.close().await;
        first.app_pool.close().await;
        first.pool.close().await;
        first.agent_pool.close().await;
        let err = Store::open(root.path()).await.err().unwrap();
        assert!(err.to_string().contains("Conflicting migrated document"));
        let app = open_pool(&root.path().join("db/app.db"), true, false)
            .await
            .unwrap();
        assert!(sqlx::query_scalar::<_, Vec<u8>>(
            "SELECT value FROM docs WHERE key='storage:split_migrated'"
        )
        .fetch_optional(&app)
        .await
        .unwrap()
        .is_none());
    }
    #[tokio::test]
    async fn version_one_migration_rejects_conflicting_run_index() {
        let root = tempfile::tempdir().unwrap();
        let first = Store::open(root.path()).await.unwrap();
        first
            .put("storage:split_migrated", &json!({"version":1}))
            .await
            .unwrap();
        sqlx::query("INSERT INTO run_index(id,session_id,status,updated_at) VALUES('r1','new','running',20)")
            .execute(&first.pool).await.unwrap();
        let path = root.path().join("runtime.sqlite");
        let legacy = SqlitePool::connect(&format!("sqlite://{}?mode=rwc", path.display()))
            .await
            .unwrap();
        sqlx::raw_sql("CREATE TABLE docs(key TEXT PRIMARY KEY,value BLOB NOT NULL);
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,kind TEXT NOT NULL,payload BLOB NOT NULL);
            CREATE TABLE run_index(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,status TEXT NOT NULL,updated_at INTEGER NOT NULL);")
            .execute(&legacy).await.unwrap();
        sqlx::query("INSERT INTO run_index(id,session_id,status,updated_at) VALUES('r1','old','completed',10)")
            .execute(&legacy).await.unwrap();
        legacy.close().await;
        first.app_pool.close().await;
        first.pool.close().await;
        first.agent_pool.close().await;
        let err = Store::open(root.path()).await.err().unwrap();
        assert!(err
            .to_string()
            .contains("Conflicting migrated run index: r1"));
        let user = open_pool(&root.path().join("user_data/admin/admin.db"), true, true)
            .await
            .unwrap();
        let status: String = sqlx::query_scalar("SELECT status FROM run_index WHERE id='r1'")
            .fetch_one(&user)
            .await
            .unwrap();
        assert_eq!(status, "running");
        let app = open_pool(&root.path().join("db/app.db"), true, false)
            .await
            .unwrap();
        let marker: Vec<u8> =
            sqlx::query_scalar("SELECT value FROM docs WHERE key='storage:split_migrated'")
                .fetch_one(&app)
                .await
                .unwrap();
        assert_eq!(first.unseal(&marker).unwrap()["version"], 1);
    }
    #[tokio::test]
    async fn version_one_retry_does_not_reuse_an_already_mapped_event_sequence() {
        let root = tempfile::tempdir().unwrap();
        let first = Store::open(root.path()).await.unwrap();
        first
            .put("storage:split_migrated", &json!({"version":1}))
            .await
            .unwrap();
        for ordinal in 1..=4 {
            first
                .event("old", "split", &json!({"ordinal":ordinal}))
                .await
                .unwrap();
        }
        let user_payload = json!({"kind":"legacy-user"});
        let app_payload = json!({"kind":"legacy-app"});
        sqlx::query("INSERT INTO events(seq,session_id,kind,payload) VALUES(5,'old','legacy',?)")
            .bind(first.seal(&user_payload).unwrap())
            .execute(&first.pool)
            .await
            .unwrap();
        sqlx::query("INSERT INTO legacy_event_map(legacy_seq,new_seq) VALUES(4,5)")
            .execute(&first.pool)
            .await
            .unwrap();
        sqlx::query("INSERT INTO events(seq,session_id,kind,payload) VALUES(9,'','split',?)")
            .bind(first.seal(&json!({"kind":"split-app"})).unwrap())
            .execute(&first.app_pool)
            .await
            .unwrap();
        sqlx::query("INSERT INTO events(seq,session_id,kind,payload) VALUES(10,'','legacy',?)")
            .bind(first.seal(&app_payload).unwrap())
            .execute(&first.app_pool)
            .await
            .unwrap();
        sqlx::query("INSERT INTO legacy_event_map(legacy_seq,new_seq) VALUES(9,10)")
            .execute(&first.app_pool)
            .await
            .unwrap();
        let path = root.path().join("runtime.sqlite");
        let legacy = SqlitePool::connect(&format!("sqlite://{}?mode=rwc", path.display()))
            .await
            .unwrap();
        sqlx::raw_sql("CREATE TABLE docs(key TEXT PRIMARY KEY,value BLOB NOT NULL);
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,kind TEXT NOT NULL,payload BLOB NOT NULL);
            CREATE TABLE run_index(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,status TEXT NOT NULL,updated_at INTEGER NOT NULL);")
            .execute(&legacy).await.unwrap();
        for (seq, sid, payload) in [
            (4, "old", &user_payload),
            (5, "old", &user_payload),
            (9, "", &app_payload),
            (10, "", &app_payload),
        ] {
            sqlx::query("INSERT INTO events(seq,session_id,kind,payload) VALUES(?,?,?,?)")
                .bind(seq)
                .bind(sid)
                .bind("legacy")
                .bind(first.seal(payload).unwrap())
                .execute(&legacy)
                .await
                .unwrap();
        }
        legacy.close().await;
        first.app_pool.close().await;
        first.pool.close().await;
        first.agent_pool.close().await;
        let migrated = Store::open(root.path()).await.unwrap();
        let user_map: i64 =
            sqlx::query_scalar("SELECT new_seq FROM legacy_event_map WHERE legacy_seq=5")
                .fetch_one(&migrated.pool)
                .await
                .unwrap();
        let app_map: i64 =
            sqlx::query_scalar("SELECT new_seq FROM legacy_event_map WHERE legacy_seq=10")
                .fetch_one(&migrated.app_pool)
                .await
                .unwrap();
        assert_eq!((user_map, app_map), (6, 11));
        assert_eq!(migrated.events("old", 0).await.unwrap().len(), 6);
        migrated.app_pool.close().await;
        migrated.pool.close().await;
        migrated.agent_pool.close().await;
        let reopened = Store::open(root.path()).await.unwrap();
        assert_eq!(reopened.events("old", 0).await.unwrap().len(), 6);
        let app_count: i64 = sqlx::query_scalar("SELECT count(*) FROM events")
            .fetch_one(&reopened.app_pool)
            .await
            .unwrap();
        assert_eq!(app_count, 3);
    }
    #[tokio::test]
    async fn typed_state_inspection_is_session_scoped_bounded_and_redacted() {
        let root = tempfile::tempdir().unwrap();
        let s = Store::open(root.path()).await.unwrap();
        s.put(
            "session:a",
            &json!({"id":"a","title":"Alpha","created_at":12}),
        )
        .await
        .unwrap();
        s.put(
            "session:b",
            &json!({"id":"b","title":"Beta","created_at":13}),
        )
        .await
        .unwrap();
        for (seq, sid) in [(2, "a"), (4, "a"), (5, "b"), (6, "a")] {
            sqlx::query("INSERT INTO chat(seq,id,session_id,run_id,role,content,created_at) VALUES(?,?,?,?,?,?,?)")
                .bind(seq).bind(format!("message:{seq}")).bind(sid)
                .bind(format!("run:{seq}")).bind("user")
                .bind(s.seal(&json!(format!("text {seq}"))).unwrap())
                .bind(100 + seq).execute(&s.pool).await.unwrap();
        }
        let summary = s.session_summary("a").await.unwrap();
        assert_eq!(
            summary,
            json!({"id":"a","title":"Alpha","created_at":12,"chat_count":3,"chat_cursor":6})
        );
        let first = s.chat_page("a", 0, 2).await.unwrap();
        assert_eq!(first["messages"].as_array().unwrap().len(), 2);
        assert_eq!(first["messages"][0]["seq"], 2);
        assert_eq!(first["messages"][1]["seq"], 4);
        assert_eq!(first["next_seq"], 4);
        assert_eq!(first["has_more"], true);
        let second = s.chat_page("a", 4, 2).await.unwrap();
        assert_eq!(second["messages"].as_array().unwrap().len(), 1);
        assert_eq!(second["messages"][0]["seq"], 6);
        assert_eq!(second["next_seq"], 6);
        assert_eq!(second["has_more"], false);
        let empty = s.chat_page("a", 6, 2).await.unwrap();
        assert_eq!(empty["next_seq"], 6);
        assert_eq!(empty["has_more"], false);
        assert!(empty["messages"].as_array().unwrap().is_empty());
        for (after, limit) in [(-1, 1), (0, 0), (0, 51)] {
            assert!(s.chat_page("a", after, limit).await.is_err());
        }
        assert!(s.session_summary("missing").await.is_err());
        assert!(s.chat_page("missing", 0, 1).await.is_err());

        for (id, sid, updated_at) in [("r1", "a", 10), ("r2", "a", 20), ("r3", "b", 30)] {
            s.put(&format!("run:{id}"), &json!({
                "id":id,"session_id":sid,"status":"completed","updated_at":updated_at,
                "error":null,"uncertain":false,"turns":2,
                "pending":{"secret":"do not expose"},"provider_selection":{"token":"do not expose"}
            })).await.unwrap();
        }
        let runs = s.run_summaries("a", 2).await.unwrap();
        assert_eq!(runs.len(), 2);
        assert_eq!(runs[0]["id"], "r2");
        assert_eq!(runs[1]["id"], "r1");
        let keys = runs[0]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        assert_eq!(
            keys,
            vec!["error", "id", "status", "turns", "uncertain", "updated_at"]
        );
        assert!(!serde_json::to_string(&runs)
            .unwrap()
            .contains("do not expose"));
        assert_eq!(s.run_summaries("a", 1).await.unwrap().len(), 1);
        assert!(s.run_summaries("a", 0).await.is_err());
        assert!(s.run_summaries("a", 51).await.is_err());
        assert!(s.run_summaries("missing", 1).await.is_err());
    }
    #[tokio::test]
    async fn encrypted_records_survive_reopen_and_tampering_is_rejected() {
        let root = tempfile::tempdir().unwrap();
        let s = Store::open(root.path()).await.unwrap();
        s.put("secret", &json!({"content":"private text"}))
            .await
            .unwrap();
        let raw: Vec<u8> = sqlx::query_scalar("SELECT value FROM docs WHERE key='secret'")
            .fetch_one(&s.app_pool)
            .await
            .unwrap();
        assert!(!raw.windows(12).any(|w| w == b"private text"));
        let mut corrupt = raw.clone();
        corrupt[15] ^= 1;
        assert!(s.unseal(&corrupt).is_err());
        s.app_pool.close().await;
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
    #[tokio::test]
    async fn missing_key_does_not_create_unusable_replacement() {
        let root = tempfile::tempdir().unwrap();
        let s = Store::open(root.path()).await.unwrap();
        s.put("secret", &json!({"content":"private text"}))
            .await
            .unwrap();
        s.pool.close().await;
        std::fs::remove_file(root.path().join("encryption.key")).unwrap();
        let err = Store::open(root.path()).await.err().unwrap();
        assert!(err.to_string().contains("encryption key is missing"));
        assert!(!root.path().join("encryption.key").exists());
    }
    #[test]
    fn concurrent_key_creation_publishes_one_complete_key() {
        let root = tempfile::tempdir().unwrap();
        let handles = (0..8)
            .map(|_| {
                let path = root.path().to_path_buf();
                std::thread::spawn(move || load_or_create_key(&path).unwrap())
            })
            .collect::<Vec<_>>();
        let keys = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect::<Vec<_>>();
        assert!(keys.iter().all(|key| key == &keys[0]));
        assert_eq!(
            std::fs::read(root.path().join("encryption.key")).unwrap(),
            keys[0]
        );
    }
    #[tokio::test]
    async fn command_ack_and_lease_takeover_fence_terminal_publication() {
        let root=tempfile::tempdir().unwrap();
        let store=Store::open(root.path()).await.unwrap();
        let run=json!({"id":"r1","session_id":"s1","status":"queued","generation":"",
            "input":[],"turns":1,"updated_at":1});
        store.atomic_docs_event(vec![
            ("run:r1".into(),run.clone()),
            ("receipt:message:s1:one".into(),json!({"result":{"run_id":"r1"}})),
        ],"s1","message",&json!({"role":"user","content":"hello","run_id":"r1"}))
            .await.unwrap();
        assert_eq!(store.acknowledge_loop_commands().await.unwrap(),1);
        assert_eq!(store.acknowledge_loop_commands().await.unwrap(),0);
        let mut first=store.get("run:r1").await.unwrap().unwrap();
        assert!(store.claim_run(&mut first,"loop-a","generation-a").await.unwrap());
        let mut stale_http=first.clone();
        first["pending"]=json!({"state":"executing","name":"file_write"});
        store.save_run(&mut first,"run_status",&json!({})).await.unwrap();
        stale_http["status"]=json!("stopped");
        assert!(store.save_run(&mut stale_http,"run_status",&json!({})).await.is_err());
        assert_eq!(store.get("run:r1").await.unwrap().unwrap()["pending"]["state"],
            "executing");
        let rejected=store.atomic_docs_event(vec![
            ("run:r1".into(),stale_http.clone()),
            ("receipt:run:r1:cancel:stale".into(),json!({"result":{}})),
        ],"s1","run_status",&json!({"id":"r1","status":"stopped"})).await;
        assert!(rejected.is_err());
        assert!(store.get("receipt:run:r1:cancel:stale").await.unwrap().is_none());
        first["status"]=json!("queued");
        store.save_run(&mut first,"run_status",&json!({})).await.unwrap();
        let mut second=store.get("run:r1").await.unwrap().unwrap();
        assert!(store.claim_run(&mut second,"loop-b","generation-b").await.unwrap());
        sqlx::query("UPDATE run_index SET lease_expires_at=0 WHERE id='r1'")
            .execute(&store.pool).await.unwrap();
        let mut expired=second.clone();
        expired["status"]=json!("completed");
        assert!(store.save_run(&mut expired,"model_output",
            &json!({"output":[],"terminal_text":"expired"})).await.is_err());
        sqlx::query("UPDATE run_index SET lease_expires_at=strftime('%s','now')+30 WHERE id='r1'")
            .execute(&store.pool).await.unwrap();
        first["status"]=json!("completed");
        assert!(store.save_run(&mut first,"model_output",
            &json!({"output":[],"terminal_text":"stale"})).await.is_err());
        let policy=crate::workflow::Policy::resolve(None).unwrap();
        crate::publication::terminal(&store,&mut second,&[],"accepted",&policy)
            .await.unwrap();
        crate::publication::terminal(&store,&mut second,&[],"accepted",&policy)
            .await.unwrap();
        let chat_count:i64=sqlx::query_scalar(
            "SELECT count(*) FROM chat WHERE terminal_run_id='r1'")
            .fetch_one(&store.pool).await.unwrap();
        let outbox_count:i64=sqlx::query_scalar(
            "SELECT count(*) FROM publication_outbox WHERE run_id='r1'")
            .fetch_one(&store.pool).await.unwrap();
        assert_eq!((chat_count,outbox_count),(1,1));
        let observer=Store::open(root.path()).await.unwrap();
        assert_eq!(observer.get("run:r1").await.unwrap().unwrap()["status"],"completed");
    }
}
