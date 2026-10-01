//! Model selection records and immutable run plans. No secrets belong in these records.
//! The caller must persist the plan at admission and recheck live grants/revocation
//! before EVERY dispatch. Catalog edits cannot mutate an accepted plan.
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

pub const MAX_CANDIDATES: usize = 8;

/// One atomic catalog snapshot. Credentials are stored separately in encrypted docs.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Catalog {
    pub revision: u64,
    pub connections: BTreeMap<String, Connection>,
    pub entries: BTreeMap<String, ModelEntry>,
    pub groups: BTreeMap<String, BackupGroup>,
    pub assignments: BTreeMap<String, Assignment>,
}

impl Catalog {
    pub fn validate(&self) -> Result<()> {
        if self.connections.len() > 256 || self.entries.len() > 1024
            || self.groups.len() > 256 || self.assignments.len() > 1024 {
            bail!("Model catalog exceeds record limits");
        }
        for (id, c) in &self.connections {
            if id != &c.id || !valid_id(id) || !valid_id(&c.owner_id)
                || !valid_id(&c.credential_ref) || c.revision == 0
                || c.allowed_agents.iter().any(|id| !valid_id(id)) {
                bail!("Invalid connection record");
            }
        }
        for (id, e) in &self.entries {
            if id != &e.id || !valid_id(id) || !valid_id(&e.owner_id)
                || e.upstream_model.is_empty() || e.upstream_model.len()>128
                || !e.upstream_model.chars().all(|c|c.is_ascii_alphanumeric() || "-._".contains(c))
                || e.display_name.trim().is_empty()
                || e.display_name.len() > 200 || e.revision == 0
                || e.allowed_agents.iter().any(|id| !valid_id(id))
                || !self.connections.contains_key(&e.connection_id) {
                bail!("Invalid model entry record");
            }
        }
        for (id, g) in &self.groups {
            let unique: BTreeSet<_> = g.entry_ids.iter().collect();
            if id != &g.id || !valid_id(id) || !valid_id(&g.owner_id) || g.revision == 0
                || g.entry_ids.len() > MAX_CANDIDATES || unique.len() != g.entry_ids.len()
                || g.entry_ids.iter().any(|id| !self.entries.contains_key(id)) {
                bail!("Invalid backup group record");
            }
        }
        for (id, a) in &self.assignments {
            if id != &a.agent_id { bail!("Assignment key does not match agent"); }
            if !valid_id(id) || !valid_id(&a.owner_id) || a.revision == 0
                || !self.entries.contains_key(&a.primary_entry_id)
                || a.backup_group_id.as_ref().is_some_and(|id| !self.groups.contains_key(id)) {
                bail!("Invalid assignment record");
            }
        }
        Ok(())
    }

    pub fn resolve(&self, agent_id: &str) -> Result<SelectionPlan> {
        let assignment = self.assignments.get(agent_id)
            .ok_or_else(|| anyhow::anyhow!("Agent has no explicit model assignment"))?;
        SelectionPlan::resolve(assignment, &self.entries, &self.connections, &self.groups)
    }
}

const CATALOG_KEY: &str = "model_selection:catalog:v1";

impl crate::store::Store {
    pub async fn model_catalog(&self) -> Result<Catalog> {
        use sqlx::Row;
        let row = sqlx::query("SELECT value FROM docs WHERE key=?")
            .bind(CATALOG_KEY).fetch_optional(&self.app_pool).await?;
        match row {
            Some(row) => Ok(serde_json::from_value(self.unseal(&row.get::<Vec<u8>, _>("value"))?)?),
            None => Ok(Catalog::default()),
        }
    }

