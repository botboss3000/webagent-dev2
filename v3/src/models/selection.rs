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
    #[serde(default)]
    pub platform_sources: BTreeMap<String, crate::account_catalog::PlatformPin>,
    pub connections: BTreeMap<String, Connection>,
    pub entries: BTreeMap<String, ModelEntry>,
    pub groups: BTreeMap<String, BackupGroup>,
    pub assignments: BTreeMap<String, Assignment>,
    #[serde(default)]
    pub agent_models: BTreeMap<String, AgentModels>,
}

impl Catalog {
    pub fn validate(&self) -> Result<()> {
        if self.connections.len() > 256 || self.entries.len() > 1024
            || self.groups.len() > 256 || self.assignments.len() > 1024 || self.agent_models.len() > 1024 {
            bail!("Model catalog exceeds record limits");
        }
        for (id,pin) in &self.platform_sources {
            if !self.entries.contains_key(id) || !id.starts_with("platform:") || !crate::access::valid_id(&pin.roster_id) {bail!("Invalid platform provenance");}
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
                || !crate::platform_catalog::valid_model(&e.upstream_model)
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
            // Retained legacy records are rollback material once explicit roles exist.
            if self.agent_models.contains_key(id) { continue; }
            if id != &a.agent_id { bail!("Assignment key does not match agent"); }
            if !valid_id(id) || !valid_id(&a.owner_id) || a.revision == 0
                || !self.entries.contains_key(&a.primary_entry_id)
                || a.backup_group_id.as_ref().is_some_and(|id| !self.groups.contains_key(id)) {
                bail!("Invalid assignment record");
            }
        }
        for (id, roster) in &self.agent_models {
            if id != &roster.agent_id || !valid_id(id) || !valid_id(&roster.owner_id)
                || roster.revision == 0 || roster.entry_ids.len() > 256
                || roster.entry_ids.iter().collect::<BTreeSet<_>>().len() != roster.entry_ids.len()
                || roster.entry_ids.iter().any(|id| !self.entries.contains_key(id)) {
                bail!("Invalid agent model roster");
            }
            for (role, appointment) in &roster.roles {
                let ids = appointment.candidates()?;
                for id in ids {
                    let entry = self.entries.get(id).ok_or_else(|| anyhow::anyhow!("Role entry missing"))?;
                    if !roster.entry_ids.contains(id) || !role.supported_by(entry) {
                        bail!("Role entry is outside roster or lacks required capability");
                    }
                }
            }
        }
        Ok(())
    }

    /// Only persisted legacy assignments confer their former same-owner grants.
    /// An explicit role map, even an empty one, prevents a cleared Standard returning.
    pub fn migrate_legacy(&mut self) -> Result<()> {
        for (id, assignment) in &self.assignments {
            if self.agent_models.contains_key(id) { continue; }
            let mut ids = vec![assignment.primary_entry_id.clone()];
            if let Some(group_id) = &assignment.backup_group_id {
                let group = self.groups.get(group_id).ok_or_else(|| anyhow::anyhow!("Legacy backup group missing"))?;
                if group.owner_id != assignment.owner_id { bail!("Legacy backup owner mismatch"); }
                for entry in &group.entry_ids {
                    if entry != &assignment.primary_entry_id { ids.push(entry.clone()); }
                }
            }
            let appointment = RoleAssignment { entry_id: assignment.primary_entry_id.clone(), backup_entry_ids: ids[1..].to_vec() };
            appointment.candidates()?;
            for entry_id in &ids {
                let entry = self.entries.get_mut(entry_id).ok_or_else(|| anyhow::anyhow!("Legacy model missing"))?;
                if entry.owner_id == assignment.owner_id { entry.allowed_agents.insert(id.clone()); }
                let connection = self.connections.get_mut(&entry.connection_id).ok_or_else(|| anyhow::anyhow!("Legacy connection missing"))?;
                if connection.owner_id == assignment.owner_id { connection.allowed_agents.insert(id.clone()); }
            }
            self.agent_models.insert(id.clone(), AgentModels { agent_id: id.clone(), owner_id: assignment.owner_id.clone(),
                entry_ids: ids, roles: BTreeMap::from([(ModelRole::Standard, appointment)]), revision: assignment.revision });
        }
        self.validate()
    }

