//! Self-edit writes are scoped to the runner's current agent and commit with retry receipts.
use super::Store;
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use sqlx::{Row, Sqlite, Transaction};

fn valid_name(name: &str) -> bool {
    !name.is_empty() && name.len() <= 64
        && name.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}
fn key(store: &Store) -> String { format!("agent:{}:own_skills", store.current_agent_id()) }
fn receipt_key(store: &Store, pending: &str) -> Result<String> {
    if pending.is_empty() || pending.len() > 128
        || !pending.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
        bail!("Invalid pending call identity");
    }
    Ok(format!("agent:{}:base_self_edit:{pending}", store.current_agent_id()))
}
fn request_hash(command: &Value) -> Result<String> {
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(command)?)))
}
async fn previous(store: &Store, tx: &mut Transaction<'_, Sqlite>,
    receipt_key: &str, hash: &str) -> Result<Option<Value>> {
    let row = sqlx::query("SELECT value FROM docs WHERE key=?").bind(receipt_key)
        .fetch_optional(&mut **tx).await?;
    let Some(row) = row else { return Ok(None) };
    let value = store.unseal(&row.get::<Vec<u8>, _>("value"))?;
    if value["request_hash"].as_str() != Some(hash) { bail!("Pending call identity reused"); }
    Ok(Some(value["result"].clone()))
}
async fn record(store: &Store, tx: &mut Transaction<'_, Sqlite>,
    receipt_key: &str, hash: &str, result: &Value) -> Result<()> {
    sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)")
        .bind(receipt_key).bind(store.seal(&json!({"request_hash":hash,"result":result}))?)
        .execute(&mut **tx).await?;
    Ok(())
}
fn parse(value: Option<Value>) -> Result<(i64, Vec<Value>)> {
    let Some(value) = value else { return Ok((0, Vec::new())) };
    let revision = value["revision"].as_i64().filter(|r| *r > 0)
        .ok_or_else(|| anyhow!("Invalid own-skill revision"))?;
    let skills = value["skills"].as_array().ok_or_else(|| anyhow!("Invalid own-skill list"))?.clone();
    validate(&skills)?;
    Ok((revision, skills))
}
fn validate(skills: &[Value]) -> Result<()> {
    if skills.len() > 32 { bail!("Own-skill count exceeds limit"); }
    let mut names = std::collections::HashSet::new();
    let mut total = 0usize;
    for skill in skills {
        let name = skill["name"].as_str().filter(|n| valid_name(n))
            .ok_or_else(|| anyhow!("Invalid own-skill name"))?;
        if !names.insert(name) { bail!("Duplicate own-skill name"); }
        let description = skill["description"].as_str()
            .filter(|s| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control))
            .ok_or_else(|| anyhow!("Invalid own-skill description"))?;
        let body = skill["instructions"].as_str()
            .filter(|s| !s.trim().is_empty() && s.len() <= 16 * 1024)
            .ok_or_else(|| anyhow!("Invalid own-skill instructions"))?;
        if skill["mode"]!="selectable"
            || skill["revision"].as_i64().filter(|r| *r > 0).is_none() {
            bail!("Invalid own-skill mode or revision");
        }
        total = total.checked_add(description.len() + body.len())
            .ok_or_else(|| anyhow!("Own-skill content exceeds limit"))?;
        if total > 64 * 1024 { bail!("Own-skill content exceeds limit"); }
    }
    Ok(())
}
impl Store {
    pub async fn list_own_skills(&self) -> Result<Value> {
        let (revision, skills) = parse(self.get(&key(self)).await?)?;
        Ok(json!({"revision":revision,"skills":skills}))
    }
    pub async fn load_own_skill(&self, name: &str) -> Result<Option<Value>> {
        if !valid_name(name) { bail!("Invalid own-skill name"); }
        Ok(self.list_own_skills().await?["skills"].as_array().unwrap().iter()
            .find(|s| s["name"] == name).cloned())
    }
    pub async fn mutate_own_skill(&self, name: &str, description: Option<&str>,
        instructions: Option<&str>, mode: Option<&str>, expected_revision: i64,
        remove: bool, pending: &str, scope: &str) -> Result<Value> {
        if !valid_name(name) || expected_revision < 0 || remove && expected_revision == 0
            || mode.is_some_and(|m| m!="selectable") {
            bail!("Invalid own-skill selector or revision");
        }
        let command = json!(["skill",scope,name,description,instructions,mode,expected_revision,remove]);
        let hash = request_hash(&command)?;
        let receipt = receipt_key(self,pending)?;
        let key = key(self);
        let mut tx = self.agent_pool.begin().await?;
        sqlx::query("UPDATE docs SET value=value WHERE 0").execute(&mut *tx).await?;
        if let Some(result) = previous(self,&mut tx,&receipt,&hash).await? { return Ok(result); }
        let row = sqlx::query("SELECT value FROM docs WHERE key=?").bind(&key)
            .fetch_optional(&mut *tx).await?;
        let (catalog_revision,mut skills) = parse(row.as_ref()
            .map(|r| self.unseal(&r.get::<Vec<u8>, _>("value"))).transpose()?)?;
        let index = skills.iter().position(|s| s["name"] == name);
        let actual = index.map(|i| skills[i]["revision"].as_i64().unwrap()).unwrap_or(0);
        if actual != expected_revision { bail!("Own-skill revision mismatch"); }
        let result = if remove {
            if let Some(index) = index { skills.remove(index); } else { bail!("Own-skill not found"); }
            json!({"name":name,"removed":true})
        } else {
            let old = index.map(|i| &skills[i]);
            let description = description.or_else(|| old.and_then(|s|s["description"].as_str()))
                .ok_or_else(|| anyhow!("Own-skill description required"))?;
            let instructions = instructions.or_else(|| old.and_then(|s|s["instructions"].as_str()))
                .ok_or_else(|| anyhow!("Own-skill instructions required"))?;
            let mode = mode.or_else(|| old.and_then(|s|s["mode"].as_str())).unwrap_or("selectable");
            let revision = expected_revision.checked_add(1)
                .ok_or_else(|| anyhow!("Invalid own-skill revision"))?;
            let skill = json!({"name":name,"description":description,"instructions":instructions,
                "mode":mode,"revision":revision});
            if let Some(index) = index { skills[index] = skill.clone(); } else { skills.push(skill.clone()); }
            json!({"skill":skill,"created":index.is_none()})
        };
        validate(&skills)?;
        let catalog_revision = catalog_revision.checked_add(1)
            .ok_or_else(|| anyhow!("Invalid own-skill revision"))?;
        let saved = self.seal(&json!({"revision":catalog_revision,"skills":skills}))?;
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(&key).bind(saved).execute(&mut *tx).await?;
        let result = json!({"status":"success","output":result,"catalog_revision":catalog_revision});
        record(self,&mut tx,&receipt,&hash,&result).await?;
        tx.commit().await?;
        Ok(result)
    }
}
// The lock and revision are inspected under the same SQLite writer reservation
// as the edit, so an administrator can lock a slot without a self-edit race.
impl Store {
    pub async fn self_edit_prompt_slot(&self, name: &str, content: &str,
        mode: &str, expected_revision: i64, pending: &str, scope: &str) -> Result<Value> {
        if name.is_empty() || name.len() > 128 || name.trim() != name
            || name.starts_with("__") || name.chars().any(char::is_control)
            || expected_revision < 0 || !matches!(mode, "replace" | "append")
            || content.is_empty() || content.len() > 16 * 1024 {
            bail!("Invalid self-edit prompt arguments");
        }
        let command = json!(["prompt",scope,name,content,mode,expected_revision]);
        let hash = request_hash(&command)?;
        let receipt = receipt_key(self,pending)?;
        let mut tx = self.agent_pool.begin().await?;
        sqlx::query("UPDATE agent_prompts SET revision=revision WHERE 0")
            .execute(&mut *tx).await?;
        if let Some(result) = previous(self,&mut tx,&receipt,&hash).await? { return Ok(result); }
        let rows = sqlx::query("SELECT slot_name,order_index,lock,content,revision
            FROM agent_prompts WHERE agent_id=?")
            .bind(self.current_agent_id()).fetch_all(&mut *tx).await?;
        let existing = rows.iter().find(|row| row.get::<String, _>("slot_name") == name);
        let actual = existing.map(|row| row.get::<i64, _>("revision")).unwrap_or(0);
        if actual != expected_revision { bail!("Prompt slot revision mismatch"); }
        if existing.is_some_and(|row| row.get::<i64, _>("lock") != 0) {
            bail!("Prompt slot is locked by agent administration");
        }
        if existing.is_none() && rows.len() >= 32 { bail!("Prompt slot count exceeds limit"); }
        let old = existing.map(|row| self.unseal(&row.get::<Vec<u8>, _>("content")))
            .transpose()?.and_then(|v| v.as_str().map(str::to_owned)).unwrap_or_default();
        let next_content = if mode == "append" && !old.is_empty() {
            format!("{old}\n\n{content}")
        } else { content.to_owned() };
        if next_content.len() > 16 * 1024 { bail!("Prompt slot content exceeds limit"); }
        let mut total = next_content.len();
        for row in &rows {
            if row.get::<String, _>("slot_name") == name { continue; }
            let value = self.unseal(&row.get::<Vec<u8>, _>("content"))?;
            total = total.checked_add(value.as_str().ok_or_else(|| anyhow!("Invalid prompt slot content"))?.len())
                .ok_or_else(|| anyhow!("Prompt slot total exceeds limit"))?;
        }
        if total > 64 * 1024 { bail!("Prompt slot total exceeds limit"); }
        let order = existing.map(|row| row.get::<i64, _>("order_index"))
            .unwrap_or_else(|| rows.iter().map(|row| row.get::<i64, _>("order_index")).max().unwrap_or(0) + 10);
        if !(0..=1_000_000).contains(&order) { bail!("Prompt slot order exceeds limit"); }
        let revision = expected_revision.checked_add(1)
            .ok_or_else(|| anyhow!("Invalid prompt slot revision"))?;
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs() as i64;
        let sealed = self.seal(&json!(next_content))?;
        if existing.is_some() {
            let updated = sqlx::query("UPDATE agent_prompts SET content=?,revision=?,updated_at=?
                WHERE agent_id=? AND slot_name=? AND revision=? AND lock=0")
                .bind(sealed).bind(revision).bind(now).bind(self.current_agent_id())
                .bind(name).bind(expected_revision).execute(&mut *tx).await?.rows_affected();
            if updated != 1 { bail!("Prompt slot changed or locked"); }
        } else {
            sqlx::query("INSERT INTO agent_prompts(id,agent_id,slot_name,order_index,lock,merge_mode,content,revision,updated_at)
                VALUES(?,?,?,?,0,'replace',?,?,?)")
                .bind(format!("{}:{name}", self.current_agent_id())).bind(self.current_agent_id())
                .bind(name).bind(order).bind(sealed).bind(revision).bind(now)
                .execute(&mut *tx).await?;
        }
        let result = json!({"status":"success","output":{"slot_name":name,
            "revision":revision,"created":existing.is_none(),"mode":mode,"takes_effect":"next_run"}});
        record(self,&mut tx,&receipt,&hash,&result).await?;
        tx.commit().await?;
        Ok(result)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn self_edits_obey_lock_revision_and_retry_receipt() {
        let (_root, app) = crate::runtime_tests::fixture().await;
        let store = &app.store;
        let first = store.self_edit_prompt_slot("self_notes","hello","replace",0,"call_one","run_scope")
            .await.unwrap();
        assert_eq!(first["output"]["revision"],1);
        assert_eq!(store.self_edit_prompt_slot("self_notes","hello","replace",0,"call_one","run_scope")
            .await.unwrap(),first);
        assert!(store.self_edit_prompt_slot("self_notes","different","replace",0,"call_one","run_scope")
            .await.is_err());
        assert!(store.self_edit_prompt_slot("self_notes","stale","replace",0,"call_two","run_scope")
            .await.is_err());
        let locked = store.upsert_prompt_slot("self_notes","admin lock",None,Some(true),None,1)
            .await.unwrap();
        assert_eq!(locked["revision"],2);
        assert!(store.self_edit_prompt_slot("self_notes","overwrite","replace",2,"call_three","run_scope")
            .await.is_err());
        assert_eq!(store.get_prompt_slot("self_notes").await.unwrap().unwrap()["content"],"admin lock");
    }
    #[tokio::test]
    async fn own_skills_are_revisioned_and_remove_only_exact_agent_skill() {
        let (_root, app) = crate::runtime_tests::fixture().await;
        let store = &app.store;
        let first = store.mutate_own_skill("test_steps",Some("when testing"),
            Some("Do the thing"),Some("selectable"),0,false,"skill_one","run_scope")
            .await.unwrap();
        assert_eq!(first["output"]["skill"]["revision"],1);
        assert_eq!(store.mutate_own_skill("test_steps",Some("when testing"),
            Some("Do the thing"),Some("selectable"),0,false,"skill_one","run_scope")
            .await.unwrap(),first);
        assert!(store.mutate_own_skill("test_steps",None,Some("stale"),None,0,false,
            "skill_two","run_scope").await.is_err());
        assert_eq!(store.load_own_skill("test_steps").await.unwrap().unwrap()["instructions"],"Do the thing");
        assert!(store.mutate_own_skill("test_steps",None,None,None,1,true,
            "skill_one","different_run").await.is_err());
        store.mutate_own_skill("test_steps",None,None,None,1,true,
            "skill_three","run_scope").await.unwrap();
        assert!(store.load_own_skill("test_steps").await.unwrap().is_none());
    }
}
