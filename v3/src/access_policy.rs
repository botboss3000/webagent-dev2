//! Answer "may this person use this agent or tool right now?" Account status,
//! tier, agent relationship, and permission all matter. Visibility only decides
//! what the model sees; it never grants execution. A fresh access snapshot is
//! required at each checkpoint because an earlier approval can become stale.
use crate::access::{self, AccountStatus, Permission, Principal, Relationship, Tier,
    ToolDecision, ToolFactors};
use crate::store::Store;
use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::Row;

fn permission_map(value:&Value) -> Result<()> {
    if value.is_null() {return Ok(());}
    let map=value.as_object().ok_or_else(||anyhow!("Invalid permission map"))?;
    if map.len()>128 {return Err(anyhow!("Permission map exceeds limit"));}
    for (name,mode) in map {
        if name!="*" && !access::valid_id(name) {
            return Err(anyhow!("Invalid permission name"));
        }
        Permission::from_policy(mode.as_str()
            .ok_or_else(||anyhow!("Invalid permission mode"))?)?;
    }
    Ok(())
}

fn caller_map(value:&Value) -> Result<()> {
    if value.is_null() {return Ok(());}
    let map=value.as_object().ok_or_else(||anyhow!("Invalid tool caller map"))?;
    if map.len()>128 {return Err(anyhow!("Tool caller map exceeds limit"));}
    for (name,roles) in map {
        if name!="*" && !access::valid_id(name) {
            return Err(anyhow!("Invalid tool caller name"));
        }
        let roles=roles.as_array().ok_or_else(||anyhow!("Invalid tool caller roles"))?;
        if roles.len()>3 {return Err(anyhow!("Too many tool caller roles"));}
        let mut seen=std::collections::BTreeSet::new();
        for role in roles {
            let role=role.as_str().ok_or_else(||anyhow!("Invalid tool caller role"))?;
            if !matches!(role,"owner"|"member"|"caller") || !seen.insert(role) {
                return Err(anyhow!("Invalid tool caller role"));
            }
        }
    }
    Ok(())
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AccessRevision {
    pub account: u64,
    pub tier: u64,
    pub membership: u64,
    pub agent: u64,
}

#[derive(Clone, Debug)]
pub struct LiveAccess {
    pub principal: Principal,
    pub tier: Tier,
    pub agent_id: String,
    pub relationship: Relationship,
    pub agent_enabled: bool,
    pub revision: AccessRevision,
    tool_permissions: Value,
    ability_permissions: Value,
    tool_callers: Value,
}

impl LiveAccess {
    /// CONTEXT-AUTH-FINGERPRINT: explicit shaping inputs, not Debug formatting.
    /// This private value is hashed by the run-local cache and never sent to the
    /// provider or browser. Include grants as well as revisions so malformed
    /// revision updates cannot retain stale guidance or tool exposure.
    pub fn context_fingerprint(&self) -> Value {
        serde_json::json!([self.principal.user_id, self.principal.status,
            self.principal.role, self.principal.tier_id, self.principal.access_revision,
            self.tier, self.agent_id, self.relationship, self.agent_enabled,
            self.revision, self.tool_permissions, self.ability_permissions, self.tool_callers])
    }

    pub fn admitted(&self) -> bool {
        self.principal.status == AccountStatus::Active
            && self.relationship != Relationship::Revoked
            && self.agent_enabled
    }

    pub fn model_allowed(&self, model: &str) -> bool {
        self.admitted() && self.tier.allows_model(model)
    }

    pub fn max_sessions(&self) -> u32 { self.tier.max_sessions }
    pub fn max_active_runs(&self) -> u32 { self.tier.max_active_runs }

    fn agent_tool_permission(&self, name: &str) -> Permission {
        let value = self.tool_permissions.get(name)
            .or_else(|| self.tool_permissions.get("*"));
        match value {
            None => Permission::Auto,
            Some(Value::String(value)) => {
                Permission::from_policy(value).unwrap_or(Permission::Deny)
            }
            _ => Permission::Deny,
        }
    }

    pub fn ability_permission(&self,ability_id:&str) -> Permission {
        match self.ability_permissions.get(ability_id)
            .or_else(||self.ability_permissions.get("*")) {
            None=>Permission::Auto,
            Some(Value::String(value))=>Permission::from_policy(value)
                .unwrap_or(Permission::Deny),
            _=>Permission::Deny,
        }
    }

    fn caller_allowed(&self,name:&str) -> bool {
        let value=self.tool_callers.get(name).or_else(||self.tool_callers.get("*"));
        match value {
            None => true,
            Some(Value::Array(items)) => items.iter().any(|item| {
                item.as_str()==Some(match self.relationship {
                    Relationship::Owner=>"owner",Relationship::Member=>"member",
                    Relationship::Caller=>"caller",Relationship::Revoked=>"revoked"
                })
            }),
            _ => false,
        }
    }

    /// `available` must come from the installed tool catalog; `visible`
    /// must come from the agent and session visibility state. Neither grants
    /// access over a Deny in the operator, tier, or agent permission plane.
    pub fn tool_decision(&self, name: &str, installation: Permission,
        available: bool, visible: bool, read_only: bool, is_read: bool) -> ToolDecision {
        self.resolve_tool(name,installation,Permission::Auto,
            available,visible,read_only,is_read)
    }

    pub fn tool_decision_for_abilities(&self,name:&str,ability_ids:&[String],
        installation:Permission,available:bool,visible:bool,
        read_only:bool,is_read:bool) -> ToolDecision {
        let ability=if ability_ids.is_empty() {
            if available {Permission::Deny} else {Permission::Auto}
        } else {
            ability_ids.iter().fold(Permission::Auto,
                |permission,id|permission.meet(self.ability_permission(id)))
        };
        self.resolve_tool(name,installation,ability,
            available,visible,read_only,is_read)
    }

    fn resolve_tool(&self,name:&str,installation:Permission,ability:Permission,
        available:bool,visible:bool,read_only:bool,is_read:bool) -> ToolDecision {
        ToolFactors {
            installation,
            tier:self.tier.tool_permission(name),
            agent:self.agent_tool_permission(name),
            ability,
            account_active:self.principal.active(),
            relationship:self.relationship != Relationship::Revoked
                && self.caller_allowed(name),
            agent_enabled:self.agent_enabled,
            available, visible, read_only, is_read,
        }.resolve()
    }
}

impl Store {
    pub async fn live_access(&self, user_id: &str, agent_id: &str) -> Result<LiveAccess> {
        if !access::valid_id(user_id) || !access::valid_agent_id(agent_id) {
            return Err(anyhow!("Invalid access identity"));
        }
        let agent_view = self.for_agent(agent_id).await?;
        let account = self.account_by_id(user_id).await?
            .ok_or_else(|| anyhow!("Account not found"))?;
        let principal = Principal::from_account(&account)?;
        if principal.user_id != user_id {return Err(anyhow!("Account identity conflict"));}
        // Guest policy is rechecked by the loop as well as HTTP authentication.
        if principal.role == access::Role::Guest {
            let policy=self.read_access_policy()?;
            if policy["registration"] != "open" {
                return Err(anyhow!("Guest access is not enabled"));
            }
        }
        let tier = self.entitlement_tier(&principal.tier_id).await?
            .ok_or_else(|| anyhow!("Entitlement tier not found"))?;
        if tier.id != principal.tier_id {return Err(anyhow!("Tier identity conflict"));}
        let membership = self.agent_membership(user_id, agent_id).await?;
        if let Some(value)=&membership {
            if value["user_id"]!=user_id || value["agent_id"]!=agent_id
                || value["revision"].as_u64().is_none_or(|revision|revision==0) {
                return Err(anyhow!("Membership identity or revision conflict"));
            }
        }
        let relationship = match membership.as_ref()
            .and_then(|value| value["relationship"].as_str()) {
            Some(value) => serde_json::from_value(Value::String(value.into()))?,
            None => Relationship::Revoked,
        };
        let membership_revision = membership.as_ref()
            .and_then(|value| value["revision"].as_u64()).unwrap_or(0);
        let agent = sqlx::query("SELECT status,metadata,revision FROM agents WHERE id=?")
            .bind(agent_id).fetch_optional(&agent_view.agent_pool).await?;
        let (agent_enabled, agent_revision, tool_permissions, ability_permissions, tool_callers) = if let Some(agent) = agent {
            let metadata = self.unseal(&agent.get::<Vec<u8>,_>("metadata"))?;
            if !metadata.is_object() {return Err(anyhow!("Invalid agent metadata"));}
            let owner:Option<String>=sqlx::query_scalar(
                "SELECT owner_id FROM agent_directory WHERE id=?")
                .bind(agent_id).fetch_optional(&self.app_pool).await?;
            let owner=owner.ok_or_else(||anyhow!("Agent directory entry missing"))?;
            if (agent_id=="shared_default" && owner!="admin")
                || metadata["owner_id"].as_str().is_some_and(|id|id!=owner)
                || (agent_id!="shared_default" && metadata["owner_id"].as_str()!=Some(owner.as_str())) {
                return Err(anyhow!("Agent owner conflict"));
            }
            let permissions = metadata.get("tool_permissions")
                .cloned().unwrap_or(Value::Null);
            permission_map(&permissions)?;
            let abilities=metadata.get("ability_permissions").cloned().unwrap_or(Value::Null);
            permission_map(&abilities)?;
            let callers=metadata.get("tool_callers").cloned().unwrap_or(Value::Null);
            caller_map(&callers)?;
            (agent.get::<String,_>("status") == "active",
                agent.get::<i64,_>("revision").max(0) as u64, permissions, abilities, callers)
        } else {
            (false, 0, Value::Null, Value::Null, Value::Null)
        };
        let revision = AccessRevision { account:principal.access_revision,
            tier:tier.revision, membership:membership_revision, agent:agent_revision };
        Ok(LiveAccess { principal,tier,agent_id:agent_id.to_owned(),
            relationship,agent_enabled,revision,tool_permissions,ability_permissions,tool_callers })
    }
}

impl Store {
    pub async fn agent_tool_permissions(&self, agent_id: &str) -> Result<Value> {
        if !access::valid_agent_id(agent_id) {
            return Err(anyhow!("Invalid agent identity"));
        }
        let view = self.for_agent(agent_id).await?;
        let row = sqlx::query("SELECT metadata,revision FROM agents WHERE id=?")
            .bind(agent_id).fetch_optional(&view.agent_pool).await?
            .ok_or_else(|| anyhow!("Agent not found"))?;
        let metadata = self.unseal(&row.get::<Vec<u8>,_>("metadata"))?;
        Ok(serde_json::json!({"agent_id":agent_id,
            "revision":row.get::<i64,_>("revision"),
            "tool_permissions":metadata.get("tool_permissions")
                .cloned().unwrap_or_else(||serde_json::json!({}))}))
    }

    pub async fn set_agent_tool_permissions(&self, agent_id: &str,
        expected_revision: u64,
        permissions: std::collections::BTreeMap<String, Permission>) -> Result<Value> {
        if !access::valid_agent_id(agent_id)
            || expected_revision >= i64::MAX as u64 || permissions.len() > 128
            || permissions.keys().any(|name| name != "*" && !access::valid_id(name)) {
            return Err(anyhow!("Invalid agent permission update"));
        }
        let view = self.for_agent(agent_id).await?;
        let mut tx = view.agent_pool.begin().await?;
        let row = sqlx::query("SELECT metadata,revision FROM agents WHERE id=?")
            .bind(agent_id).fetch_optional(&mut *tx).await?
            .ok_or_else(|| anyhow!("Agent not found"))?;
        if row.get::<i64,_>("revision") as u64 != expected_revision {
            return Err(anyhow!("Agent revision changed"));
        }
        let mut metadata = self.unseal(&row.get::<Vec<u8>,_>("metadata"))?;
        if !metadata.is_object() {
            return Err(anyhow!("Invalid agent metadata"));
        }
        metadata["tool_permissions"] = serde_json::to_value(&permissions)?;
        let updated = sqlx::query("UPDATE agents SET metadata=?,revision=revision+1,
            updated_at=strftime('%s','now') WHERE id=? AND revision=?")
            .bind(self.seal(&metadata)?).bind(agent_id).bind(expected_revision as i64)
            .execute(&mut *tx).await?;
        if updated.rows_affected() != 1 {
            return Err(anyhow!("Agent revision changed"));
        }
        tx.commit().await?;
        self.event("","agent_tool_permissions_changed",&serde_json::json!({
            "agent_id":agent_id,"revision":expected_revision+1})).await?;
        Ok(serde_json::json!({"agent_id":agent_id,"revision":expected_revision+1,
            "tool_permissions":permissions}))
    }
    pub async fn agent_ability_permissions(&self,agent_id:&str) -> Result<Value> {
        let view=self.for_agent(agent_id).await?;
        let row=sqlx::query("SELECT metadata,revision FROM agents WHERE id=?")
            .bind(agent_id).fetch_optional(&view.agent_pool).await?
            .ok_or_else(||anyhow!("Agent not found"))?;
        let metadata=self.unseal(&row.get::<Vec<u8>,_>("metadata"))?;
        Ok(serde_json::json!({"agent_id":agent_id,
            "revision":row.get::<i64,_>("revision"),
            "ability_permissions":metadata.get("ability_permissions")
                .cloned().unwrap_or_else(||serde_json::json!({}))}))
    }

    pub async fn set_agent_ability_permissions(&self,agent_id:&str,
        expected_revision:u64,
        permissions:std::collections::BTreeMap<String,Permission>) -> Result<Value> {
        if expected_revision >= i64::MAX as u64 || permissions.len()>128
            || permissions.keys().any(|id|id!="*" && !access::valid_id(id)) {
            return Err(anyhow!("Invalid agent ability permissions"));
        }
        let view=self.for_agent(agent_id).await?;
        let mut tx=view.agent_pool.begin().await?;
        let row=sqlx::query("SELECT metadata,revision FROM agents WHERE id=?")
            .bind(agent_id).fetch_optional(&mut *tx).await?
            .ok_or_else(||anyhow!("Agent not found"))?;
        if row.get::<i64,_>("revision") as u64!=expected_revision {
            return Err(anyhow!("Agent revision changed"));
        }
        let mut metadata=self.unseal(&row.get::<Vec<u8>,_>("metadata"))?;
        if !metadata.is_object() {return Err(anyhow!("Invalid agent metadata"));}
        metadata["ability_permissions"]=serde_json::to_value(&permissions)?;
        let updated=sqlx::query("UPDATE agents SET metadata=?,revision=revision+1,
            updated_at=strftime('%s','now') WHERE id=? AND revision=?")
            .bind(self.seal(&metadata)?).bind(agent_id).bind(expected_revision as i64)
            .execute(&mut *tx).await?;
        if updated.rows_affected()!=1 {return Err(anyhow!("Agent revision changed"));}
        tx.commit().await?;
        self.event("","agent_ability_permissions_changed",&serde_json::json!({
            "agent_id":agent_id,"revision":expected_revision+1})).await?;
        Ok(serde_json::json!({"agent_id":agent_id,"revision":expected_revision+1,
            "ability_permissions":permissions}))
    }

    pub async fn agent_tool_callers(&self, agent_id: &str) -> Result<Value> {
        let view=self.for_agent(agent_id).await?;
        let row=sqlx::query("SELECT metadata,revision FROM agents WHERE id=?")
            .bind(agent_id).fetch_optional(&view.agent_pool).await?
            .ok_or_else(||anyhow!("Agent not found"))?;
        let metadata=self.unseal(&row.get::<Vec<u8>,_>("metadata"))?;
        Ok(serde_json::json!({"agent_id":agent_id,
            "revision":row.get::<i64,_>("revision"),
            "tool_callers":metadata.get("tool_callers")
                .cloned().unwrap_or_else(||serde_json::json!({}))}))
    }

    pub async fn set_agent_tool_callers(&self,agent_id:&str,expected_revision:u64,
        callers:std::collections::BTreeMap<String,Vec<Relationship>>) -> Result<Value> {
        if expected_revision >= i64::MAX as u64 || callers.len()>128
            || callers.iter().any(|(name,roles)| {
                (name!="*" && !access::valid_id(name)) || roles.len()>3
                    || roles.iter().any(|role|*role==Relationship::Revoked)
                    || roles.iter().enumerate().any(|(i,role)|roles[..i].contains(role))
            }) {
            return Err(anyhow!("Invalid agent tool caller rules"));
        }
        let view=self.for_agent(agent_id).await?;
        let mut tx=view.agent_pool.begin().await?;
        let row=sqlx::query("SELECT metadata,revision FROM agents WHERE id=?")
            .bind(agent_id).fetch_optional(&mut *tx).await?
            .ok_or_else(||anyhow!("Agent not found"))?;
        if row.get::<i64,_>("revision") as u64 != expected_revision {
            return Err(anyhow!("Agent revision changed"));
        }
        let mut metadata=self.unseal(&row.get::<Vec<u8>,_>("metadata"))?;
        if !metadata.is_object() {return Err(anyhow!("Invalid agent metadata"));}
        metadata["tool_callers"]=serde_json::to_value(&callers)?;
        let updated=sqlx::query("UPDATE agents SET metadata=?,revision=revision+1,
            updated_at=strftime('%s','now') WHERE id=? AND revision=?")
            .bind(self.seal(&metadata)?).bind(agent_id).bind(expected_revision as i64)
            .execute(&mut *tx).await?;
        if updated.rows_affected()!=1 {return Err(anyhow!("Agent revision changed"));}
        tx.commit().await?;
        self.event("","agent_tool_callers_changed",&serde_json::json!({
            "agent_id":agent_id,"revision":expected_revision+1})).await?;
        Ok(serde_json::json!({"agent_id":agent_id,"revision":expected_revision+1,
            "tool_callers":callers}))
    }

    pub async fn agent_status(&self, agent_id: &str) -> Result<Value> {
        let view = self.for_agent(agent_id).await?;
        let row = sqlx::query("SELECT status,revision FROM agents WHERE id=?")
            .bind(agent_id).fetch_optional(&view.agent_pool).await?
            .ok_or_else(|| anyhow!("Agent not found"))?;
        Ok(serde_json::json!({"agent_id":agent_id,
            "status":row.get::<String,_>("status"),
            "revision":row.get::<i64,_>("revision")}))
    }

    pub async fn set_agent_status(&self, agent_id: &str,
        expected_revision: u64, status: &str) -> Result<Value> {
        if !matches!(status,"active"|"disabled")
            || expected_revision >= i64::MAX as u64 {
            return Err(anyhow!("Invalid agent status"));
        }
        let view = self.for_agent(agent_id).await?;
        let mut tx = view.agent_pool.begin().await?;
        let row = sqlx::query("SELECT revision FROM agents WHERE id=?")
            .bind(agent_id).fetch_optional(&mut *tx).await?
            .ok_or_else(|| anyhow!("Agent not found"))?;
        if row.get::<i64,_>("revision") as u64 != expected_revision {
            return Err(anyhow!("Agent revision changed"));
        }
        let updated = sqlx::query("UPDATE agents SET status=?,revision=revision+1,
            updated_at=strftime('%s','now') WHERE id=? AND revision=?")
            .bind(status).bind(agent_id).bind(expected_revision as i64)
            .execute(&mut *tx).await?;
        if updated.rows_affected()!=1 {
            return Err(anyhow!("Agent revision changed"));
        }
        tx.commit().await?;
        self.event("","agent_status_changed",&serde_json::json!({
            "agent_id":agent_id,"status":status,"revision":expected_revision+1})).await?;
        Ok(serde_json::json!({"agent_id":agent_id,"status":status,
            "revision":expected_revision+1}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::access::{AccountStatus, Reason, Role};

    #[test]
    fn malformed_permission_and_caller_maps_fail_closed() {
        assert!(permission_map(&serde_json::json!({"file_read":true})).is_err());
        assert!(permission_map(&serde_json::json!({"../file_read":"auto"})).is_err());
        assert!(caller_map(&serde_json::json!({"file_read":["owner","owner"]})).is_err());
        assert!(caller_map(&serde_json::json!({"file_read":["revoked"]})).is_err());
    }

    #[tokio::test]
    async fn malformed_agent_metadata_fails_closed() {
        let root=tempfile::tempdir().unwrap();
        let store=Store::open(root.path()).await.unwrap();
        store.create_admin_account("admin","hash","generation").await.unwrap();
        sqlx::query("UPDATE agents SET metadata=? WHERE id='shared_default'")
            .bind(store.seal(&serde_json::json!("malformed")).unwrap())
            .execute(&store.agent_pool).await.unwrap();
        assert!(store.live_access("admin","shared_default").await.is_err());
    }

    #[tokio::test]
    async fn live_access_restricts_pending_and_rechecks_revisions() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store.create_admin_account("admin","hash","generation").await.unwrap();
        let admin = store.live_access("admin","shared_default").await.unwrap();
        // CONTEXT-FINGERPRINT-REGRESSION: grants invalidate even without revision changes.
        let mut changed=admin.clone();changed.tool_permissions=serde_json::json!({"file_read":"deny"});
        assert_ne!(admin.context_fingerprint(),changed.context_fingerprint());
        assert_eq!(admin.revision,changed.revision);
        assert!(admin.admitted());
        assert!(admin.model_allowed("gpt-6-luna"));
        assert_eq!(admin.tool_decision("file_write",Permission::Ask,
            true,true,false,false).permission,Permission::Ask);
        let original = store.agent_tool_permissions("shared_default").await.unwrap();
        let agent_revision = original["revision"].as_u64().unwrap();
        let permissions = [("file_write".to_owned(),Permission::Deny)].into();
        store.set_agent_tool_permissions("shared_default",agent_revision,permissions)
            .await.unwrap();
        let tightened = store.live_access("admin","shared_default").await.unwrap();
        assert_ne!(admin.revision,tightened.revision);
        assert_eq!(tightened.tool_decision("file_write",Permission::Ask,
            true,true,false,false).reason,Reason::AgentDenied);
        assert!(store.set_agent_tool_permissions("shared_default",agent_revision,
            Default::default()).await.is_err());
        let disabled = store.set_agent_status("shared_default",agent_revision+1,
            "disabled").await.unwrap();
        assert_eq!(disabled["status"],"disabled");
        assert!(!store.live_access("admin","shared_default").await.unwrap().admitted());
        store.set_agent_status("shared_default",agent_revision+2,
            "active").await.unwrap();
        let agent_revision=store.agent_ability_permissions("shared_default").await.unwrap()
            ["revision"].as_u64().unwrap();
        store.set_agent_ability_permissions("shared_default",agent_revision,
            [("codebase_admin".to_owned(),Permission::Deny)].into())
            .await.unwrap();
        let denied=store.live_access("admin","shared_default").await.unwrap();
        assert_eq!(denied.tool_decision_for_abilities("file_read",
            &["codebase_admin".to_owned()],Permission::Auto,true,true,false,true)
            .reason,Reason::AbilityDenied);
        store.set_agent_ability_permissions("shared_default",agent_revision+1,
            [("codebase_admin".to_owned(),Permission::Ask)].into())
            .await.unwrap();
        let ask=store.live_access("admin","shared_default").await.unwrap();
        assert_eq!(ask.tool_decision_for_abilities("file_read",
            &["codebase_admin".to_owned()],Permission::Auto,true,true,false,true)
            .reason,Reason::ApprovalRequired);
        store.set_agent_ability_permissions("shared_default",agent_revision+2,
            Default::default()).await.unwrap();
        let user = store.create_user_account("second","x".repeat(24).as_str())
            .await.unwrap();
        let id = user["user_id"].as_str().unwrap();
        store.set_agent_membership(id,"shared_default",Relationship::Caller,0)
            .await.unwrap();
        let pending = store.live_access(id,"shared_default").await.unwrap();
        assert!(!pending.admitted());
        assert_eq!(pending.tool_decision("file_read",Permission::Auto,
            true,true,false,true).reason,Reason::TierDenied);
        store.update_account_access(id,2,AccountStatus::Active,Role::User,"basic")
            .await.unwrap();
        let active = store.live_access(id,"shared_default").await.unwrap();
        assert!(active.admitted());
        assert!(!active.model_allowed("gpt-6-luna"));
        assert_ne!(pending.revision,active.revision);
        let mut tier = store.entitlement_tier("basic").await.unwrap().unwrap();
        tier.allowed_models.push("gpt-6-luna".into());
        tier.tools.insert("file_read".into(),Permission::Auto);
        store.save_entitlement_tier(tier,1).await.unwrap();
        let granted = store.live_access(id,"shared_default").await.unwrap();
        assert!(granted.model_allowed("gpt-6-luna"));
        assert_eq!(granted.tool_decision("file_read",Permission::Auto,
            true,true,false,true).permission,Permission::Auto);
        assert_ne!(active.revision,granted.revision);
        let current=store.agent_tool_callers("shared_default").await.unwrap();
        let agent_revision=current["revision"].as_u64().unwrap();
        let callers=[("file_read".to_owned(),
            vec![Relationship::Owner,Relationship::Member])].into();
        store.set_agent_tool_callers("shared_default",agent_revision,callers)
            .await.unwrap();
        let caller=store.live_access(id,"shared_default").await.unwrap();
        assert_eq!(caller.tool_decision("file_read",Permission::Auto,
            true,true,false,true).reason,Reason::CallerDenied);
        store.set_agent_membership(id,"shared_default",Relationship::Member,1)
            .await.unwrap();
        let member=store.live_access(id,"shared_default").await.unwrap();
        assert_eq!(member.tool_decision("file_read",Permission::Auto,
            true,true,false,true).permission,Permission::Auto);
        store.set_agent_membership(id,"shared_default",Relationship::Revoked,2)
            .await.unwrap();
        let revoked = store.live_access(id,"shared_default").await.unwrap();
        assert!(!revoked.admitted());
        assert_ne!(granted.revision,revoked.revision);
        assert_eq!(revoked.tool_decision("file_read",Permission::Auto,
            true,true,false,true).reason,Reason::CallerDenied);
    }
}
