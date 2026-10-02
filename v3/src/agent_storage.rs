//! Additive split of nondefault agent records from the original shared agent DB.
//! The default agent keeps its existing path and all legacy rows remain for rollback.
use crate::{access, store::{open_pool, Store}};
use anyhow::{anyhow, bail, Result};
use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use sqlx::{Row, SqlitePool};
use std::path::Path;

const MARKER: &str = "storage:agent_split_v1";

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
struct AgentRow {
    id:String, name:String, description:String, provider:String, model:String,
    status:String, metadata:Vec<u8>, revision:i64, created_at:i64, updated_at:i64,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
struct ConnectionRow {
    id:String, agent_id:String, connection_type:String, section:String,
    enabled:i64, config:Vec<u8>, revision:i64, created_at:i64, updated_at:i64,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
struct PromptRow {
    id:String, agent_id:String, slot_name:String, order_index:i64, lock:i64,
    merge_mode:String, content:Vec<u8>, revision:i64, updated_at:i64,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
struct DocRow { key:String, value:Vec<u8> }
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
struct Snapshot {
    agent:AgentRow,
    connections:Vec<ConnectionRow>,
    prompts:Vec<PromptRow>,
    docs:Vec<DocRow>,
}

pub(crate) async fn open_agent_pool(root:&Path, agent_id:&str) -> Result<SqlitePool> {
    if !access::valid_agent_id(agent_id) { bail!("Invalid agent identity"); }
    let path = root.join("agent_data").join(agent_id).join(format!("{agent_id}.db"));
    let pool = open_pool(&path,false,false).await?;
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
        .execute(&pool).await?;
    Ok(pool)
}

async fn snapshot(pool:&SqlitePool,id:&str) -> Result<Option<Snapshot>> {
    let row = sqlx::query("SELECT id,name,description,provider,model,status,metadata,revision,created_at,updated_at FROM agents WHERE id=?")
        .bind(id).fetch_optional(pool).await?;
    let Some(row)=row else {return Ok(None)};
    let agent=AgentRow {
        id:row.get("id"),name:row.get("name"),description:row.get("description"),
        provider:row.get("provider"),model:row.get("model"),status:row.get("status"),
        metadata:row.get("metadata"),revision:row.get("revision"),
        created_at:row.get("created_at"),updated_at:row.get("updated_at"),
    };
    let connections=sqlx::query("SELECT id,agent_id,connection_type,section,enabled,config,revision,created_at,updated_at FROM agent_connections WHERE agent_id=? ORDER BY id")
        .bind(id).fetch_all(pool).await?.into_iter().map(|row| ConnectionRow {
            id:row.get("id"),agent_id:row.get("agent_id"),connection_type:row.get("connection_type"),
            section:row.get("section"),enabled:row.get("enabled"),config:row.get("config"),
            revision:row.get("revision"),created_at:row.get("created_at"),updated_at:row.get("updated_at")
        }).collect();
    let prompts=sqlx::query("SELECT id,agent_id,slot_name,order_index,lock,merge_mode,content,revision,updated_at FROM agent_prompts WHERE agent_id=? ORDER BY id")
        .bind(id).fetch_all(pool).await?.into_iter().map(|row| PromptRow {
            id:row.get("id"),agent_id:row.get("agent_id"),slot_name:row.get("slot_name"),
            order_index:row.get("order_index"),lock:row.get("lock"),merge_mode:row.get("merge_mode"),
            content:row.get("content"),revision:row.get("revision"),updated_at:row.get("updated_at")
        }).collect();
    let prefix=format!("agent:{id}:");
    let root_key=format!("agent:{id}");
    let ability_key=format!("agent_abilities:{id}");
    let docs=sqlx::query("SELECT key,value FROM docs WHERE substr(key,1,?)=? OR key=? OR key=? ORDER BY key")
        .bind(prefix.len() as i64).bind(prefix).bind(root_key).bind(ability_key)
        .fetch_all(pool).await?
        .into_iter().map(|row| DocRow {key:row.get("key"),value:row.get("value")}).collect();
    Ok(Some(Snapshot {agent,connections,prompts,docs}))
}

async fn copy_snapshot(pool:&SqlitePool,source:&Snapshot) -> Result<()> {
    let mut tx=pool.begin().await?;
    let a=&source.agent;
    sqlx::query("INSERT OR IGNORE INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)")
        .bind(&a.id).bind(&a.name).bind(&a.description).bind(&a.provider).bind(&a.model)
        .bind(&a.status).bind(&a.metadata).bind(a.revision).bind(a.created_at).bind(a.updated_at)
        .execute(&mut *tx).await?;
    for c in &source.connections {
        sqlx::query("INSERT OR IGNORE INTO agent_connections(id,agent_id,connection_type,section,enabled,config,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)")
            .bind(&c.id).bind(&c.agent_id).bind(&c.connection_type).bind(&c.section)
            .bind(c.enabled).bind(&c.config).bind(c.revision).bind(c.created_at).bind(c.updated_at)
            .execute(&mut *tx).await?;
    }
    for p in &source.prompts {
        sqlx::query("INSERT OR IGNORE INTO agent_prompts(id,agent_id,slot_name,order_index,lock,merge_mode,content,revision,updated_at) VALUES(?,?,?,?,?,?,?,?,?)")
            .bind(&p.id).bind(&p.agent_id).bind(&p.slot_name).bind(p.order_index).bind(p.lock)
            .bind(&p.merge_mode).bind(&p.content).bind(p.revision).bind(p.updated_at)
            .execute(&mut *tx).await?;
    }
    for d in &source.docs {
        sqlx::query("INSERT OR IGNORE INTO docs(key,value) VALUES(?,?)")
            .bind(&d.key).bind(&d.value).execute(&mut *tx).await?;
    }
    tx.commit().await?;
    let destination=snapshot(pool,&a.id).await?
        .ok_or_else(||anyhow!("Agent copy missing after commit"))?;
    if destination!=*source {bail!("Agent destination conflicts with source");}
    let other:Option<String>=sqlx::query_scalar("SELECT id FROM agents WHERE id<>? LIMIT 1")
        .bind(&a.id).fetch_optional(pool).await?;
    if other.is_some() {bail!("Agent destination contains another agent");}
    let keys=sqlx::query("SELECT key FROM docs ORDER BY key").fetch_all(pool).await?
        .into_iter().map(|row|row.get::<String,_>("key"))
        .collect::<Vec<_>>();
    let expected=source.docs.iter().map(|doc|doc.key.clone()).collect::<Vec<_>>();
    if keys!=expected {bail!("Agent destination contains unexpected documents");}
    Ok(())
}

async fn validate_destination(store:&Store,pool:&SqlitePool,id:&str,owner:&str) -> Result<()> {
    let value=snapshot(pool,id).await?
        .ok_or_else(||anyhow!("Migrated agent database is missing its agent"))?;
    let metadata:Value=store.unseal(&value.agent.metadata)?;
    if metadata["owner_id"].as_str()!=Some(owner) {
        bail!("Agent owner conflicts with directory");
    }
    if value.agent.revision<0 || !metadata.is_object()
        || value.connections.iter().any(|row|row.agent_id!=id || row.revision<1)
        || value.prompts.iter().any(|row|row.agent_id!=id || row.revision<1) {
        bail!("Invalid agent record");
    }
    for row in &value.connections {
        store.unseal(&row.config)?;
    }
    for row in &value.prompts {
        store.unseal(&row.content)?;
    }
    let prefix=format!("agent:{id}:");
    let root_key=format!("agent:{id}");
    let ability_key=format!("agent_abilities:{id}");
    for row in &value.docs {
        if !row.key.starts_with(&prefix) && row.key!=root_key && row.key!=ability_key {
            bail!("Foreign agent document");
        }
        store.unseal(&row.value)?;
    }
    let agent_count:i64=sqlx::query_scalar("SELECT count(*) FROM agents")
        .fetch_one(pool).await?;
    let conn_count:i64=sqlx::query_scalar("SELECT count(*) FROM agent_connections")
        .fetch_one(pool).await?;
    let prompt_count:i64=sqlx::query_scalar("SELECT count(*) FROM agent_prompts")
        .fetch_one(pool).await?;
    let doc_count:i64=sqlx::query_scalar("SELECT count(*) FROM docs")
        .fetch_one(pool).await?;
    if agent_count!=1 || conn_count!=value.connections.len() as i64
        || prompt_count!=value.prompts.len() as i64 || doc_count!=value.docs.len() as i64 {
        bail!("Agent database contains foreign rows");
    }
    Ok(())
}

impl Store {
    pub async fn ensure_default_agent_directory(&self) -> Result<()> {
        sqlx::query("CREATE TABLE IF NOT EXISTS agent_directory(
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL)")
            .execute(&self.app_pool).await?;
        let owner:Option<String>=sqlx::query_scalar(
            "SELECT owner_id FROM agent_directory WHERE id='shared_default'")
            .fetch_optional(&self.app_pool).await?;
        if owner.as_deref().is_some_and(|owner| owner!="admin") {
            bail!("Default agent directory conflicts");
        }
        sqlx::query("INSERT OR IGNORE INTO agent_directory(id,owner_id) VALUES('shared_default','admin')")
            .execute(&self.app_pool).await?;
        Ok(())
    }

