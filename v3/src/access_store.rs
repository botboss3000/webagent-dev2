//! Application-wide records for who people are, what their tier permits, and
//! which agents they may use. Conversations themselves belong to user storage.
use crate::access::{self, AccountStatus, Principal, Relationship, Role, Tier};
use crate::store::Store;
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};
use sqlx::Row;

fn account_view(value: &Value) -> Result<Value> {
    let principal = Principal::from_account(value)?;
    Ok(json!({"user_id":principal.user_id,"username":value["username"],
        "status":principal.status,"role":principal.role,
        "tier_id":principal.tier_id,"access_revision":principal.access_revision}))
}
fn username_ok(name: &str) -> bool {
    !name.is_empty() && name.len() <= 128 && name.trim() == name
        && !name.chars().any(char::is_control)
}
async fn audit(tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>, store: &Store,
    kind: &str, payload: &Value) -> Result<()> {
    sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES('',?,?)")
        .bind(kind).bind(store.seal(payload)?).execute(&mut **tx).await?;
    Ok(())
}

impl Store {
    pub async fn access_audit(&self, limit: u32) -> Result<Vec<Value>> {
        if limit == 0 || limit > 200 { bail!("Invalid audit limit"); }
        let rows = sqlx::query("SELECT seq,kind,payload FROM events
            WHERE session_id='' AND kind IN ('account_created',
                'account_access_changed','tier_changed','agent_membership_changed',
                'profile_updated','password_changed',
                'agent_status_changed','agent_tool_permissions_changed',
                'agent_tool_callers_changed','agent_ability_permissions_changed')
            ORDER BY seq DESC LIMIT ?")
            .bind(limit as i64).fetch_all(&self.app_pool).await?;
        rows.iter().map(|row| {
            Ok(json!({"seq":row.get::<i64,_>("seq"),"kind":row.get::<String,_>("kind"),
                "detail":self.unseal(&row.get::<Vec<u8>,_>("payload"))?}))
        }).collect()
    }
    pub async fn all_user_ids(&self) -> Result<Vec<String>> {
        let rows = sqlx::query("SELECT user_id FROM user_accounts ORDER BY user_id")
            .fetch_all(&self.app_pool).await?;
        Ok(rows.into_iter().map(|row| row.get::<String,_>("user_id")).collect())
    }
    pub async fn access_accounts(&self) -> Result<Vec<Value>> {
        let rows = sqlx::query("SELECT value FROM user_accounts ORDER BY username LIMIT 1000")
            .fetch_all(&self.app_pool).await?;
        rows.iter().map(|row| account_view(&self.unseal(&row.get::<Vec<u8>,_>("value"))?))
            .collect()
    }
    pub async fn create_user_account(&self, username: &str, password_hash: &str) -> Result<Value> {
        if !username_ok(username) || password_hash.len() < 20 || password_hash.len() > 1024 {
            bail!("Invalid account input");
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        let account = json!({"user_id":id,"username":username,"password_hash":password_hash,
            "generation":uuid::Uuid::new_v4().to_string(),"is_approved":false,
            "status":AccountStatus::Pending,"role":Role::User,"tier_id":"basic",
            "access_revision":1});
        let profile = json!({"user_id":id,"display_name":username,"is_admin":false,
            "default_agent_id":null,"last_login_at":null});
        let mut tx = self.app_pool.begin().await?;
        sqlx::query("INSERT INTO user_accounts(user_id,username,value) VALUES(?,?,?)")
            .bind(&id).bind(username).bind(self.seal(&account)?)
            .execute(&mut *tx).await?;
        sqlx::query("INSERT INTO user_profiles(user_id,value) VALUES(?,?)")
            .bind(&id).bind(self.seal(&profile)?).execute(&mut *tx).await?;
        audit(&mut tx,self,"account_created",&json!({"user_id":id,"status":"pending"})).await?;
        tx.commit().await?;
        account_view(&account)
    }
    pub async fn update_account_access(&self, id: &str, expected_revision: u64,
        status: AccountStatus, role: Role, tier_id: &str) -> Result<Value> {
        if !access::valid_id(id) || !access::valid_id(tier_id) || expected_revision == 0 {
            bail!("Invalid account update");
        }
        if id == "admin" && (status != AccountStatus::Active
            || role != Role::Administrator || tier_id != "operator") {
            bail!("The original administrator must remain active");
        }
        let mut tx = self.app_pool.begin().await?;
        sqlx::query("UPDATE user_accounts SET value=value WHERE user_id=?")
            .bind(id).execute(&mut *tx).await?;
        let blob: Vec<u8> = sqlx::query_scalar("SELECT value FROM user_accounts WHERE user_id=?")
            .bind(id).fetch_optional(&mut *tx).await?
            .ok_or_else(|| anyhow!("Account not found"))?;
        let mut account = self.unseal(&blob)?;
        if Principal::from_account(&account)?.access_revision != expected_revision {
            bail!("Account revision changed");
        }
        let tier: Option<i64> = sqlx::query_scalar(
            "SELECT 1 FROM entitlement_tiers WHERE tier_id=?")
            .bind(tier_id).fetch_optional(&mut *tx).await?;
        if tier.is_none() { bail!("Entitlement tier not found"); }
        account["status"] = json!(status);
        account["role"] = json!(role);
        account["tier_id"] = json!(tier_id);
        account["access_revision"] = json!(expected_revision + 1);
        account["generation"] = json!(uuid::Uuid::new_v4().to_string());
        account["is_approved"] = json!(status == AccountStatus::Active);
        sqlx::query("UPDATE user_accounts SET value=? WHERE user_id=?")
            .bind(self.seal(&account)?).bind(id).execute(&mut *tx).await?;
        let profile_blob: Vec<u8> = sqlx::query_scalar(
            "SELECT value FROM user_profiles WHERE user_id=?")
            .bind(id).fetch_one(&mut *tx).await?;
        let mut profile = self.unseal(&profile_blob)?;
        profile["is_admin"] = json!(status == AccountStatus::Active
            && role == Role::Administrator);
        sqlx::query("UPDATE user_profiles SET value=? WHERE user_id=?")
            .bind(self.seal(&profile)?).bind(id).execute(&mut *tx).await?;
        audit(&mut tx,self,"account_access_changed",&json!({"user_id":id,
            "status":status,"role":role,"tier_id":tier_id,
            "access_revision":expected_revision+1})).await?;
        tx.commit().await?;
        account_view(&account)
    }
    pub async fn entitlement_tier(&self, id: &str) -> Result<Option<Tier>> {
        if !access::valid_id(id) { bail!("Invalid tier ID"); }
        let row = sqlx::query("SELECT value FROM entitlement_tiers WHERE tier_id=?")
            .bind(id).fetch_optional(&self.app_pool).await?;
        row.map(|row| {
            let tier: Tier = serde_json::from_value(self.unseal(&row.get::<Vec<u8>,_>("value"))?)?;
            tier.validate()?;
            Ok(tier)
        }).transpose()
    }
    pub async fn entitlement_tiers(&self) -> Result<Vec<Tier>> {
        let rows = sqlx::query("SELECT value FROM entitlement_tiers ORDER BY tier_id LIMIT 100")
            .fetch_all(&self.app_pool).await?;
        rows.iter().map(|row| {
            let tier: Tier = serde_json::from_value(self.unseal(&row.get::<Vec<u8>,_>("value"))?)?;
            tier.validate()?;
            Ok(tier)
        }).collect()
    }
    pub async fn save_entitlement_tier(&self, mut tier: Tier,
        expected_revision: u64) -> Result<Tier> {
        if tier.id == "operator" { bail!("Operator tier is immutable"); }
        tier.revision = expected_revision.checked_add(1)
            .ok_or_else(|| anyhow!("Tier revision overflow"))?;
        tier.validate()?;
        let mut tx = self.app_pool.begin().await?;
        sqlx::query("UPDATE entitlement_tiers SET value=value WHERE tier_id=?")
            .bind(&tier.id).execute(&mut *tx).await?;
        let current: Option<i64> = sqlx::query_scalar(
            "SELECT revision FROM entitlement_tiers WHERE tier_id=?")
            .bind(&tier.id).fetch_optional(&mut *tx).await?;
        if current.map(|value|value as u64).unwrap_or(0) != expected_revision {
            bail!("Tier revision changed");
        }
        sqlx::query("INSERT INTO entitlement_tiers(tier_id,revision,value) VALUES(?,?,?)
            ON CONFLICT(tier_id) DO UPDATE SET revision=excluded.revision,value=excluded.value")
            .bind(&tier.id).bind(tier.revision as i64)
            .bind(self.seal(&serde_json::to_value(&tier)?)?)
            .execute(&mut *tx).await?;
        let rows = sqlx::query("SELECT user_id,value FROM user_accounts")
            .fetch_all(&mut *tx).await?;
        for row in rows {
            let mut account = self.unseal(&row.get::<Vec<u8>,_>("value"))?;
            if account["tier_id"] != tier.id { continue; }
            let revision = Principal::from_account(&account)?.access_revision;
            account["access_revision"] = json!(revision + 1);
            account["generation"] = json!(uuid::Uuid::new_v4().to_string());
            sqlx::query("UPDATE user_accounts SET value=? WHERE user_id=?")
                .bind(self.seal(&account)?).bind(row.get::<String,_>("user_id"))
                .execute(&mut *tx).await?;
        }
        audit(&mut tx,self,"tier_changed",&json!({"tier_id":tier.id,
            "revision":tier.revision})).await?;
        tx.commit().await?;
        Ok(tier)
    }
    pub async fn agent_membership(&self, user_id: &str, agent_id: &str) -> Result<Option<Value>> {
        if !access::valid_id(user_id) || !access::valid_agent_id(agent_id) {
            bail!("Invalid membership identity");
        }
        let row = sqlx::query("SELECT value FROM agent_memberships WHERE user_id=? AND agent_id=?")
            .bind(user_id).bind(agent_id).fetch_optional(&self.app_pool).await?;
        row.map(|row| self.unseal(&row.get::<Vec<u8>,_>("value"))).transpose()
    }
    pub async fn set_agent_membership(&self, user_id: &str, agent_id: &str,
        relationship: Relationship, expected_revision: u64) -> Result<Value> {
        if !access::valid_id(user_id) || !access::valid_agent_id(agent_id) {
            bail!("Invalid membership identity");
        }
        if user_id == "admin" && agent_id == "shared_default"
            && relationship != Relationship::Owner {
            bail!("The original administrator must retain its agent");
        }
        let agent_view = self.for_agent(agent_id).await?;
        let agent: Option<String> = sqlx::query_scalar("SELECT status FROM agents WHERE id=?")
            .bind(agent_id).fetch_optional(&agent_view.agent_pool).await?;
        if agent.is_none() { bail!("Agent not found"); }
        let owner: Option<String> = sqlx::query_scalar(
            "SELECT owner_id FROM agent_directory WHERE id=?")
            .bind(agent_id).fetch_optional(&self.app_pool).await?;
        let owner = owner.ok_or_else(||anyhow!("Agent is not registered"))?;
        if (owner == user_id && relationship != Relationship::Owner)
            || (owner != user_id && relationship == Relationship::Owner) {
            bail!("Agent owner relationship cannot be changed here");
        }
        let mut tx = self.app_pool.begin().await?;
        sqlx::query("UPDATE user_accounts SET value=value WHERE user_id=?")
            .bind(user_id).execute(&mut *tx).await?;
        let account_blob: Vec<u8> = sqlx::query_scalar(
            "SELECT value FROM user_accounts WHERE user_id=?")
            .bind(user_id).fetch_optional(&mut *tx).await?
            .ok_or_else(|| anyhow!("Account not found"))?;
        let row: Option<Vec<u8>> = sqlx::query_scalar(
            "SELECT value FROM agent_memberships WHERE user_id=? AND agent_id=?")
            .bind(user_id).bind(agent_id).fetch_optional(&mut *tx).await?;
        let current = row.as_ref().map(|row| self.unseal(row)).transpose()?;
        if current.as_ref().and_then(|value|value["revision"].as_u64()).unwrap_or(0)
            != expected_revision { bail!("Membership revision changed"); }
        let revision = expected_revision.checked_add(1)
            .ok_or_else(|| anyhow!("Membership revision overflow"))?;
        let value = json!({"user_id":user_id,"agent_id":agent_id,
            "relationship":relationship,"revision":revision});
        sqlx::query("INSERT INTO agent_memberships(user_id,agent_id,value) VALUES(?,?,?)
            ON CONFLICT(user_id,agent_id) DO UPDATE SET value=excluded.value")
            .bind(user_id).bind(agent_id).bind(self.seal(&value)?)
            .execute(&mut *tx).await?;
        let mut account = self.unseal(&account_blob)?;
        let old = Principal::from_account(&account)?.access_revision;
        account["access_revision"] = json!(old + 1);
        account["generation"] = json!(uuid::Uuid::new_v4().to_string());
        sqlx::query("UPDATE user_accounts SET value=? WHERE user_id=?")
            .bind(self.seal(&account)?).bind(user_id).execute(&mut *tx).await?;
        audit(&mut tx,self,"agent_membership_changed",&json!({"user_id":user_id,
            "agent_id":agent_id,"relationship":relationship,"revision":revision})).await?;
        tx.commit().await?;
        Ok(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn new_user_is_pending_and_access_revisions_revoke_credentials() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store.create_admin_account("root","long-hash-placeholder","generation")
            .await.unwrap();
        let created = store.create_user_account("second","long-hash-placeholder").await.unwrap();
        let id = created["user_id"].as_str().unwrap();
        assert_eq!(created["status"], "pending");
        assert_eq!(created["tier_id"], "basic");
        let before = store.account_by_id(id).await.unwrap().unwrap();
        let active = store.update_account_access(id,1,AccountStatus::Active,
            Role::User,"basic").await.unwrap();
        assert_eq!(active["access_revision"],2);
        let after = store.account_by_id(id).await.unwrap().unwrap();
        assert_ne!(before["generation"],after["generation"]);
        assert!(store.update_account_access(id,1,AccountStatus::Suspended,
            Role::User,"basic").await.is_err());
        assert!(store.update_account_access("admin",1,AccountStatus::Disabled,
            Role::User,"basic").await.is_err());
        store.app_pool.close().await;
        store.pool.close().await;
        store.agent_pool.close().await;
        let reopened = Store::open(root.path()).await.unwrap();
        assert_eq!(reopened.account_by_id(id).await.unwrap().unwrap()["status"],"active");
    }
    #[tokio::test]
    async fn user_planes_are_isolated_and_admin_path_is_stable() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store.create_admin_account("root","long-hash-placeholder","generation")
            .await.unwrap();
        let first = store.create_user_account("first","long-hash-placeholder").await.unwrap();
        let second = store.create_user_account("second","long-hash-placeholder").await.unwrap();
        let first_id = first["user_id"].as_str().unwrap();
        let second_id = second["user_id"].as_str().unwrap();
        let one = store.for_user(first_id).await.unwrap();
        let two = store.for_user(second_id).await.unwrap();
        one.put("session:shared", &json!({"owner":first_id})).await.unwrap();
        two.put("session:shared", &json!({"owner":second_id})).await.unwrap();
        one.put("receipt:session:same-request", &json!({"owner":first_id})).await.unwrap();
        two.put("receipt:session:same-request", &json!({"owner":second_id})).await.unwrap();
        assert_eq!(one.get("receipt:session:same-request").await.unwrap().unwrap()["owner"],first_id);
        assert_eq!(two.get("receipt:session:same-request").await.unwrap().unwrap()["owner"],second_id);
        assert_eq!(one.get("session:shared").await.unwrap().unwrap()["owner"],first_id);
        assert_eq!(two.get("session:shared").await.unwrap().unwrap()["owner"],second_id);
        assert!(store.get("session:shared").await.unwrap().is_none());
        assert_eq!(store.current_user_id(),"admin");
        assert!(root.path().join("user_data/admin/admin.db").exists());
    }
    #[tokio::test]
    async fn tier_and_membership_changes_bump_access_revision() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store.create_admin_account("root","long-hash-placeholder","generation")
            .await.unwrap();
        let created = store.create_user_account("second","long-hash-placeholder").await.unwrap();
        let id = created["user_id"].as_str().unwrap();
        let tier = store.save_entitlement_tier(Tier{id:"team".into(),revision:0,
            allowed_models:vec!["gpt-6-luna".into()],tools:Default::default(),
            max_sessions:4,max_active_runs:1},0).await.unwrap();
        assert_eq!(tier.revision,1);
        store.update_account_access(id,1,AccountStatus::Active,Role::User,"team")
            .await.unwrap();
        let membership = store.set_agent_membership(id,"shared_default",
            Relationship::Caller,0).await.unwrap();
        assert_eq!(membership["revision"],1);
        let revision = store.account_by_id(id).await.unwrap().unwrap()["access_revision"].as_u64().unwrap();
        assert!(revision >= 3);
        store.save_entitlement_tier(Tier{max_sessions:2,..tier},1).await.unwrap();
        let after = store.account_by_id(id).await.unwrap().unwrap()["access_revision"].as_u64().unwrap();
        assert_eq!(after,revision+1);
        assert_eq!(store.agent_membership(id,"shared_default").await.unwrap().unwrap()
            ["relationship"],"caller");
    }
}