    /// Administrator API must authorize the caller before invoking this method.
    /// Acquire a database write lock BEFORE reading the revision, including first insert.
    pub async fn save_model_catalog(&self, catalog: &Catalog, expected_revision: u64) -> Result<Catalog> {
        use sqlx::Row;
        catalog.validate()?;
        let mut tx = self.app_pool.begin().await?;
        sqlx::query("UPDATE docs SET value=value WHERE key=?")
            .bind(CATALOG_KEY).execute(&mut *tx).await?;
        let row = sqlx::query("SELECT value FROM docs WHERE key=?")
            .bind(CATALOG_KEY).fetch_optional(&mut *tx).await?;
        let previous: Catalog = match row {
            Some(row) => serde_json::from_value(self.unseal(&row.get::<Vec<u8>, _>("value"))?)?,
            None => Catalog::default(),
        };
        if previous.revision != expected_revision || catalog.revision != expected_revision {
            bail!("Model catalog revision changed");
        }
        let mut saved = catalog.clone();
        // Clients submit the last observed record revision. The server advances it.
        macro_rules! revisions {
            ($field:ident) => {
                for (id, record) in &mut saved.$field {
                    if let Some(old) = previous.$field.get(id) {
                        if record.owner_id != old.owner_id || record.revision != old.revision {
                            bail!("Model record ownership or revision changed");
                        }
                        if record != old {
                            record.revision = old.revision.checked_add(1)
                                .ok_or_else(|| anyhow::anyhow!("Model record revision exhausted"))?;
                        }
                    } else if record.revision != 1 {
                        bail!("New model record revision must be one");
                    }
                }
            };
        }
        revisions!(connections);
        revisions!(entries);
        revisions!(groups);
        revisions!(assignments);
        for (id, assignment) in &saved.assignments {
            let assignment_changed = previous.assignments.get(id) != catalog.assignments.get(id);
            let group_changed = assignment.backup_group_id.as_ref().is_some_and(|group_id| {
                previous.groups.get(group_id).map(|g| &g.entry_ids)
                    != saved.groups.get(group_id).map(|g| &g.entry_ids)
            });
            if assignment_changed || group_changed {
                // Existing revoked assignments can remain so that revocation cannot
                // be blocked by their references; new use must authorize every candidate.
                saved.resolve(id)?;
            }
        }
        saved.revision = expected_revision.checked_add(1)
            .ok_or_else(|| anyhow::anyhow!("Model catalog revision exhausted"))?;
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(CATALOG_KEY).bind(self.seal(&serde_json::to_value(&saved)?)?)
            .execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(saved)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Transport { OpenaiSubscription, Deepseek }

impl Transport {
    pub fn provider_id(&self) -> crate::provider::ProviderId {
        match self { Self::OpenaiSubscription => crate::provider::ProviderId::OpenaiSubscription,
            Self::Deepseek => crate::provider::ProviderId::Deepseek }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Connection {
    pub id: String,
    pub owner_id: String,
    pub transport: Transport,
    /// Reference to an encrypted credential document, never a token or password.
    pub credential_ref: String,
    pub allowed_agents: BTreeSet<String>,
    pub revision: u64,
    pub revoked: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelEntry {
    pub id: String,
    pub owner_id: String,
    pub connection_id: String,
    pub upstream_model: String,
    pub display_name: String,
    pub capabilities: BTreeSet<String>,
    pub allowed_agents: BTreeSet<String>,
    pub revision: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BackupGroup {
    pub id: String,
    pub owner_id: String,
    /// Explicit order, independent of model-table display order. May include primary.
    pub entry_ids: Vec<String>,
    pub revision: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Assignment {
    pub agent_id: String,
    pub owner_id: String,
    pub primary_entry_id: String,
    pub backup_group_id: Option<String>,
    pub revision: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Candidate {
    pub entry_id: String,
    pub entry_revision: u64,
    pub connection_id: String,
    pub connection_revision: u64,
    pub credential_ref: String,
    pub transport: Transport,
    pub upstream_model: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SelectionPlan {
    pub version: u32,
    pub agent_id: String,
    pub owner_id: String,
    pub assignment_revision: u64,
    pub group_revision: Option<u64>,
    pub candidates: Vec<Candidate>,
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 128 && id.bytes().all(|c| c.is_ascii_alphanumeric() || b"-._:".contains(&c))
}

fn granted(owner: &str, allowed: &BTreeSet<String>, assignment: &Assignment) -> bool {
    owner == assignment.owner_id || allowed.contains(&assignment.agent_id)
}

impl SelectionPlan {
    pub fn resolve(
        assignment: &Assignment,
        entries: &BTreeMap<String, ModelEntry>,
        connections: &BTreeMap<String, Connection>,
        groups: &BTreeMap<String, BackupGroup>,
    ) -> Result<Self> {
        if !valid_id(&assignment.agent_id) || !valid_id(&assignment.owner_id)
            || !valid_id(&assignment.primary_entry_id) || assignment.revision == 0 {
            bail!("Invalid model assignment");
        }
        let mut ids = vec![assignment.primary_entry_id.clone()];
        let mut group_revision = None;
        if let Some(id) = &assignment.backup_group_id {
            let group = groups.get(id).ok_or_else(|| anyhow::anyhow!("Backup group not found"))?;
            if group.id != *id || !valid_id(id) || group.owner_id != assignment.owner_id || group.revision == 0 {
                bail!("Backup group is not authorized");
            }
            if group.entry_ids.len() > MAX_CANDIDATES { bail!("Too many backup candidates"); }
            let mut unique = BTreeSet::new();
            for entry_id in &group.entry_ids {
                if !valid_id(entry_id) || !unique.insert(entry_id) { bail!("Invalid or duplicate backup entry"); }
                if entry_id != &assignment.primary_entry_id { ids.push(entry_id.clone()); }
            }
            group_revision = Some(group.revision);
        }
        if ids.len() > MAX_CANDIDATES { bail!("Too many selection candidates"); }
        let mut candidates = Vec::new();
        for id in ids {
            let entry = entries.get(&id).ok_or_else(|| anyhow::anyhow!("Assigned model entry not found"))?;
            let connection = connections.get(&entry.connection_id)
                .ok_or_else(|| anyhow::anyhow!("Model connection not found"))?;
            if entry.id != id || connection.id != entry.connection_id || entry.revision == 0
                || connection.revision == 0 || !valid_id(&connection.id)
                || !valid_id(&connection.credential_ref) || !valid_id(&entry.upstream_model)
                || entry.display_name.len() > 200 || !entry.capabilities.contains("text") {
                bail!("Invalid model or connection record");
            }
            if connection.revoked || !granted(&entry.owner_id, &entry.allowed_agents, assignment)
                || !granted(&connection.owner_id, &connection.allowed_agents, assignment) {
                bail!("Model candidate is not authorized");
            }
            candidates.push(Candidate {
                entry_id: entry.id.clone(), entry_revision: entry.revision,
                connection_id: connection.id.clone(), connection_revision: connection.revision,
                credential_ref: connection.credential_ref.clone(), transport: connection.transport.clone(),
                upstream_model: entry.upstream_model.clone(),
            });
        }
        Ok(Self { version: 1, agent_id: assignment.agent_id.clone(), owner_id: assignment.owner_id.clone(),
            assignment_revision: assignment.revision, group_revision, candidates })
    }

    /// Live authorization is separate from snapshot revision: ordinary edits do not
    /// reroute a run, but deletion, credential replacement, or revocation blocks it.
    pub fn authorize_candidate(&self, index: usize, entries: &BTreeMap<String, ModelEntry>,
        connections: &BTreeMap<String, Connection>) -> Result<&Candidate> {
        let candidate = self.candidates.get(index).ok_or_else(|| anyhow::anyhow!("Selection candidates exhausted"))?;
        let entry = entries.get(&candidate.entry_id).ok_or_else(|| anyhow::anyhow!("Model entry removed"))?;
        let connection = connections.get(&candidate.connection_id).ok_or_else(|| anyhow::anyhow!("Model connection removed"))?;
        let assignment = Assignment { agent_id: self.agent_id.clone(), owner_id: self.owner_id.clone(),
            primary_entry_id: candidate.entry_id.clone(), backup_group_id: None, revision: self.assignment_revision };
        if entry.id != candidate.entry_id || connection.id != candidate.connection_id
            || connection.revoked || connection.credential_ref != candidate.credential_ref
            || connection.transport != candidate.transport
            || !granted(&entry.owner_id, &entry.allowed_agents, &assignment)
            || !granted(&connection.owner_id, &connection.allowed_agents, &assignment) {
            bail!("Pinned model candidate is no longer authorized");
        }
        Ok(candidate)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureKind { Transient, QuotaExhausted, InvalidCredentials, ModelUnavailable, MalformedRequest, Cancelled, PolicyRejected, Protocol }

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FailoverState {
    pub candidate_index: usize,
    pub retries: u8,
    pub response_started: bool,
    pub uncertain_tool_effect: bool,
    pub history: Vec<FailoverEvent>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FailoverEvent { pub from_entry_id: String, pub to_entry_id: String, pub reason: FailureKind }
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NextAction { Retry, Backup, Terminal }
impl Default for FailoverState {
    fn default() -> Self { Self { candidate_index: 0, retries: 0, response_started: false, uncertain_tool_effect: false, history: Vec::new() } }
}
impl FailoverState {
    /// At most one transient retry per candidate; never replay partial output or
    /// uncertain effects. Caller must durably save this state before redispatch.
    pub fn failed(&mut self, plan: &SelectionPlan, kind: FailureKind) -> NextAction {
        if self.response_started || self.uncertain_tool_effect || self.candidate_index >= plan.candidates.len()
            || matches!(kind, FailureKind::MalformedRequest | FailureKind::Cancelled | FailureKind::PolicyRejected | FailureKind::Protocol) {
            return NextAction::Terminal;
        }
        if kind == FailureKind::Transient && self.retries < 1 {
            self.retries += 1;
            return NextAction::Retry;
        }
        let next = self.candidate_index + 1;
        if next >= plan.candidates.len() { return NextAction::Terminal; }
        self.history.push(FailoverEvent { from_entry_id: plan.candidates[self.candidate_index].entry_id.clone(),
            to_entry_id: plan.candidates[next].entry_id.clone(), reason: kind });
        self.candidate_index = next;
        self.retries = 0;
        NextAction::Backup
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (Assignment, BTreeMap<String, ModelEntry>, BTreeMap<String, Connection>, BTreeMap<String, BackupGroup>) {
        let assignment = Assignment { agent_id: "agent-a".into(), owner_id: "owner".into(), primary_entry_id: "a".into(), backup_group_id: Some("group".into()), revision: 1 };
        let mut entries = BTreeMap::new(); let mut connections = BTreeMap::new();
        for id in ["a", "b", "c"] {
            connections.insert(id.into(), Connection { id: id.into(), owner_id: "owner".into(), transport: Transport::OpenaiSubscription,
                credential_ref: format!("credential:{id}"), allowed_agents: BTreeSet::new(), revision: 1, revoked: false });
            entries.insert(id.into(), ModelEntry { id: id.into(), owner_id: "owner".into(), connection_id: id.into(), upstream_model: "same-upstream".into(),
                display_name: id.into(), capabilities: BTreeSet::from(["text".into()]), allowed_agents: BTreeSet::new(), revision: 1 });
        }
        let groups = BTreeMap::from([("group".into(), BackupGroup { id: "group".into(), owner_id: "owner".into(), entry_ids: vec!["c".into(), "a".into(), "b".into()], revision: 3 })]);
        (assignment, entries, connections, groups)
    }
    fn catalog_fixture() -> Catalog {
        let (assignment, entries, connections, mut groups) = fixture();
        for group in groups.values_mut() { group.revision = 1; }
        Catalog { revision: 0, entries, connections, groups,
            assignments: BTreeMap::from([(assignment.agent_id.clone(), assignment)]) }
    }
    #[tokio::test]
    async fn encrypted_catalog_round_trip_and_revision_conflict() {
        let root = tempfile::tempdir().unwrap();
        let store = crate::store::Store::open(root.path()).await.unwrap();
        assert_eq!(store.model_catalog().await.unwrap(), Catalog::default());
        let saved = store.save_model_catalog(&catalog_fixture(), 0).await.unwrap();
        assert_eq!(saved.revision, 1);
        assert_eq!(store.model_catalog().await.unwrap(), saved);
        assert!(store.save_model_catalog(&catalog_fixture(), 0).await.is_err());
        let raw: Vec<u8> = sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
            .bind(CATALOG_KEY).fetch_one(&store.app_pool).await.unwrap();
        assert!(!raw.windows(b"same-upstream".len()).any(|x| x == b"same-upstream"));
        let mut changed = saved.clone();
        changed.entries.get_mut("a").unwrap().display_name = "Renamed".into();
        let updated = store.save_model_catalog(&changed, 1).await.unwrap();
        assert_eq!(updated.entries["a"].revision, 2);
        assert_eq!(updated.entries["b"].revision, 1);
        store.app_pool.close().await; store.pool.close().await; store.agent_pool.close().await;
        let reopened = crate::store::Store::open(root.path()).await.unwrap();
        assert_eq!(reopened.model_catalog().await.unwrap(), updated);
    }
    #[tokio::test]
    async fn revocation_allowed_but_new_assignment_and_owner_transfer_rejected() {
        let root = tempfile::tempdir().unwrap();
        let store = crate::store::Store::open(root.path()).await.unwrap();
        let mut catalog = store.save_model_catalog(&catalog_fixture(), 0).await.unwrap();
        catalog.connections.get_mut("a").unwrap().revoked = true;
        catalog = store.save_model_catalog(&catalog, 1).await.unwrap();
        assert!(catalog.resolve("agent-a").is_err());
        let mut transfer = catalog.clone();
        transfer.entries.get_mut("a").unwrap().owner_id = "other-owner".into();
        assert!(store.save_model_catalog(&transfer, 2).await.is_err());
        let mut assignment = catalog.assignments["agent-a"].clone();
        assignment.agent_id = "agent-b".into();
        catalog.assignments.insert("agent-b".into(), assignment);
        assert!(store.save_model_catalog(&catalog, 2).await.is_err());
        assert_eq!(store.model_catalog().await.unwrap().revision, 2);
    }
    #[test] fn explicit_order_and_distinct_connections() {
        let (a,e,c,g) = fixture(); let p = SelectionPlan::resolve(&a,&e,&c,&g).unwrap();
        assert_eq!(p.candidates.iter().map(|x| x.entry_id.as_str()).collect::<Vec<_>>(), vec!["a","c","b"]);
        assert_ne!(p.candidates[0].credential_ref,p.candidates[1].credential_ref);
        assert_eq!(p.group_revision,Some(3));
    }
    #[test] fn editing_assignment_does_not_reroute_snapshot() {
        let (mut a,mut e,c,g) = fixture(); let p = SelectionPlan::resolve(&a,&e,&c,&g).unwrap();
        a.primary_entry_id = "b".into(); a.revision += 1; e.get_mut("a").unwrap().upstream_model = "changed".into();
        assert_eq!(p.authorize_candidate(0,&e,&c).unwrap().upstream_model,"same-upstream");
        assert_eq!(SelectionPlan::resolve(&a,&e,&c,&g).unwrap().candidates[0].entry_id,"b");
    }
    #[test] fn every_backup_requires_model_and_connection_grants() {
        let (a,mut e,mut c,g) = fixture(); e.get_mut("c").unwrap().owner_id = "private".into();
        assert!(SelectionPlan::resolve(&a,&e,&c,&g).is_err());
        e.get_mut("c").unwrap().allowed_agents.insert(a.agent_id.clone()); c.get_mut("c").unwrap().owner_id = "private".into();
        assert!(SelectionPlan::resolve(&a,&e,&c,&g).is_err());
        c.get_mut("c").unwrap().allowed_agents.insert(a.agent_id.clone());
        let p = SelectionPlan::resolve(&a,&e,&c,&g).unwrap();
        c.get_mut("c").unwrap().revoked = true; assert!(p.authorize_candidate(1,&e,&c).is_err());
    }
    #[test] fn replaced_credentials_are_not_silently_used() {
        let (a,e,mut c,g) = fixture(); let p = SelectionPlan::resolve(&a,&e,&c,&g).unwrap();
        c.get_mut("a").unwrap().credential_ref = "new-credential".into();
        assert!(p.authorize_candidate(0,&e,&c).is_err());
    }
    #[test] fn duplicate_or_missing_group_rejected() {
        let (a,e,c,mut g) = fixture(); g.get_mut("group").unwrap().entry_ids.push("c".into());
        assert!(SelectionPlan::resolve(&a,&e,&c,&g).is_err());
        g.clear(); assert!(SelectionPlan::resolve(&a,&e,&c,&g).is_err());
    }
    #[test] fn bounded_retry_then_ordered_failover_then_terminal() {
        let (a,e,c,g) = fixture(); let p = SelectionPlan::resolve(&a,&e,&c,&g).unwrap(); let mut s = FailoverState::default();
        assert_eq!(s.failed(&p,FailureKind::Transient),NextAction::Retry);
        assert_eq!(s.failed(&p,FailureKind::Transient),NextAction::Backup);
        assert_eq!(s.history[0].to_entry_id,"c");
        assert_eq!(s.failed(&p,FailureKind::QuotaExhausted),NextAction::Backup);
        assert_eq!(s.history[1].to_entry_id,"b");
        assert_eq!(s.failed(&p,FailureKind::InvalidCredentials),NextAction::Terminal);
    }
    #[test] fn unsafe_failures_never_failover() {
        let (a,e,c,g) = fixture(); let p = SelectionPlan::resolve(&a,&e,&c,&g).unwrap();
        for kind in [FailureKind::MalformedRequest,FailureKind::Cancelled,FailureKind::PolicyRejected,FailureKind::Protocol] {
            assert_eq!(FailoverState::default().failed(&p,kind),NextAction::Terminal);
        }
        let mut s = FailoverState { response_started: true, ..Default::default() };
        assert_eq!(s.failed(&p,FailureKind::Transient),NextAction::Terminal);
        let mut s = FailoverState { uncertain_tool_effect: true, ..Default::default() };
        assert_eq!(s.failed(&p,FailureKind::ModelUnavailable),NextAction::Terminal);
    }
    #[test] fn snapshot_and_history_round_trip() {
        let (a,e,c,g) = fixture(); let p = SelectionPlan::resolve(&a,&e,&c,&g).unwrap();
        assert_eq!(p,serde_json::from_str::<SelectionPlan>(&serde_json::to_string(&p).unwrap()).unwrap());
        let mut s = FailoverState::default(); s.failed(&p,FailureKind::ModelUnavailable);
        assert_eq!(s,serde_json::from_str::<FailoverState>(&serde_json::to_string(&s).unwrap()).unwrap());
    }
}