    pub async fn migrate_agent_storage(&self,root:&Path) -> Result<()> {
        self.ensure_default_agent_directory().await?;
        let rows=sqlx::query("SELECT id FROM agents WHERE id<>'shared_default' ORDER BY id")
            .fetch_all(&self.agent_pool).await?;
        let mut sources=Vec::new();
        for row in rows {
            let id:String=row.get("id");
            if !access::valid_agent_id(&id) {bail!("Invalid legacy agent identity");}
            sources.push(snapshot(&self.agent_pool,&id).await?
                .ok_or_else(||anyhow!("Legacy agent disappeared"))?);
        }
        let digest=hex::encode(Sha256::digest(serde_json::to_vec(&sources)?));
        let marker=self.get(MARKER).await?;
        if let Some(marker)=&marker {
            if marker["version"]!=1 || marker["source_sha256"].as_str()!=Some(digest.as_str()) {
                bail!("Agent migration marker or legacy source changed");
            }
        }
        for source in &sources {
            let id=&source.agent.id;
            let metadata:Value=self.unseal(&source.agent.metadata)?;
            let owner=metadata["owner_id"].as_str()
                .ok_or_else(||anyhow!("Legacy agent owner is missing"))?;
            if !access::valid_id(owner) {bail!("Invalid legacy agent owner");}
            let pool=open_agent_pool(root,id).await?;
            let result=async {
                if marker.is_none() { copy_snapshot(&pool,source).await?; }
                validate_destination(self,&pool,id,owner).await
            }.await;
            pool.close().await;
            result?;
            let existing:Option<String>=sqlx::query_scalar(
                "SELECT owner_id FROM agent_directory WHERE id=?")
                .bind(id).fetch_optional(&self.app_pool).await?;
            if existing.as_deref().is_some_and(|current| current!=owner) {
                bail!("Agent directory conflicts with legacy owner");
            }
            sqlx::query("INSERT OR IGNORE INTO agent_directory(id,owner_id) VALUES(?,?)")
                .bind(id).bind(owner).execute(&self.app_pool).await?;
        }
        let index_rows=sqlx::query("SELECT id FROM agent_directory ORDER BY id")
            .fetch_all(&self.app_pool).await?;
        let expected=std::iter::once("shared_default".to_owned())
            .chain(sources.iter().map(|s|s.agent.id.clone()))
            .collect::<std::collections::BTreeSet<_>>();
        let actual=index_rows.into_iter().map(|row|row.get::<String,_>("id"))
            .collect::<std::collections::BTreeSet<_>>();
        // New agents exist only in their own DB after migration and are not in
        // the legacy source. Their directory entries must remain intact.
        if !expected.is_subset(&actual) {bail!("Agent directory is incomplete");}
        for id in actual {
            if id=="shared_default" {continue;}
            let owner:String=sqlx::query_scalar(
                "SELECT owner_id FROM agent_directory WHERE id=?")
                .bind(&id).fetch_one(&self.app_pool).await?;
            let pool=open_agent_pool(root,&id).await?;
            let result=validate_destination(self,&pool,&id,&owner).await;
            pool.close().await;
            result?;
        }
        if marker.is_none() {
            self.put(MARKER,&json!({"version":1,"source_sha256":digest})).await?;
        }
        Ok(())
    }

