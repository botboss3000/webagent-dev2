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
        let mut accounts=Vec::new();
        for row in rows {
            let mut account=account_view(&self.unseal(&row.get::<Vec<u8>,_>("value"))?)?;
            let id=account["user_id"].as_str().unwrap();
            let profile=self.user_profile(id).await?.unwrap_or_else(||json!({}));
            let view=self.for_user(id).await?;
            let sessions:i64=sqlx::query_scalar("SELECT COUNT(*) FROM docs WHERE key LIKE 'session:%'").fetch_one(&view.pool).await?;
            let messages:i64=sqlx::query_scalar("SELECT COUNT(*) FROM chat").fetch_one(&view.pool).await?;
            account["profile"]=profile;account["session_count"]=json!(sessions);account["message_count"]=json!(messages);
            accounts.push(account);
        }
        Ok(accounts)
    }
    /// Guests are durable independent identities, never password accounts.
    pub async fn create_guest_account(&self) -> Result<Value> {
        let id = format!("guest_{}", uuid::Uuid::new_v4().simple());
        let account = json!({"user_id":id,"username":id,"password_hash":null,
            "generation":uuid::Uuid::new_v4().to_string(),"is_approved":true,
            "status":AccountStatus::Active,"role":Role::Guest,"tier_id":"basic",
            "access_revision":1});
        let profile = json!({"user_id":id,"display_name":"Guest","is_admin":false,
            "default_agent_id":null,"last_login_at":null,"source_channel":"web_public"});
        let mut tx = self.app_pool.begin().await?;
        sqlx::query("INSERT INTO user_accounts(user_id,username,value) VALUES(?,?,?)")
            .bind(&id).bind(&id).bind(self.seal(&account)?).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO user_profiles(user_id,value) VALUES(?,?)")
            .bind(&id).bind(self.seal(&profile)?).execute(&mut *tx).await?;
        audit(&mut tx,self,"account_created",&json!({"user_id":id,"role":"guest"})).await?;
        tx.commit().await?;
        Ok(account)
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
        if (account["role"] == "guest") != (role == Role::Guest) {
            bail!("Guest identity cannot be converted through access administration");
        }
        if Principal::from_account(&account)?.access_revision != expected_revision {
            bail!("Account revision changed");
        }
        if self.entitlement_tier(tier_id).await?.is_none() {bail!("Entitlement tier not found");}
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
        let policy=self.read_access_policy()?;
        policy["tiers"].get(id).map(|value| {
            let tier:Tier=serde_json::from_value(value.clone())?;
            tier.validate()?;
            Ok(tier)
        }).transpose()
    }
    pub async fn entitlement_tiers(&self) -> Result<Vec<Tier>> {
        let policy=self.read_access_policy()?;
        let tiers=policy["tiers"].as_object().ok_or_else(||anyhow!("Policy tiers missing"))?;
        tiers.values().map(|value| {let tier:Tier=serde_json::from_value(value.clone())?;tier.validate()?;Ok(tier)}).collect()
    }
    pub async fn save_entitlement_tier(&self, mut tier: Tier,
        expected_revision: u64) -> Result<Tier> {
        if tier.id == "operator" {
            let mut previous=self.entitlement_tier("operator").await?.ok_or_else(||anyhow!("Operator tier missing"))?;
            previous.ability_permissions=tier.ability_permissions.clone();previous.revision=tier.revision;
            if serde_json::to_value(&previous)?!=serde_json::to_value(&tier)? {
                bail!("Only operator ability grants may change");
            }
        }
        tier.revision=expected_revision.checked_add(1).ok_or_else(||anyhow!("Tier revision overflow"))?;
        tier.validate()?;
        {
            let _lock=self.policy_lock()?;
            let mut policy=self.read_access_policy()?;
            if policy["tiers"][&tier.id]["revision"].as_u64().unwrap_or(0)!=expected_revision {bail!("Tier revision changed");}
            policy["tiers"][&tier.id]=serde_json::to_value(&tier)?;
            policy["revision"]=json!(policy["revision"].as_u64().unwrap_or(0).checked_add(1).ok_or_else(||anyhow!("Policy revision overflow"))?);
            self.write_access_policy(&policy)?;
        }
        // File authority is published first; runtime tier revisions invalidate old grants.
        let mut tx=self.app_pool.begin().await?;
        let rows=sqlx::query("SELECT user_id,value FROM user_accounts").fetch_all(&mut *tx).await?;
        for row in rows {
            let mut account=self.unseal(&row.get::<Vec<u8>,_>("value"))?;
            if account["tier_id"]!=tier.id {continue;}
            let revision=Principal::from_account(&account)?.access_revision;
            account["access_revision"]=json!(revision+1);
            account["generation"]=json!(uuid::Uuid::new_v4().to_string());
            sqlx::query("UPDATE user_accounts SET value=? WHERE user_id=?").bind(self.seal(&account)?).bind(row.get::<String,_>("user_id")).execute(&mut *tx).await?;
        }
        audit(&mut tx,self,"tier_changed",&json!({"tier_id":tier.id,"revision":tier.revision})).await?;
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
    /// Directory ownership is immutable. Agent administration is a separate
    /// membership capability and does not grant ability/tool permissions.
    pub async fn agent_authority(&self, user_id: &str, agent_id: &str) -> Result<Value> {
        if !access::valid_id(user_id) || !access::valid_agent_id(agent_id) {
            bail!("Invalid membership identity");
        }
        let owner: String = sqlx::query_scalar("SELECT owner_id FROM agent_directory WHERE id=?")
            .bind(agent_id).fetch_optional(&self.app_pool).await?
            .ok_or_else(||anyhow!("Agent not found"))?;
        let account = self.account_by_id(user_id).await?
            .ok_or_else(||anyhow!("Account not found"))?;
        let principal = Principal::from_account(&account)?;
        let membership = self.agent_membership(user_id,agent_id).await?;
        if membership.as_ref().is_some_and(|value| value["user_id"] != user_id
            || value["agent_id"] != agent_id || value["revision"].as_u64().is_none_or(|r|r == 0)) {
            bail!("Invalid membership record");
        }
        let relationship: Relationship = membership.as_ref()
            .map(|value|serde_json::from_value(value["relationship"].clone()))
            .transpose()?.unwrap_or(Relationship::Revoked);
        let administrator = membership.as_ref().is_some_and(|value|
            value["agent_administrator"] == true && value["relationship"] == "member");
        Ok(json!({"agent_id":agent_id,"owner_id":owner,"relationship":relationship,
            "revision":membership.as_ref().and_then(|v|v["revision"].as_u64()).unwrap_or(0),
            "agent_administrator":administrator,"installation_administrator":principal.administrator(),
            "can_manage":principal.active() && (owner == user_id || administrator || principal.administrator()),
            "can_use":principal.active() && relationship != Relationship::Revoked}))
    }
    /// Caller must authorize roster access before returning these bounded records.
    pub async fn agent_memberships(&self, agent_id: &str) -> Result<Vec<Value>> {
        if !access::valid_agent_id(agent_id) { bail!("Invalid agent identity"); }
        let exists: Option<String> = sqlx::query_scalar("SELECT id FROM agent_directory WHERE id=?")
            .bind(agent_id).fetch_optional(&self.app_pool).await?;
        if exists.is_none() { bail!("Agent not found"); }
        let rows = sqlx::query("SELECT user_id,value FROM agent_memberships WHERE agent_id=? ORDER BY user_id")
            .bind(agent_id).fetch_all(&self.app_pool).await?;
        rows.iter().map(|row| {
            let value = self.unseal(&row.get::<Vec<u8>,_>("value"))?;
            Ok(json!({"user_id":row.get::<String,_>("user_id"),"agent_id":agent_id,
                "relationship":value["relationship"],"revision":value["revision"],
                "agent_administrator":value["agent_administrator"].as_bool().unwrap_or(false),
                "capability_level_id":crate::capabilities::membership_level(Some(&value),
                    serde_json::from_value(value["relationship"].clone())?)?}))
        }).collect()
    }
    // TENANT-ASSIGNMENT: existing accounts receive revisioned grants; no invitation identity is minted.
    pub async fn assign_agent_membership(&self, actor_id: &str, user_id: &str,
        agent_id: &str, relationship: Relationship, agent_administrator: bool,
        expected_revision: u64) -> Result<Value> {
        if !access::valid_id(actor_id) { bail!("Invalid actor identity"); }
        self.write_agent_membership(user_id,agent_id,relationship,expected_revision,
            Some((actor_id,agent_administrator))).await
    }
    pub async fn set_agent_membership(&self, user_id: &str, agent_id: &str,
        relationship: Relationship, expected_revision: u64) -> Result<Value> {
        // Trusted bootstrap/internal seam; HTTP roster writes use the actor-aware method.
        self.write_agent_membership(user_id,agent_id,relationship,expected_revision,None).await
    }
    // MEMBERSHIP-TRANSACTION: authorize and rotate account access in the same app-plane write.
    async fn write_agent_membership(&self, user_id: &str, agent_id: &str,
        relationship: Relationship, expected_revision: u64,
        actor: Option<(&str,bool)>) -> Result<Value> {
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
        let current_admin = current.as_ref().is_some_and(|v|v["agent_administrator"] == true);
        let administrator = if let Some((actor_id, requested_admin)) = actor {
            // This write transaction serializes authority reads with revocation.
            let blob: Vec<u8> = sqlx::query_scalar("SELECT value FROM user_accounts WHERE user_id=?")
                .bind(actor_id).fetch_optional(&mut *tx).await?
                .ok_or_else(||anyhow!("Actor account not found"))?;
            let principal = Principal::from_account(&self.unseal(&blob)?)?;
            let actor_membership: Option<Vec<u8>> = sqlx::query_scalar(
                "SELECT value FROM agent_memberships WHERE user_id=? AND agent_id=?")
                .bind(actor_id).bind(agent_id).fetch_optional(&mut *tx).await?;
            let actor_membership = actor_membership.as_ref().map(|blob|self.unseal(blob)).transpose()?;
            let delegated = actor_membership.as_ref().is_some_and(|v|
                v["agent_administrator"] == true && v["relationship"] == "member");
            if !principal.active() || !(actor_id == owner || delegated || principal.administrator()) {
                bail!("Agent administration required");
            }
            // Owners appoint administrators. Installation oversight can revoke
            // an administrator but never silently gains ownership or appoints one.
            if requested_admin && actor_id != owner && !current_admin {
                bail!("Only the agent owner can appoint administrators");
            }
            if actor_id != owner && !principal.administrator()
                && (current_admin || requested_admin || user_id == owner) {
                bail!("Agent administrators cannot change privileged memberships");
            }
            if requested_admin && relationship != Relationship::Member {
                bail!("Agent administrator must be a member");
            }
            requested_admin
        } else {
            current_admin && relationship == Relationship::Member
        };
        let revision = expected_revision.checked_add(1)
            .ok_or_else(|| anyhow!("Membership revision overflow"))?;
        let mut value = json!({"user_id":user_id,"agent_id":agent_id,
            "relationship":relationship,"agent_administrator":administrator,"revision":revision});
        // Relationship/admin edits do not silently replace an appointed capability policy.
        value["capability_level_id"]=json!(if let Some(level)=current.as_ref().and_then(|v|v.get("capability_level_id")) {
            level.as_str().ok_or_else(||anyhow!("Invalid membership level"))?.to_owned()
        } else {crate::capabilities::membership_level(Some(&value),relationship)?});
        sqlx::query("INSERT INTO agent_memberships(user_id,agent_id,value) VALUES(?,?,?)
            ON CONFLICT(user_id,agent_id) DO UPDATE SET value=excluded.value")
            .bind(user_id).bind(agent_id).bind(self.seal(&value)?)
            .execute(&mut *tx).await?;
        let mut account = self.unseal(&account_blob)?;
        let old = Principal::from_account(&account)?.access_revision;
        account["access_revision"] = json!(old.checked_add(1)
            .ok_or_else(||anyhow!("Account revision overflow"))?);
        // INITIAL-OWNER-SESSION: new ownership grants no previously held access to revoke.
        // Keep the creator signed in so creation can proceed to model configuration.
        // Every later membership edit still invalidates sign-ins and access snapshots.
        let initial_owner = owner == user_id && current.is_none() && relationship == Relationship::Owner;
        if !initial_owner {
            account["generation"] = json!(uuid::Uuid::new_v4().to_string());
        }
        sqlx::query("UPDATE user_accounts SET value=? WHERE user_id=?")
            .bind(self.seal(&account)?).bind(user_id).execute(&mut *tx).await?;
        audit(&mut tx,self,"agent_membership_changed",&json!({"user_id":user_id,
            "agent_id":agent_id,"relationship":relationship,"agent_administrator":administrator,
            "actor_id":actor.map(|(id,_)|id),"revision":revision})).await?;
        tx.commit().await?;
        Ok(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    // TENANT-FIXTURE: isolated temporary agent files exercise real storage ownership.
    async fn tenant_fixture(store: &Store, id: &str, owner: &str) {
        let pool = crate::agent_storage::open_agent_pool(&store.root,id).await.unwrap();
        sqlx::query("INSERT INTO agents(id,name,description,provider,model,status,metadata,revision,created_at,updated_at) VALUES(?,?,'','openai_subscription','','active',?,1,1,1)")
            .bind(id).bind(id).bind(store.seal(&json!({"owner_id":owner})).unwrap())
            .execute(&pool).await.unwrap();
        pool.close().await;
        store.register_agent(id,owner).await.unwrap();
        store.set_agent_membership(owner,id,Relationship::Owner,0).await.unwrap();
    }
    async fn active_user(store: &Store, name: &str) -> String {
        let value = store.create_user_account(name,"long-hash-placeholder").await.unwrap();
        let id = value["user_id"].as_str().unwrap();
        store.update_account_access(id,1,AccountStatus::Active,Role::User,"basic").await.unwrap();
        id.to_owned()
    }
    // TENANT-BOUNDARY-TEST: separate owners and delegated membership authority never merge.
    #[tokio::test]
    async fn tenant_owner_and_delegated_roster_authority_are_separate() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store.create_admin_account("root","long-hash-placeholder","generation").await.unwrap();
        let owner = active_user(&store,"owner").await;
        let other_owner = active_user(&store,"other-owner").await;
        let manager = active_user(&store,"manager").await;
        let member = active_user(&store,"member").await;
        tenant_fixture(&store,"tenant-one",&owner).await;
        tenant_fixture(&store,"tenant-two",&other_owner).await;
        let first = store.for_agent("tenant-one").await.unwrap();
        let second = store.for_agent("tenant-two").await.unwrap();
        first.save_agent_model(&json!({"provider":"openai_subscription","model":"first-model"})).await.unwrap();
        assert_ne!(second.agent_model().await.unwrap()["model"],"first-model");
        assert_eq!(store.agent_authority("admin","tenant-one").await.unwrap()["owner_id"],owner);
        assert_eq!(store.agent_authority("admin","tenant-one").await.unwrap()["can_manage"],true);
        assert_eq!(store.agent_authority("admin","tenant-one").await.unwrap()["can_use"],false);
        assert!(store.assign_agent_membership("admin",&manager,"tenant-one",Relationship::Member,true,0).await.is_err());
        let appointed = store.assign_agent_membership(&owner,&manager,"tenant-one",Relationship::Member,true,0).await.unwrap();
        assert_eq!(appointed["agent_administrator"],true);
        assert_eq!(store.agent_authority(&manager,"tenant-one").await.unwrap()["can_manage"],true);
        assert_eq!(store.agent_authority(&manager,"tenant-two").await.unwrap()["can_manage"],false);
        assert!(store.assign_agent_membership(&manager,&member,"tenant-two",Relationship::Member,false,0).await.is_err());
        let visitor = store.assign_agent_membership(&manager,&member,"tenant-one",Relationship::Caller,false,0).await.unwrap();
        assert_eq!(visitor["relationship"],"caller");
        assert!(store.assign_agent_membership(&manager,&member,"tenant-one",Relationship::Member,false,0).await.is_err());
        assert!(store.assign_agent_membership(&manager,&member,"tenant-one",Relationship::Member,true,1).await.is_err());
        store.assign_agent_membership(&manager,&member,"tenant-one",Relationship::Member,false,1).await.unwrap();
        assert!(store.assign_agent_membership(&member,&manager,"tenant-one",Relationship::Member,false,1).await.is_err());
        assert!(store.assign_agent_membership(&manager,&manager,"tenant-one",Relationship::Revoked,false,1).await.is_err());
        assert!(store.assign_agent_membership(&owner,&owner,"tenant-one",Relationship::Member,false,1).await.is_err());
        assert!(store.set_agent_membership(&member,"tenant-one",Relationship::Owner,2).await.is_err());
        assert!(store.assign_agent_membership(&owner,"missing_account","tenant-one",Relationship::Member,false,0).await.is_err());
        let revoked = store.assign_agent_membership("admin",&manager,"tenant-one",Relationship::Revoked,false,1).await.unwrap();
        assert_eq!(revoked["agent_administrator"],false);
        assert_eq!(store.agent_authority(&manager,"tenant-one").await.unwrap()["can_manage"],false);
        assert!(store.assign_agent_membership(&manager,&member,"tenant-one",Relationship::Caller,false,2).await.is_err());
        let members = store.agent_memberships("tenant-one").await.unwrap();
        assert_eq!(members.len(),3);
        assert!(members.iter().all(|value|value.get("password_hash").is_none()));
        assert!(store.agent_authority(&owner,"unknown").await.is_err());
        // Existing owner/member/caller tool policy relationships remain unchanged.
        assert_eq!(store.live_access(&member,"tenant-one").await.unwrap().relationship,Relationship::Member);
    }
    // TENANT-REVOCATION-TEST: reopen and account suspension must preserve live denials.
    #[tokio::test]
    async fn tenant_administration_revocation_persists_and_inactive_owner_cannot_write() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store.create_admin_account("root","long-hash-placeholder","generation").await.unwrap();
        let owner = active_user(&store,"owner").await;
        let manager = active_user(&store,"manager").await;
        tenant_fixture(&store,"tenant-one",&owner).await;
        store.assign_agent_membership(&owner,&manager,"tenant-one",Relationship::Member,true,0).await.unwrap();
        let reopened = Store::open(root.path()).await.unwrap();
        assert_eq!(reopened.agent_authority(&manager,"tenant-one").await.unwrap()["agent_administrator"],true);
        let before = store.account_by_id(&manager).await.unwrap().unwrap();
        store.assign_agent_membership(&owner,&manager,"tenant-one",Relationship::Member,false,1).await.unwrap();
        let after = store.account_by_id(&manager).await.unwrap().unwrap();
        assert_ne!(before["generation"],after["generation"]);
        assert_eq!(reopened.agent_authority(&manager,"tenant-one").await.unwrap()["can_manage"],false);
        let revision = store.account_by_id(&owner).await.unwrap().unwrap()["access_revision"].as_u64().unwrap();
        store.update_account_access(&owner,revision,AccountStatus::Suspended,Role::User,"basic").await.unwrap();
        assert!(store.assign_agent_membership(&owner,&manager,"tenant-one",Relationship::Member,true,2).await.is_err());
        assert_eq!(store.agent_authority(&owner,"tenant-one").await.unwrap()["can_manage"],false);
    }
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
        let tier = store.save_entitlement_tier(Tier{id:"team".into(),revision:0,model_policy:None,ability_permissions:Default::default(),
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
