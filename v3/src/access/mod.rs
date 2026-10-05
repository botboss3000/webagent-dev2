//! Phase 3 access decisions. Visibility and availability never grant permission.
use anyhow::{anyhow, bail, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AccountStatus { Pending, Active, Suspended, Disabled }

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role { Guest, User, Administrator }

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Relationship { Revoked, Owner, Member, Caller }

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Permission { Deny, Ask, Auto }

impl Permission {
    pub fn from_policy(value: &str) -> Result<Self> {
        match value {
            "deny" => Ok(Self::Deny),
            "require_approval" | "ask" => Ok(Self::Ask),
            "allow" | "auto" => Ok(Self::Auto),
            _ => bail!("Invalid permission"),
        }
    }
    pub fn meet(self, other: Self) -> Self {
        use Permission::*;
        match (self, other) {
            (Deny, _) | (_, Deny) => Deny,
            (Ask, _) | (_, Ask) => Ask,
            _ => Auto,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelPolicy {
    pub roster_id: Option<String>,
    pub allowed_entry_ids: std::collections::BTreeSet<String>,
    pub allow_byo: bool,
    pub max_byo_entries: u32,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tier {
    pub id: String,
    pub revision: u64,
    pub allowed_models: Vec<String>,
    #[serde(default)]
    pub model_policy: Option<ModelPolicy>,
    pub tools: BTreeMap<String, Permission>,
    // Missing legacy grants preserve only the two already executable adapters.
    // Newly registered ability IDs remain denied until explicitly appointed.
    #[serde(default = "legacy_ability_grants")]
    pub ability_permissions: BTreeMap<String, Permission>,
    pub max_sessions: u32,
    pub max_active_runs: u32,
}
pub fn legacy_ability_grants() -> BTreeMap<String, Permission> {
    [("codebase_admin".into(),Permission::Auto),("web_access".into(),Permission::Auto)].into()
}
impl Tier {
    pub fn validate(&self) -> Result<()> {
        if let Some(policy)=&self.model_policy {
            if policy.roster_id.as_ref().is_some_and(|id|!valid_id(id))
                || policy.allowed_entry_ids.len()>256 || policy.allowed_entry_ids.iter().any(|id|!valid_id(id))
                || policy.max_byo_entries>256 || (policy.roster_id.is_none() && !policy.allowed_entry_ids.is_empty()) {
                bail!("Invalid model policy");
            }
        }
        if !valid_id(&self.id) || self.revision == 0
            || self.allowed_models.len() > 64 || self.tools.len() > 128 || self.ability_permissions.len() > 128
            || self.max_sessions > 10_000 || self.max_active_runs > 200 {
            bail!("Invalid entitlement tier");
        }
        if self.allowed_models.iter().any(|model| model != "*" && !valid_model(model))
            || self.tools.keys().any(|tool| tool != "*" && !valid_id(tool))
            || self.ability_permissions.keys().any(|id| id != "*" && !valid_id(id)) {
            bail!("Invalid entitlement grant");
        }
        Ok(())
    }
    /// Legacy/BYO lane only. Platform access must use stable published identity.
    pub fn allows_byo_model(&self, model: &str) -> bool {
        self.model_policy.as_ref().is_none_or(|policy|policy.allow_byo)
            && self.allows_model(model)
    }
    /// Downgrades must permit removals/revocations, never new over-quota entries.
    pub fn allows_byo_growth(&self, previous:usize, next:usize) -> bool {
        if next<=previous {return true;}
        self.model_policy.as_ref().is_none_or(|policy|
            policy.allow_byo && next<=policy.max_byo_entries as usize)
    }
    pub fn allows_model(&self, model: &str) -> bool {
        self.allowed_models.iter().any(|item| item == "*" || item == model)
    }
    pub fn ability_permission(&self, id: &str) -> Permission {
        self.ability_permissions.get(id).or_else(||self.ability_permissions.get("*"))
            .copied().unwrap_or(Permission::Deny)
    }
    pub fn tool_permission(&self, name: &str) -> Permission {
        self.tools.get(name).or_else(|| self.tools.get("*"))
            .copied().unwrap_or(Permission::Deny)
    }
}
fn valid_model(value: &str) -> bool {
    !value.is_empty() && value.len() <= 128
        && value.bytes().all(|b| b.is_ascii_alphanumeric()
            || matches!(b, b'.' | b'-' | b'_' | b':' | b'/'))
}
pub fn valid_id(value: &str) -> bool {
    !value.is_empty() && value.len() <= 64
        && value.bytes().all(|b| b.is_ascii_lowercase()
            || b.is_ascii_digit() || b == b'_')
}
pub fn valid_agent_id(value: &str) -> bool {
    !value.is_empty() && value.len() <= 64
        && value.bytes().all(|b| b.is_ascii_lowercase()
            || b.is_ascii_digit() || b == b'_' || b == b'-')
}

#[derive(Clone, Debug)]
pub struct Principal {
    pub user_id: String,
    pub status: AccountStatus,
    pub role: Role,
    pub tier_id: String,
    pub access_revision: u64,
}
impl Principal {
    /// Absent fields are allowed only for the original administrator record.
    pub fn from_account(account: &Value) -> Result<Self> {
        let user_id = account["user_id"].as_str()
            .ok_or_else(|| anyhow!("Account has no stable identity"))?;
        if !valid_id(user_id) { bail!("Invalid account identity"); }
        let legacy_admin = user_id == "admin";
        let status = match account["status"].as_str() {
            Some(value) => serde_json::from_value(Value::String(value.into()))?,
            None if legacy_admin => AccountStatus::Active,
            _ => bail!("Account has no status"),
        };
        let role = match account["role"].as_str() {
            Some(value) => serde_json::from_value(Value::String(value.into()))?,
            None if legacy_admin => Role::Administrator,
            _ => bail!("Account has no role"),
        };
        let tier_id = match account["tier_id"].as_str() {
            Some(value) if valid_id(value) => value.to_owned(),
            None if legacy_admin => "operator".into(),
            _ => bail!("Account has no valid tier"),
        };
        let access_revision = match account["access_revision"].as_u64() {
            Some(value) if value > 0 => value,
            None if legacy_admin => 1,
            _ => bail!("Account has no access revision"),
        };
        Ok(Self { user_id:user_id.into(), status, role, tier_id, access_revision })
    }
    pub fn active(&self) -> bool { self.status == AccountStatus::Active }
    pub fn administrator(&self) -> bool { self.active() && self.role == Role::Administrator }
}

#[derive(Clone, Copy, Debug)]
pub struct ToolFactors {
    pub installation: Permission,
    pub tier: Permission,
    pub agent: Permission,
    pub ability: Permission,
    pub account_active: bool,
    pub relationship: bool,
    pub agent_enabled: bool,
    pub available: bool,
    pub visible: bool,
    pub read_only: bool,
    pub is_read: bool,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Reason { Allowed, ApprovalRequired, PolicyDenied, TierDenied, AgentDenied, AbilityDenied,
    AccountInactive, CallerDenied, AgentDisabled, CapabilityDenied, Unavailable, Hidden, ReadOnly }
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct ToolDecision { pub permission: Permission, pub reason: Reason }
impl ToolFactors {
    pub fn resolve(self) -> ToolDecision {
        use Permission::*;
        let denial = if self.installation == Deny { Some(Reason::PolicyDenied) }
            else if self.tier == Deny { Some(Reason::TierDenied) }
            else if self.agent == Deny { Some(Reason::AgentDenied) }
            else if self.ability == Deny { Some(Reason::AbilityDenied) }
            else if !self.account_active { Some(Reason::AccountInactive) }
            else if !self.relationship { Some(Reason::CallerDenied) }
            else if !self.agent_enabled { Some(Reason::AgentDisabled) }
            else if !self.available { Some(Reason::Unavailable) }
            else if !self.visible { Some(Reason::Hidden) }
            else if self.read_only && !self.is_read { Some(Reason::ReadOnly) }
            else { None };
        if let Some(reason) = denial {
            return ToolDecision { permission:Deny, reason };
        }
        let permission = self.installation.meet(self.tier).meet(self.agent).meet(self.ability);
        ToolDecision { permission, reason:if permission == Ask {
            Reason::ApprovalRequired
        } else { Reason::Allowed } }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn legacy_admin_is_active_but_new_accounts_need_explicit_access_fields() {
        let admin = Principal::from_account(&json!({"user_id":"admin"})).unwrap();
        assert!(admin.administrator());
        assert_eq!(admin.tier_id, "operator");
        assert!(Principal::from_account(&json!({"user_id":"second"})).is_err());
        let suspended = Principal::from_account(&json!({"user_id":"second",
            "status":"suspended","role":"administrator","tier_id":"operator",
            "access_revision":2})).unwrap();
        assert!(!suspended.administrator());
    }
    #[test]
    fn strictest_permission_and_independent_denials_win() {
        let base = ToolFactors { installation:Permission::Auto, tier:Permission::Auto,
            agent:Permission::Auto, ability:Permission::Auto,
            account_active:true, relationship:true, agent_enabled:true,
            available:true, visible:true, read_only:false, is_read:false };
        assert_eq!(base.resolve().permission, Permission::Auto);
        assert_eq!(ToolFactors {tier:Permission::Ask,..base}.resolve().reason,
            Reason::ApprovalRequired);
        assert_eq!(ToolFactors {tier:Permission::Deny,..base}.resolve().reason,
            Reason::TierDenied);
        assert_eq!(ToolFactors {ability:Permission::Deny,..base}.resolve().reason,
            Reason::AbilityDenied);
        assert_eq!(ToolFactors {ability:Permission::Ask,..base}.resolve().reason,
            Reason::ApprovalRequired);
        assert_eq!(ToolFactors {account_active:false,..base}.resolve().reason,
            Reason::AccountInactive);
        assert_eq!(ToolFactors {relationship:false,..base}.resolve().reason,
            Reason::CallerDenied);
        assert_eq!(ToolFactors {available:false,..base}.resolve().reason,
            Reason::Unavailable);
        assert_eq!(ToolFactors {visible:false,..base}.resolve().reason,
            Reason::Hidden);
        assert_eq!(ToolFactors {read_only:true,..base}.resolve().reason,
            Reason::ReadOnly);
        assert_eq!(ToolFactors {read_only:true,is_read:true,..base}.resolve().permission,
            Permission::Auto);
    }
    #[test]
    fn byo_downgrade_blocks_use_and_growth_but_allows_reduction() {
        let mut tier=Tier{id:"basic".into(),revision:1,model_policy:None,
            allowed_models:vec!["*".into()],tools:BTreeMap::new(),ability_permissions:legacy_ability_grants(),
            max_sessions:10,max_active_runs:2};
        assert!(tier.allows_byo_model("deepseek-chat"));
        assert!(tier.allows_byo_growth(0,1));
        tier.model_policy=Some(ModelPolicy{roster_id:None,allowed_entry_ids:Default::default(),
            allow_byo:false,max_byo_entries:0});
        assert!(!tier.allows_byo_model("deepseek-chat"));
        assert!(!tier.allows_byo_growth(0,1));
        assert!(tier.allows_byo_growth(3,3));
        assert!(tier.allows_byo_growth(3,2));
        let policy=tier.model_policy.as_mut().unwrap();
        policy.allow_byo=true;policy.max_byo_entries=2;
        assert!(tier.allows_byo_growth(1,2));
        assert!(!tier.allows_byo_growth(2,3));
        assert!(!tier.allows_byo_growth(3,4));
        tier.allowed_models=vec!["deepseek-chat".into()];
        assert!(!tier.allows_byo_model("other"));
        assert!(tier.allows_byo_model("deepseek-chat"));
    }
    #[test]
    fn tier_is_explicit_and_bounded() {
        let mut tier = Tier {id:"basic".into(),revision:1,model_policy:None,
            allowed_models:vec!["gpt-6-luna".into()],tools:BTreeMap::new(),ability_permissions:legacy_ability_grants(),
            max_sessions:10,max_active_runs:2};
        tier.validate().unwrap();
        assert!(!tier.allows_model("other"));
        assert_eq!(tier.tool_permission("file_write"), Permission::Deny);
        tier.tools.insert("file_write".into(), Permission::Ask);
        assert_eq!(tier.tool_permission("file_write"), Permission::Ask);
        tier.max_active_runs=201;
        assert!(tier.validate().is_err());
    }
}