    // Stored migration separates legacy sharing authority from tenant ownership.
    // First preserve grants under the old assignment owner, then appoint the directory
    // owner to newly projected rosters only. Explicit role records never transfer here.
    pub(crate) async fn migrate_stored(&mut self, connection: &mut sqlx::SqliteConnection) -> Result<()> {
        let mut owners = BTreeMap::new();
        for id in self.assignments.keys().filter(|id| !self.agent_models.contains_key(*id)) {
            let owner: Option<String> = sqlx::query_scalar("SELECT owner_id FROM agent_directory WHERE id=?")
                .bind(id).fetch_optional(&mut *connection).await?;
            if let Some(owner) = owner { owners.insert(id.clone(), owner); }
        }
        self.migrate_legacy()?;
        for (id, owner) in owners {
            self.agent_models.get_mut(&id).expect("Legacy roster was projected").owner_id = owner;
        }
        self.validate()
    }

    // Admission uses explicit appointments; missing specialized roles never inherit Standard.
    pub fn resolve_role(&self, agent_id: &str, role: ModelRole) -> Result<SelectionPlan> {
        if let Some(roster) = self.agent_models.get(agent_id) {
            let appointment = roster.roles.get(&role).ok_or_else(|| anyhow::anyhow!("{} model required", role.label()))?;
            return self.resolve_roster(roster, role, appointment, None);
        }
        if role != ModelRole::Standard { bail!("{} model required", role.label()); }
        let assignment = self.assignments.get(agent_id).ok_or_else(|| anyhow::anyhow!("Standard model required"))?;
        let mut plan = SelectionPlan::resolve(assignment, &self.entries, &self.connections, &self.groups)?;
        plan.catalog_revision = self.revision;
        Ok(plan)
    }

    /// Custom chat uses an explicit roster entry and never changes Standard or inherits its backups.
    pub fn resolve_custom(&self, agent_id: &str, entry_id: &str) -> Result<SelectionPlan> {
        let roster = self.agent_models.get(agent_id).ok_or_else(|| anyhow::anyhow!("Agent model roster required"))?;
        self.resolve_roster(roster, ModelRole::Standard,
            &RoleAssignment { entry_id: entry_id.into(), backup_entry_ids: vec![] }, Some(entry_id.into()))
    }

    fn resolve_roster(&self, roster: &AgentModels, role: ModelRole, appointment: &RoleAssignment,
        custom_entry_id: Option<String>) -> Result<SelectionPlan> {
        if !valid_id(&roster.agent_id) || !valid_id(&roster.owner_id) || roster.revision == 0 {
            bail!("Invalid agent model roster");
        }
        let mut candidates = Vec::new();
        for id in appointment.candidates()? {
            if !roster.entry_ids.contains(id) { bail!("Selected model is outside agent roster"); }
            let entry = self.entries.get(id).ok_or_else(|| anyhow::anyhow!("Role entry missing"))?;
            let connection = self.connections.get(&entry.connection_id).ok_or_else(|| anyhow::anyhow!("Model connection missing"))?;
            if !role.supported_by(entry) || entry.id != *id || connection.id != entry.connection_id
                || entry.revision == 0 || connection.revision == 0 || connection.revoked
                || !entry.allowed_agents.contains(&roster.agent_id) || !connection.allowed_agents.contains(&roster.agent_id) {
                bail!("Role model is incompatible or not authorized");
            }
            candidates.push(Candidate { platform:self.platform_sources.get(id).cloned(), entry_id: id.clone(), entry_revision: entry.revision,
                connection_id: connection.id.clone(), connection_revision: connection.revision,
                credential_ref: connection.credential_ref.clone(), transport: connection.transport.clone(), upstream_model: entry.upstream_model.clone() });
        }
        Ok(SelectionPlan { version: 2, agent_id: roster.agent_id.clone(), owner_id: roster.owner_id.clone(),
            assignment_revision: roster.revision, group_revision: None, candidates, role, custom_entry_id,
            roster_revision: roster.revision, catalog_revision: self.revision })
    }

    pub fn resolve(&self, agent_id: &str) -> Result<SelectionPlan> {
        self.resolve_role(agent_id, ModelRole::Standard)
    }
}