    pub async fn agent_ids(&self) -> Result<Vec<String>> {
        let rows=sqlx::query("SELECT id FROM agent_directory ORDER BY id")
            .fetch_all(&self.app_pool).await?;
        Ok(rows.into_iter().map(|row|row.get("id")).collect())
    }

    pub async fn register_agent(&self,id:&str,owner:&str) -> Result<()> {
        if id=="shared_default" || !access::valid_agent_id(id) || !access::valid_id(owner) {
            bail!("Invalid agent registration");
        }
        let pool=open_agent_pool(&self.root,id).await?;
        let verified=validate_destination(self,&pool,id,owner).await;
        pool.close().await;
        verified?;
        if self.account_by_id(owner).await?.is_none() {
            bail!("Agent owner account not found");
        }
        let existing:Option<String>=sqlx::query_scalar(
            "SELECT owner_id FROM agent_directory WHERE id=?")
            .bind(id).fetch_optional(&self.app_pool).await?;
        if existing.as_deref().is_some_and(|current|current!=owner) {
            bail!("Agent owner conflict");
        }
        sqlx::query("INSERT OR IGNORE INTO agent_directory(id,owner_id) VALUES(?,?)")
            .bind(id).bind(owner).execute(&self.app_pool).await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn split_preserves_source_and_reopens_with_matching_records() {
        let dir=tempfile::tempdir().unwrap();
        let store=Store::open(dir.path()).await.unwrap();
        let metadata=store.seal(&json!({"owner_id":"admin","ability_revision":0})).unwrap();
        sqlx::query("INSERT INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES('agent_1','Second','','openai_subscription','gpt-6-luna','active',?,1,1,1)")
            .bind(metadata).execute(&store.agent_pool).await.unwrap();
        let source=snapshot(&store.agent_pool,"agent_1").await.unwrap().unwrap();
        store.migrate_agent_storage(dir.path()).await.unwrap();
        let destination=open_agent_pool(dir.path(),"agent_1").await.unwrap();
        assert_eq!(snapshot(&destination,"agent_1").await.unwrap().unwrap(),source);
        sqlx::query("UPDATE agents SET model='gpt-6-sol',revision=revision+1 WHERE id='agent_1'")
            .execute(&destination).await.unwrap();
        destination.close().await;
        store.migrate_agent_storage(dir.path()).await.unwrap();
        assert!(snapshot(&store.agent_pool,"agent_1").await.unwrap().is_some());
    }
    #[tokio::test]
    async fn agent_views_isolate_config_and_preserve_legacy_source() {
        let dir=tempfile::tempdir().unwrap();
        let store=Store::open(dir.path()).await.unwrap();
        store.create_admin_account("root","fixture-hash","generation").await.unwrap();
        let metadata=store.seal(&json!({"owner_id":"admin","ability_revision":0})).unwrap();
        sqlx::query("INSERT INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES('agent_4','Fourth','','openai_subscription','model-old','active',?,1,1,1)")
            .bind(metadata).execute(&store.agent_pool).await.unwrap();
        store.migrate_agent_storage(dir.path()).await.unwrap();
        let agent=store.for_agent("agent_4").await.unwrap();
        assert_eq!(agent.current_agent_id(),"agent_4");
        assert_eq!(agent.agent_model().await.unwrap()["model"],"model-old");
        assert!(store.agent_abilities_for("agent_4").await.is_err());
        agent.save_agent_model(&json!({"provider":"openai_subscription",
            "model":"model-new"})).await.unwrap();
        assert_eq!(agent.agent_model().await.unwrap()["model"],"model-new");
        assert_ne!(store.agent_model().await.unwrap()["model"],"model-new");
        let source=snapshot(&store.agent_pool,"agent_4").await.unwrap().unwrap();
        assert_eq!(source.agent.model,"model-old");
        store.migrate_agent_storage(dir.path()).await.unwrap();
    }
    #[tokio::test]
    async fn reopen_rejects_malformed_destination_after_marker() {
        let dir=tempfile::tempdir().unwrap();
        let store=Store::open(dir.path()).await.unwrap();
        let metadata=store.seal(&json!({"owner_id":"admin"})).unwrap();
        sqlx::query("INSERT INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES('agent_3','Third','','openai_subscription','gpt-6-luna','active',?,1,1,1)")
            .bind(metadata).execute(&store.agent_pool).await.unwrap();
        store.migrate_agent_storage(dir.path()).await.unwrap();
        let destination=open_agent_pool(dir.path(),"agent_3").await.unwrap();
        sqlx::query("UPDATE agents SET metadata=x'00' WHERE id='agent_3'")
            .execute(&destination).await.unwrap();
        destination.close().await;
        assert!(store.migrate_agent_storage(dir.path()).await.is_err());
    }
    #[tokio::test]
    async fn split_rejects_conflicting_destination_without_marker() {
        let dir=tempfile::tempdir().unwrap();
        let store=Store::open(dir.path()).await.unwrap();
        let metadata=store.seal(&json!({"owner_id":"admin"})).unwrap();
        sqlx::query("INSERT INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES('agent_2','Source','','openai_subscription','gpt-6-luna','active',?,1,1,1)")
            .bind(&metadata).execute(&store.agent_pool).await.unwrap();
        let destination=open_agent_pool(dir.path(),"agent_2").await.unwrap();
        sqlx::query("INSERT INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES('agent_2','Conflicting','','openai_subscription','gpt-6-luna','active',?,1,1,1)")
            .bind(metadata).execute(&destination).await.unwrap();
        destination.close().await;
        assert!(store.migrate_agent_storage(dir.path()).await.is_err());
        assert!(store.get(MARKER).await.unwrap().is_none());
        let retry=open_agent_pool(dir.path(),"agent_2").await.unwrap();
        sqlx::query("DELETE FROM agents WHERE id='agent_2'")
            .execute(&retry).await.unwrap();
        retry.close().await;
        store.migrate_agent_storage(dir.path()).await.unwrap();
        assert!(store.get(MARKER).await.unwrap().is_some());
    }
}
