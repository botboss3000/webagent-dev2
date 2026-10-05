//! Agent-owned capability levels narrow use without appointing administrators.
//! Compatibility projects explicit grants for existing adapters; new plugins fail closed.
use crate::{access::{self,Permission,Relationship},store::Store};
use anyhow::{anyhow,bail,Result};
use serde::{Deserialize,Serialize};
use serde_json::{json,Value};
use sqlx::Row;
use std::collections::BTreeMap;

#[derive(Clone,Debug,Eq,PartialEq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CapabilityLevel {
    pub id:String,
    pub name:String,
    pub revision:u64,
    pub ability_permissions:BTreeMap<String,Permission>,
    pub tool_permissions:BTreeMap<String,Permission>,
}
impl CapabilityLevel {
    pub fn validate(&self)->Result<()> {
        if !access::valid_id(&self.id) || self.name.trim().is_empty() || self.name.len()>100
            || self.revision==0 || self.ability_permissions.len()>128 || self.tool_permissions.len()>256
            || self.ability_permissions.keys().chain(self.tool_permissions.keys())
                .any(|id|id!="*" && !access::valid_id(id)) {bail!("Invalid capability level");}
        Ok(())
    }
    pub fn ability_permission(&self,id:&str)->Permission {
        self.ability_permissions.get(id).or_else(||self.ability_permissions.get("*"))
            .copied().unwrap_or(Permission::Deny)
    }
    pub fn tool_permission(&self,id:&str)->Permission {
        self.tool_permissions.get(id).or_else(||self.tool_permissions.get("*"))
            .copied().unwrap_or(Permission::Auto)
    }
}
#[derive(Clone,Debug,Eq,PartialEq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CapabilityLevels {pub revision:u64,pub levels:BTreeMap<String,CapabilityLevel>}
impl Default for CapabilityLevels {
    fn default()->Self {
        // Default levels preserve prior ready-tool access beneath existing tier,
        // agent, installation and caller rules. Their names confer no authority.
        Self {revision:0,levels:[("visitor","Visitor"),("member","Member"),("administrator","Administrator")]
            .into_iter().map(|(id,name)|(id.into(),CapabilityLevel {id:id.into(),name:name.into(),revision:1,
                ability_permissions:access::legacy_ability_grants(),tool_permissions:BTreeMap::new()})).collect()}
    }
}
impl CapabilityLevels {
    pub fn from_metadata(metadata:&Value)->Result<Self> {
        let value=match metadata.get("capability_levels") {
            Some(v)=>serde_json::from_value::<Self>(v.clone())?,None=>Self::default(),
        };
        if value.levels.len()>64 {bail!("Too many capability levels");}
        for (id,level) in &value.levels {level.validate()?;if id!=&level.id {bail!("Capability level identity mismatch");}}
        if ["visitor","member","administrator"].iter().any(|id|!value.levels.contains_key(*id)) {
            bail!("Built-in capability levels must remain present");
        }
        Ok(value)
    }
}
pub fn membership_level(membership:Option<&Value>,relationship:Relationship)->Result<String> {
    if let Some(id)=membership.and_then(|v|v.get("capability_level_id")) {
        let id=id.as_str().ok_or_else(||anyhow!("Invalid membership level"))?;
        if !access::valid_id(id) {bail!("Invalid membership level");}return Ok(id.into());
    }
    Ok(match relationship {
        Relationship::Owner=>"administrator",Relationship::Member=>if membership.is_some_and(|v|v["agent_administrator"]==true) {"administrator"} else {"member"},
        Relationship::Caller|Relationship::Revoked=>"visitor",
    }.into())
}
impl Store {
    pub async fn capability_levels(&self,agent:&str)->Result<CapabilityLevels> {
        let view=self.for_agent(agent).await?;
        let blob:Vec<u8>=sqlx::query_scalar("SELECT metadata FROM agents WHERE id=?")
            .bind(agent).fetch_one(&view.agent_pool).await?;
        CapabilityLevels::from_metadata(&self.unseal(&blob)?)
    }
    pub async fn save_capability_level(&self,agent:&str,mut level:CapabilityLevel,expected:u64)->Result<CapabilityLevels> {
        level.validate()?;
        let view=self.for_agent(agent).await?;let mut tx=view.agent_pool.begin().await?;
        // Take the writer lock before reading either the catalog or record revision.
        sqlx::query("UPDATE agents SET revision=revision WHERE id=?").bind(agent).execute(&mut *tx).await?;
        let row=sqlx::query("SELECT metadata,revision FROM agents WHERE id=?").bind(agent).fetch_one(&mut *tx).await?;
        let mut metadata=self.unseal(&row.get::<Vec<u8>,_>("metadata"))?;
        let mut levels=CapabilityLevels::from_metadata(&metadata)?;
        if levels.revision!=expected {bail!("Capability levels revision changed");}
        if let Some(old)=levels.levels.get(&level.id) {
            if level.revision!=old.revision {bail!("Capability level revision changed");}
            level.revision=old.revision.checked_add(1).ok_or_else(||anyhow!("Level revision overflow"))?;
        } else if level.revision!=1 || levels.levels.len()>=64 {bail!("Invalid new capability level");}
        levels.revision=expected.checked_add(1).ok_or_else(||anyhow!("Level revision overflow"))?;
        levels.levels.insert(level.id.clone(),level.clone());metadata["capability_levels"]=serde_json::to_value(&levels)?;
        sqlx::query("UPDATE agents SET metadata=?,revision=revision+1,updated_at=strftime('%s','now') WHERE id=?")
            .bind(self.seal(&metadata)?).bind(agent).execute(&mut *tx).await?;
        // The agent-owned audit receipt commits with its policy, not in another tenant's DB.
        let receipt=json!({"agent_id":agent,"actor_id":self.current_user_id(),"level_id":level.id,"revision":levels.revision});
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)")
            .bind(format!("agent:{agent}:capability_audit:{}",levels.revision)).bind(self.seal(&receipt)?).execute(&mut *tx).await?;
        tx.commit().await?;Ok(levels)
    }
    pub async fn assign_capability_level(&self,actor:&str,user:&str,agent:&str,level:&str,expected:u64)->Result<Value> {
        if !access::valid_id(level) {bail!("Invalid capability level");}
        // The application transaction serializes membership authority and revocation.
        // Level records cannot be removed; live access rechecks every level revision.
        if !self.capability_levels(agent).await?.levels.contains_key(level) {bail!("Capability level not found");}
        let mut tx=self.app_pool.begin().await?;
        sqlx::query("UPDATE user_accounts SET value=value WHERE user_id=?").bind(user).execute(&mut *tx).await?;
        let actor_blob:Vec<u8>=sqlx::query_scalar("SELECT value FROM user_accounts WHERE user_id=?").bind(actor).fetch_one(&mut *tx).await?;
        let principal=access::Principal::from_account(&self.unseal(&actor_blob)?)?;
        let owner:String=sqlx::query_scalar("SELECT owner_id FROM agent_directory WHERE id=?").bind(agent).fetch_one(&mut *tx).await?;
        let actor_member:Option<Vec<u8>>=sqlx::query_scalar("SELECT value FROM agent_memberships WHERE user_id=? AND agent_id=?")
            .bind(actor).bind(agent).fetch_optional(&mut *tx).await?;
        let delegated=actor_member.as_ref().map(|v|self.unseal(v)).transpose()?
            .is_some_and(|v|v["relationship"]=="member" && v["agent_administrator"]==true);
        if !principal.active() || !(actor==owner || delegated || principal.administrator()) {bail!("Agent administration required");}
        let blob:Vec<u8>=sqlx::query_scalar("SELECT value FROM agent_memberships WHERE user_id=? AND agent_id=?")
            .bind(user).bind(agent).fetch_one(&mut *tx).await?;
        let mut membership=self.unseal(&blob)?;
        if membership["revision"]!=expected || membership["relationship"]=="revoked" {bail!("Membership revision changed or revoked");}
        if actor!=owner && !principal.administrator() && (user==owner || membership["agent_administrator"]==true) {
            bail!("Delegated administrators cannot change privileged memberships");
        }
        membership["revision"]=json!(expected.checked_add(1).ok_or_else(||anyhow!("Membership revision overflow"))?);
        membership["capability_level_id"]=json!(level);
        sqlx::query("UPDATE agent_memberships SET value=? WHERE user_id=? AND agent_id=?")
            .bind(self.seal(&membership)?).bind(user).bind(agent).execute(&mut *tx).await?;
        let account:Vec<u8>=sqlx::query_scalar("SELECT value FROM user_accounts WHERE user_id=?").bind(user).fetch_one(&mut *tx).await?;
        let mut account=self.unseal(&account)?;let revision=access::Principal::from_account(&account)?.access_revision;
        account["access_revision"]=json!(revision.checked_add(1).ok_or_else(||anyhow!("Access revision overflow"))?);
        account["generation"]=json!(uuid::Uuid::new_v4().to_string());
        sqlx::query("UPDATE user_accounts SET value=? WHERE user_id=?").bind(self.seal(&account)?).bind(user).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES('','agent_capability_assigned',?)")
            .bind(self.seal(&json!({"agent_id":agent,"user_id":user,"actor_id":actor,"level_id":level,"revision":membership["revision"]}))?)
            .execute(&mut *tx).await?;
        tx.commit().await?;Ok(membership)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn legacy_projection_is_bounded_and_does_not_grant_new_plugins() {
        let levels=CapabilityLevels::from_metadata(&json!({})).unwrap();
        assert_eq!(levels.levels.len(),3);
        for level in levels.levels.values() {
            assert_eq!(level.ability_permission("codebase_admin"),Permission::Auto);
            assert_eq!(level.ability_permission("new_plugin"),Permission::Deny);
        }
        assert!(CapabilityLevels::from_metadata(&json!({"capability_levels":{"revision":1,"levels":{}}})).is_err());
    }
    #[tokio::test]
    async fn capability_changes_narrow_access_and_survive_reopen() {
        let root=tempfile::tempdir().unwrap();let store=Store::open(root.path()).await.unwrap();
        store.create_admin_account("admin","hash","generation").await.unwrap();
        let before=store.live_access("admin","shared_default").await.unwrap();
        let mut level=before.capability_level.clone();
        level.ability_permissions.insert("codebase_admin".into(),Permission::Deny);
        let saved=store.save_capability_level("shared_default",level,0).await.unwrap();
        let after=store.live_access("admin","shared_default").await.unwrap();
        assert_ne!(before.context_fingerprint(),after.context_fingerprint());
        assert_eq!(after.revision.capability_level,saved.revision);
        let decide=|access:&crate::access_policy::LiveAccess|access.tool_decision_for_abilities("file_read",&["codebase_admin".into()],Permission::Auto,true,true,false,true);
        assert_eq!(decide(&before).permission,Permission::Auto);
        assert_eq!(decide(&after).reason,access::Reason::CapabilityDenied);
        let reopened=Store::open(root.path()).await.unwrap();
        assert_eq!(decide(&reopened.live_access("admin","shared_default").await.unwrap()).permission,Permission::Deny);
        let mut level=after.capability_level.clone();level.ability_permissions.insert("codebase_admin".into(),Permission::Ask);
        store.save_capability_level("shared_default",level,1).await.unwrap();
        assert_eq!(decide(&store.live_access("admin","shared_default").await.unwrap()).permission,Permission::Ask);
        let mut tier=store.entitlement_tier("operator").await.unwrap().unwrap();
        tier.ability_permissions.insert("codebase_admin".into(),Permission::Deny);
        let revision=tier.revision;store.save_entitlement_tier(tier,revision).await.unwrap();
        assert_eq!(decide(&store.live_access("admin","shared_default").await.unwrap()).reason,access::Reason::TierDenied);
    }
    #[tokio::test]
    async fn combined_base_permissions_reject_stale_writes_without_partial_replacement() {
        let root=tempfile::tempdir().unwrap();let store=Store::open(root.path()).await.unwrap();
        store.create_admin_account("admin","hash","generation").await.unwrap();
        let revision=store.agent_ability_permissions("shared_default").await.unwrap()["revision"].as_u64().unwrap();
        store.set_agent_permissions("shared_default",revision,json!({"codebase_admin":"ask"}),json!({"file_read":"deny"})).await.unwrap();
        assert!(store.set_agent_permissions("shared_default",revision,json!({"codebase_admin":"auto"}),json!({"file_read":"auto"})).await.is_err());
        let access=store.live_access("admin","shared_default").await.unwrap();
        assert_eq!(access.ability_permission("codebase_admin"),Permission::Ask);
        assert_eq!(access.tool_decision_for_abilities("file_read",&["codebase_admin".into()],Permission::Auto,true,true,false,true).reason,access::Reason::AgentDenied);
    }
    #[tokio::test]
    async fn level_writes_are_agent_owned_revisioned_and_durable() {
        let root=tempfile::tempdir().unwrap();let store=Store::open(root.path()).await.unwrap();
        store.create_admin_account("admin","hash","generation").await.unwrap();
        let mut level=store.capability_levels("shared_default").await.unwrap().levels["member"].clone();
        level.ability_permissions.insert("codebase_admin".into(),Permission::Deny);
        let saved=store.save_capability_level("shared_default",level.clone(),0).await.unwrap();
        assert_eq!(saved.revision,1);assert!(store.save_capability_level("shared_default",level,0).await.is_err());
        let reopened=Store::open(root.path()).await.unwrap();assert_eq!(reopened.capability_levels("shared_default").await.unwrap(),saved);
    }
}