const CATALOG_KEY: &str = "model_selection:catalog:v1";

impl crate::store::Store {
    pub async fn model_catalog(&self) -> Result<Catalog> {
        use sqlx::Row;
        let mut connection = self.app_pool.acquire().await?;
        let row = sqlx::query("SELECT value FROM docs WHERE key=?")
            .bind(CATALOG_KEY).fetch_optional(&mut *connection).await?;
        let mut catalog: Catalog = match row {
            Some(row) => serde_json::from_value(self.unseal(&row.get::<Vec<u8>, _>("value"))?)?,
            None => Catalog::default(),
        };
        catalog.migrate_stored(&mut connection).await?;
        Ok(catalog)
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
        let mut previous: Catalog = match row {
            Some(row) => serde_json::from_value(self.unseal(&row.get::<Vec<u8>, _>("value"))?)?,
            None => Catalog::default(),
        };
        previous.migrate_stored(&mut tx).await?;
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
        revisions!(agent_models);
        for id in previous.agent_models.keys() {
            if !saved.agent_models.contains_key(id) {
                bail!("Clear roles and roster explicitly instead of removing the agent model record");
            }
            if previous.assignments.get(id) != saved.assignments.get(id) {
                bail!("Legacy assignment is read-only after role migration");
            }
        }
        // Catalog writes authorize newly appointed uses while allowing grants to be
        // revoked from existing appointments. Dispatch checks that live revocation.
        for (id, roster) in &saved.agent_models {
            let previous_roster = previous.agent_models.get(id);
            for entry_id in &roster.entry_ids {
                if previous_roster.is_none_or(|old| !old.entry_ids.contains(entry_id)) {
                    let entry = &saved.entries[entry_id];
                    let connection = &saved.connections[&entry.connection_id];
                    if connection.revoked || !entry.allowed_agents.contains(id) || !connection.allowed_agents.contains(id) {
                        bail!("New roster entry is not authorized");
                    }
                }
            }
            for (role, appointment) in &roster.roles {
                if previous_roster.and_then(|old| old.roles.get(role)) != Some(appointment) {
                    saved.resolve_role(id, *role)?;
                }
            }
        }
        for (id, assignment) in &saved.assignments {
            if saved.agent_models.contains_key(id) { continue; }
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
        saved.migrate_stored(&mut tx).await?;
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
pub enum Transport { OpenaiSubscription, Deepseek, OpenaiApi, Openrouter, Abacus, Gemini, Groq, Together, Mistral, Fireworks, Xai, Perplexity, Deepinfra }

impl Transport {
    pub fn uses_api_key(&self) -> bool { self.provider_id().chat_endpoint().is_some() }
    pub fn provider_id(&self) -> crate::provider::ProviderId {
        match self { Self::OpenaiSubscription => crate::provider::ProviderId::OpenaiSubscription,
            Self::Deepseek => crate::provider::ProviderId::Deepseek,
            Self::OpenaiApi => crate::provider::ProviderId::OpenaiApi,
            Self::Openrouter => crate::provider::ProviderId::Openrouter,
            Self::Abacus => crate::provider::ProviderId::Abacus,
            Self::Gemini => crate::provider::ProviderId::Gemini,
            Self::Groq => crate::provider::ProviderId::Groq,
            Self::Together => crate::provider::ProviderId::Together,
            Self::Mistral => crate::provider::ProviderId::Mistral,
            Self::Fireworks => crate::provider::ProviderId::Fireworks,
            Self::Xai => crate::provider::ProviderId::Xai,
            Self::Perplexity => crate::provider::ProviderId::Perplexity,
            Self::Deepinfra => crate::provider::ProviderId::Deepinfra,
        }
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

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
// Capabilities describe support; these six semantic appointments select workflows.
pub enum ModelRole { #[default] Standard, Premium, ImageIn, ImageOut, System, Voice }
impl ModelRole {
    pub fn label(self) -> &'static str {
        match self { Self::Standard => "Standard", Self::Premium => "Premium", Self::ImageIn => "Image In",
            Self::ImageOut => "Image Out", Self::System => "System", Self::Voice => "Voice" }
    }
    pub fn supported_by(self, entry: &ModelEntry) -> bool {
        entry.capabilities.contains(match self { Self::Standard | Self::Premium | Self::System => "text",
            Self::ImageIn => "image_in", Self::ImageOut => "image_out", Self::Voice => "voice" })
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentModels {
    pub agent_id: String,
    pub owner_id: String,
    pub entry_ids: Vec<String>,
    pub roles: BTreeMap<ModelRole, RoleAssignment>,
    pub revision: u64,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RoleAssignment {
    pub entry_id: String,
    #[serde(default)]
    pub backup_entry_ids: Vec<String>,
}
impl RoleAssignment {
    fn candidates(&self) -> Result<Vec<&String>> {
        let ids: Vec<_> = std::iter::once(&self.entry_id).chain(&self.backup_entry_ids).collect();
        if ids.len() > MAX_CANDIDATES || ids.iter().any(|id| !valid_id(id))
            || ids.iter().copied().collect::<BTreeSet<_>>().len() != ids.len() {
            bail!("Invalid or duplicate role backup candidates");
        }
        Ok(ids)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Candidate {
    #[serde(default)]
    pub platform: Option<crate::account_catalog::PlatformPin>,
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
    #[serde(default)]
    pub role: ModelRole,
    #[serde(default)]
    pub custom_entry_id: Option<String>,
    #[serde(default)]
    pub roster_revision: u64,
    #[serde(default)]
    pub catalog_revision: u64,
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 128 && id.bytes().all(|c| c.is_ascii_alphanumeric() || b"-._:".contains(&c))
}

// Ownership permits administration, never implicit runtime sharing between agents.
fn granted(_owner: &str, allowed: &BTreeSet<String>, assignment: &Assignment) -> bool {
    allowed.contains(&assignment.agent_id)
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
                || !valid_id(&connection.credential_ref) || !crate::platform_catalog::valid_model(&entry.upstream_model)
                || entry.display_name.len() > 200 || !entry.capabilities.contains("text") {
                bail!("Invalid model or connection record");
            }
            if connection.revoked || !granted(&entry.owner_id, &entry.allowed_agents, assignment)
                || !granted(&connection.owner_id, &connection.allowed_agents, assignment) {
                bail!("Model candidate is not authorized");
            }
            candidates.push(Candidate {
                platform:None,
                entry_id: entry.id.clone(), entry_revision: entry.revision,
                connection_id: connection.id.clone(), connection_revision: connection.revision,
                credential_ref: connection.credential_ref.clone(), transport: connection.transport.clone(),
                upstream_model: entry.upstream_model.clone(),
            });
        }
        Ok(Self { version: 1, agent_id: assignment.agent_id.clone(), owner_id: assignment.owner_id.clone(),
            assignment_revision: assignment.revision, group_revision, candidates, role: ModelRole::Standard,
            custom_entry_id: None, roster_revision: 0, catalog_revision: 0 })
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
                credential_ref: format!("credential:{id}"), allowed_agents: BTreeSet::from(["agent-a".into()]), revision: 1, revoked: false });
            entries.insert(id.into(), ModelEntry { id: id.into(), owner_id: "owner".into(), connection_id: id.into(), upstream_model: "same-upstream".into(),
                display_name: id.into(), capabilities: BTreeSet::from(["text".into()]), allowed_agents: BTreeSet::from(["agent-a".into()]), revision: 1 });
        }
        let groups = BTreeMap::from([("group".into(), BackupGroup { id: "group".into(), owner_id: "owner".into(), entry_ids: vec!["c".into(), "a".into(), "b".into()], revision: 3 })]);
        (assignment, entries, connections, groups)
    }
    fn catalog_fixture() -> Catalog {
        let (assignment, entries, connections, mut groups) = fixture();
        for group in groups.values_mut() { group.revision = 1; }
        Catalog { revision: 0, platform_sources: BTreeMap::new(), entries, connections, groups,
            assignments: BTreeMap::from([(assignment.agent_id.clone(), assignment)]), agent_models: BTreeMap::new() }
    }
    #[test]
    fn legacy_migration_is_explicit_ordered_idempotent_and_does_not_resurrect_cleared_roles() {
        let mut catalog = catalog_fixture();
        for entry in catalog.entries.values_mut() { entry.allowed_agents.clear(); }
        for connection in catalog.connections.values_mut() { connection.allowed_agents.clear(); }
        catalog.migrate_legacy().unwrap();
        let before = catalog.clone();
        catalog.migrate_legacy().unwrap();
        assert_eq!(catalog, before);
        assert_eq!(catalog.agent_models["agent-a"].entry_ids, vec!["a", "c", "b"]);
        assert_eq!(catalog.resolve("agent-a").unwrap().candidates.iter().map(|c| c.entry_id.as_str()).collect::<Vec<_>>(), vec!["a", "c", "b"]);
        catalog.agent_models.get_mut("agent-a").unwrap().roles.clear();
        catalog.entries.get_mut("a").unwrap().allowed_agents.clear();
        catalog.migrate_legacy().unwrap();
        assert!(catalog.resolve("agent-a").unwrap_err().to_string().contains("Standard model required"));
        assert!(catalog.entries["a"].allowed_agents.is_empty());
    }

    #[test]
    fn roles_and_custom_selections_stay_explicit_and_pinned() {
        let mut catalog = catalog_fixture(); catalog.migrate_legacy().unwrap();
        let standard = catalog.resolve("agent-a").unwrap();
        assert!(catalog.resolve_role("agent-a", ModelRole::System).is_err());
        let roster = catalog.agent_models.get_mut("agent-a").unwrap();
        roster.roles.insert(ModelRole::System, RoleAssignment { entry_id: "b".into(), backup_entry_ids: vec!["a".into()] });
        roster.roles.insert(ModelRole::Premium, roster.roles[&ModelRole::Standard].clone());
        let system = catalog.resolve_role("agent-a", ModelRole::System).unwrap();
        assert_eq!(system.role, ModelRole::System);
        assert_eq!(system.candidates.iter().map(|c| c.entry_id.as_str()).collect::<Vec<_>>(), vec!["b", "a"]);
        let custom = catalog.resolve_custom("agent-a", "c").unwrap();
        assert_eq!(custom.custom_entry_id.as_deref(), Some("c")); assert_eq!(custom.candidates.len(), 1);
        assert_eq!(catalog.resolve("agent-a").unwrap(), standard);
        assert_eq!(catalog.resolve_role("agent-a", ModelRole::Premium).unwrap().candidates, standard.candidates);
        catalog.agent_models.get_mut("agent-a").unwrap().roles.get_mut(&ModelRole::System).unwrap().entry_id = "c".into();
        assert_eq!(system.candidates[0].entry_id, "b");
        assert_eq!(system.roster_revision, 1);
        let mut failed = FailoverState::default();
        assert_eq!(failed.failed(&system, FailureKind::ModelUnavailable), NextAction::Backup);
        assert_eq!(failed.history[0].to_entry_id, "a");
    }

    #[test]
    fn every_role_checks_capability_roster_and_both_explicit_grants() {
        let mut catalog = catalog_fixture(); catalog.migrate_legacy().unwrap();
        for role in [ModelRole::ImageIn, ModelRole::ImageOut, ModelRole::Voice] {
            catalog.agent_models.get_mut("agent-a").unwrap().roles.insert(role,
                RoleAssignment { entry_id: "a".into(), backup_entry_ids: vec![] });
            assert!(catalog.resolve_role("agent-a", role).is_err());
        }
        catalog.entries.get_mut("a").unwrap().capabilities.extend(["image_in".into(), "image_out".into(), "voice".into()]);
        for role in [ModelRole::ImageIn, ModelRole::ImageOut, ModelRole::Voice] { assert!(catalog.resolve_role("agent-a", role).is_ok()); }
        catalog.entries.get_mut("a").unwrap().allowed_agents.clear();
        assert!(catalog.resolve_role("agent-a", ModelRole::Voice).is_err());
        catalog.entries.get_mut("a").unwrap().allowed_agents.insert("agent-a".into());
        catalog.connections.get_mut("a").unwrap().allowed_agents.clear();
        assert!(catalog.resolve_role("agent-a", ModelRole::Voice).is_err());
        catalog.connections.get_mut("a").unwrap().allowed_agents.insert("agent-a".into());
        catalog.agent_models.get_mut("agent-a").unwrap().entry_ids.retain(|id| id != "a");
        assert!(catalog.resolve_custom("agent-a", "a").is_err());
        assert!(catalog.validate().is_err());
    }

    #[test]
    fn old_plan_defaults_to_standard_without_altering_candidates() {
        let (a,e,c,g) = fixture(); let original = SelectionPlan::resolve(&a,&e,&c,&g).unwrap();
        let mut value = serde_json::to_value(&original).unwrap();
        for key in ["role", "custom_entry_id", "roster_revision", "catalog_revision"] { value.as_object_mut().unwrap().remove(key); }
        let restored: SelectionPlan = serde_json::from_value(value).unwrap();
        assert_eq!(restored, original);
    }

    #[tokio::test]
    async fn role_edits_are_atomic_revisioned_and_revocation_does_not_block_clearing() {
        let root = tempfile::tempdir().unwrap(); let store = crate::store::Store::open(root.path()).await.unwrap();
        let mut catalog = store.save_model_catalog(&catalog_fixture(), 0).await.unwrap();
        let pinned = catalog.resolve("agent-a").unwrap();
        catalog.connections.get_mut("a").unwrap().allowed_agents.clear();
        catalog = store.save_model_catalog(&catalog, 1).await.unwrap();
        assert!(pinned.authorize_candidate(0, &catalog.entries, &catalog.connections).is_err());
        let mut invalid = catalog.clone();
        invalid.agent_models.get_mut("agent-a").unwrap().roles.insert(ModelRole::Premium,
            RoleAssignment { entry_id: "a".into(), backup_entry_ids: vec![] });
        assert!(store.save_model_catalog(&invalid, 2).await.is_err());
        assert_eq!(store.model_catalog().await.unwrap(), catalog);
        catalog.agent_models.get_mut("agent-a").unwrap().roles.clear();
        let saved = store.save_model_catalog(&catalog, 2).await.unwrap();
        assert_eq!(saved.agent_models["agent-a"].revision, 2);
        assert_eq!(saved.entries["a"].revision, 1);
        assert!(saved.resolve("agent-a").is_err());
        assert!(store.save_model_catalog(&catalog, 2).await.is_err());
        let mut forged = saved.clone(); forged.agent_models.get_mut("agent-a").unwrap().owner_id = "other".into();
        assert!(store.save_model_catalog(&forged, saved.revision).await.is_err());
    }

    // The old installer could configure a different owner's agent. Migration keeps
    // its grant authority but uses the canonical tenant owner for future role edits.
    #[tokio::test]
    async fn stored_legacy_roles_use_directory_owner_without_granting_its_private_connections() {
        let root = tempfile::tempdir().unwrap(); let store = crate::store::Store::open(root.path()).await.unwrap();
        sqlx::query("INSERT INTO agent_directory(id,owner_id) VALUES('agent-a','tenant-owner')")
            .execute(&store.app_pool).await.unwrap();
        let mut legacy = catalog_fixture();
        legacy.assignments.get_mut("agent-a").unwrap().owner_id = "admin".into();
        legacy.groups.get_mut("group").unwrap().owner_id = "admin".into();
        for entry in legacy.entries.values_mut() { entry.owner_id = "admin".into(); entry.allowed_agents.clear(); }
        for connection in legacy.connections.values_mut() { connection.owner_id = "admin".into(); connection.allowed_agents.clear(); }
        // An originally ungranted tenant-owned backup must not gain authorization
        // merely because the roster's owner is corrected to that tenant.
        legacy.connections.get_mut("b").unwrap().owner_id = "tenant-owner".into();
        sqlx::query("INSERT INTO docs(key,value) VALUES(?,?)")
            .bind(CATALOG_KEY).bind(store.seal(&serde_json::to_value(&legacy).unwrap()).unwrap())
            .execute(&store.app_pool).await.unwrap();
        let projected = store.model_catalog().await.unwrap();
        assert_eq!(projected.agent_models["agent-a"].owner_id, "tenant-owner");
        assert_eq!(projected.assignments["agent-a"].owner_id, "admin");
        assert!(projected.entries["a"].allowed_agents.contains("agent-a"));
        assert!(projected.connections["a"].allowed_agents.contains("agent-a"));
        assert!(projected.connections["b"].allowed_agents.is_empty());
        assert!(projected.resolve("agent-a").is_err());
        // An authorized administrator and the owner submit the same canonical owner;
        // storage advances revisions without turning the editor into the owner.
        let mut owner_edit = projected;
        owner_edit.agent_models.get_mut("agent-a").unwrap().roles.get_mut(&ModelRole::Standard).unwrap().backup_entry_ids = vec!["c".into()];
        let saved = store.save_model_catalog(&owner_edit,0).await.unwrap();
        assert_eq!(saved.agent_models["agent-a"].owner_id, "tenant-owner");
        assert_eq!(saved.agent_models["agent-a"].revision, 2);
        let mut oversight_edit = store.model_catalog().await.unwrap();
        oversight_edit.agent_models.get_mut("agent-a").unwrap().roles.insert(ModelRole::System,
            RoleAssignment { entry_id:"c".into(), backup_entry_ids:vec![] });
        let saved = store.save_model_catalog(&oversight_edit,1).await.unwrap();
        assert_eq!(saved.agent_models["agent-a"].owner_id, "tenant-owner");
        assert_eq!(saved.agent_models["agent-a"].revision, 3);
        assert!(saved.connections["b"].allowed_agents.is_empty());
        assert_eq!(store.model_catalog().await.unwrap(), saved);
        let mut transfer = saved.clone(); transfer.agent_models.get_mut("agent-a").unwrap().owner_id = "admin".into();
        assert!(store.save_model_catalog(&transfer,2).await.is_err());
    }

    // Newly accepted legacy-form writes use the same directory projection, while
    // an already explicit record is never rewritten as an implicit ownership transfer.
    #[tokio::test]
    async fn legacy_save_projects_owner_but_explicit_roles_keep_their_record_owner() {
        let root = tempfile::tempdir().unwrap(); let store = crate::store::Store::open(root.path()).await.unwrap();
        sqlx::query("INSERT INTO agent_directory(id,owner_id) VALUES('agent-a','tenant-owner')")
            .execute(&store.app_pool).await.unwrap();
        let saved = store.save_model_catalog(&catalog_fixture(),0).await.unwrap();
        assert_eq!(saved.agent_models["agent-a"].owner_id, "tenant-owner");
        assert_eq!(saved.resolve("agent-a").unwrap().owner_id, "tenant-owner");
        // Simulate an existing explicit snapshot from before directory reconciliation.
        let mut explicit = saved.clone(); explicit.agent_models.get_mut("agent-a").unwrap().owner_id = "previous-explicit-owner".into();
        sqlx::query("UPDATE docs SET value=? WHERE key=?")
            .bind(store.seal(&serde_json::to_value(&explicit).unwrap()).unwrap()).bind(CATALOG_KEY)
            .execute(&store.app_pool).await.unwrap();
        assert_eq!(store.model_catalog().await.unwrap().agent_models["agent-a"].owner_id,"previous-explicit-owner");
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
        let (a,mut e,mut c,g) = fixture(); e.get_mut("c").unwrap().owner_id = "private".into(); e.get_mut("c").unwrap().allowed_agents.clear();
        assert!(SelectionPlan::resolve(&a,&e,&c,&g).is_err());
        e.get_mut("c").unwrap().allowed_agents.insert(a.agent_id.clone()); c.get_mut("c").unwrap().owner_id = "private".into(); c.get_mut("c").unwrap().allowed_agents.clear();
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

#[cfg(test)]
mod upstream_id_tests {
    #[test]
    fn upstream_ids_are_not_record_ids() {
        assert!(crate::platform_catalog::valid_model("organization/model-name"));
        assert!(!super::valid_id("organization/model-name"));
        for invalid in ["", "model\n", "model name", "model?token=x"] {
            assert!(!crate::platform_catalog::valid_model(invalid));
        }
    }
    #[test]
    fn record_ids_obey_ascii_and_length_limits() {
        let max = "a".repeat(128);
        assert!(super::valid_id(&max));
        assert!(!super::valid_id(&format!("{max}a")));
        assert!(!super::valid_id("model/unsafe"));
        assert!(!super::valid_id("modél"));
    }
}
