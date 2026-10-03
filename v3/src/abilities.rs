use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashSet},
    fs,
    path::{Path, PathBuf},
};

const MAX_MANIFEST_BYTES: u64 = 64 * 1024;
const MAX_SKILL_BYTES: u64 = 32 * 1024;
const MAX_SELECTABLE_SKILL_BYTES: u64 = 64 * 1024;
const MAX_ABILITIES: usize = 128;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum EntryKind { Ability, VirtualCore, Oauth, Channel, Service, Placeholder }

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Readiness { Ready, Partial, Pending, Placeholder }

#[derive(Clone, Debug, Serialize)]
struct Ability {
    id: String,
    #[serde(skip_serializing)]
    path: PathBuf,
    manifest_digest: String,
    group: String,
    group_name: String,
    group_order: i32,
    name: String,
    description: String,
    skill_summary: Option<String>,
    migration_note: Option<String>,
    icon: Option<String>,
    color: Option<String>,
    order: i32,
    kind: EntryKind,
    readiness: Readiness,
    config: Value,
    tools: Vec<String>,
    skill: Option<String>,
    tool_skills: BTreeMap<String, String>,
    selectable_skills: BTreeMap<String, String>,
}

#[derive(Deserialize)]
struct Manifest {
    display_name: String,
    kind: EntryKind,
    description: String,
    #[serde(default)]
    tools: Vec<String>,
    #[serde(default)]
    readiness: Option<Readiness>,
    #[serde(default)]
    config: Value,
    #[serde(default)]
    skill_summary: Option<String>,
    #[serde(default)]
    migration_note: Option<String>,
    #[serde(default)]
    icon: Option<String>,
    #[serde(default)]
    color: Option<String>,
    #[serde(default)]
    order: i32,
    #[serde(default)]
    placeholder: bool,
}

#[derive(Default, Deserialize)]
struct GroupManifest {
    name: Option<String>,
    #[serde(default)]
    order: i32,
}

#[derive(Clone)]
pub struct Catalog {
    abilities: Vec<Ability>,
    version: String,
}

fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && id
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
}

fn read_bytes(path: &Path, limit: u64) -> Result<Vec<u8>> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.file_type().is_file() || meta.len() > limit {
        bail!("Invalid ability file: {}", path.display());
    }
    // A concurrent growth after metadata inspection cannot create an unbounded read.
    use std::io::Read;
    let mut bytes = Vec::new();
    fs::File::open(path)?.take(limit + 1).read_to_end(&mut bytes)
        .with_context(|| format!("Cannot read ability file: {}", path.display()))?;
    if bytes.len() as u64 > limit { bail!("Ability file grew beyond limit"); }
    Ok(bytes)
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    let data = read_bytes(path, MAX_MANIFEST_BYTES)?;
    serde_json::from_slice(&data)
        .with_context(|| format!("Invalid ability manifest: {}", path.display()))
}

fn optional_skill(path: &Path, limit: u64) -> Result<Option<String>> {
    match fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
        Ok(_) => {}
    }
    let bytes = read_bytes(path, limit)?;
    let text = String::from_utf8(bytes)
        .with_context(|| format!("Invalid ability skill text: {}", path.display()))?;
    if text.trim().is_empty() {
        bail!("Empty ability skill: {}", path.display());
    }
    Ok(Some(text))
}

impl Catalog {
    pub fn load(root: &Path) -> Result<Self> {
        let mut abilities = Vec::new();
        let mut ids = HashSet::new();
        let mut owners = BTreeMap::new();
        if root.exists() && fs::symlink_metadata(root)?.file_type().is_symlink() { bail!("Symlink catalog root"); }
        let groups = match fs::read_dir(root) {
            Ok(groups) => groups,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let version = hex::encode(Sha256::digest(b"[]"));
                return Ok(Self { abilities, version });
            }
            Err(error) => return Err(error).with_context(|| format!("Cannot read ability catalog: {}", root.display())),
        };
        for group in groups {
            let group = group?;
            if group.file_type()?.is_symlink() { bail!("Symlink ability group"); }
            if !group.file_type()?.is_dir() {
                continue;
            }
            let group_id = group.file_name().to_string_lossy().to_string();
            if group_id.is_empty()
                || group_id.len() > 64
                || !group_id
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
            {
                bail!("Invalid ability group identifier");
            }
            let group_path = group.path();
            let group_meta = group_path.join("_group.json");
            let group_meta: GroupManifest = if group_meta.exists() {
                read_json(&group_meta)?
            } else {
                GroupManifest::default()
            };
            let group_name = group_meta.name.unwrap_or_else(|| group_id.clone());
            if group_name.is_empty() || group_name.len() > 100 {
                bail!("Invalid ability group name");
            }
            for folder in fs::read_dir(&group_path)? {
                let folder = folder?;
                if folder.file_type()?.is_symlink() { bail!("Symlink ability folder"); }
                if !folder.file_type()?.is_dir() {
                    continue;
                }
                let id = folder.file_name().to_string_lossy().to_string();
                if !valid_id(&id) || !ids.insert(id.clone()) {
                    bail!("Invalid or duplicate ability identifier: {id}");
                }
                let ability_path = folder.path();
                // Pin exactly the bytes parsed here; live edits withdraw cached authority
                // until a freshly validated catalog reload adopts the new declaration.
                let manifest_bytes = read_bytes(&ability_path.join(format!("{id}.json")), MAX_MANIFEST_BYTES)?;
                let manifest_digest = hex::encode(Sha256::digest(&manifest_bytes));
                let manifest: Manifest = serde_json::from_slice(&manifest_bytes)
                    .with_context(|| format!("Invalid ability manifest: {id}"))?;
                if manifest.display_name.is_empty()
                    || manifest.display_name.len() > 100
                    || manifest.description.is_empty()
                    || manifest.description.len() > 1000
                    || manifest.tools.len() > 64
                    || manifest
                        .skill_summary
                        .as_ref()
                        .is_some_and(|s| s.len() > 2000)
                    || manifest
                        .migration_note
                        .as_ref()
                        .is_some_and(|s| s.len() > 2000)
                {
                    bail!("Invalid active ability: {id}");
                }
                let readiness = manifest.readiness.clone().unwrap_or_else(|| {
                    if manifest.placeholder || manifest.kind == EntryKind::Placeholder { Readiness::Placeholder }
                    else if manifest.tools.iter().any(|tool| Self::supported(tool)) { Readiness::Ready }
                    else { Readiness::Pending }
                });
                if (manifest.placeholder || manifest.kind == EntryKind::Placeholder)
                    && (readiness != Readiness::Placeholder || !manifest.tools.is_empty()) {
                    bail!("Invalid placeholder: {id}");
                }
                if matches!(readiness, Readiness::Ready | Readiness::Partial) &&
                    (!matches!(manifest.kind, EntryKind::Ability | EntryKind::VirtualCore) ||
                     !manifest.tools.iter().any(|tool| Self::supported(tool))) {
                    bail!("Ready entry has no installed adapter: {id}");
                }
                for directory in ["tools", "skills"] {
                    let path = ability_path.join(directory);
                    if fs::symlink_metadata(&path).is_ok_and(|m| !m.file_type().is_dir()) {
                        bail!("Invalid ability asset directory: {id}");
                    }
                }
                let mut local = HashSet::new();
                let mut tool_skills = BTreeMap::new();
                for tool in &manifest.tools {
                    if !valid_id(tool) { bail!("Invalid tool identifier: {tool}"); }
                    // Installed schemas have exactly one authority-owning entry.
                    if Self::supported(tool) && owners.insert(tool.clone(), id.clone()).is_some() {
                        bail!("Duplicate installed tool owner: {tool}");
                    }
                    if !local.insert(tool) {
                        bail!("Ability {id} declares a duplicate tool: {tool}");
                    }
                    if let Some(skill) = optional_skill(
                        &ability_path.join("tools").join(format!("{tool}.skill.md")),
                        MAX_SKILL_BYTES,
                    )? {
                        tool_skills.insert(tool.clone(), skill);
                    }
                }
                let mut selectable_skills = BTreeMap::new();
                let skills_path = ability_path.join("skills");
                if skills_path.is_dir() {
                    for asset in fs::read_dir(&skills_path)? {
                        let asset = asset?;
                        let filename = asset.file_name().to_string_lossy().to_string();
                        let Some(key) = filename.strip_suffix(".skill.md") else {
                            bail!("Invalid selectable skill asset: {id}");
                        };
                        if !valid_id(key) || selectable_skills.len() >= 32 {
                            bail!("Invalid selectable skill identifier: {id}");
                        }
                        let text = optional_skill(&asset.path(), MAX_SELECTABLE_SKILL_BYTES)?
                            .context("Missing selectable skill")?;
                        selectable_skills.insert(key.to_owned(), text);
                    }
                }
                if !manifest.config.is_null() && !manifest.config.is_object() {
                    bail!("Invalid configuration metadata: {id}");
                }
                let skill = optional_skill(
                    &ability_path.join(format!("{id}.skill.md")),
                    MAX_SKILL_BYTES,
                )?;
                abilities.push(Ability {
                    id,
                    path: ability_path,
                    manifest_digest,
                    group: group_id.clone(),
                    group_name: group_name.clone(),
                    group_order: group_meta.order,
                    name: manifest.display_name,
                    description: manifest.description,
                    skill_summary: manifest.skill_summary,
                    migration_note: manifest.migration_note,
                    icon: manifest.icon,
                    color: manifest.color,
                    order: manifest.order,
                    kind: manifest.kind,
                    readiness,
                    config: manifest.config,
                    tools: manifest.tools,
                    skill,
                    tool_skills,
                    selectable_skills,
                });
                if abilities.len() > MAX_ABILITIES {
                    bail!("Too many abilities");
                }
            }
        }
        abilities.sort_by(|a, b| {
            (a.group_order, &a.group_name, a.order, &a.name, &a.id).cmp(&(
                b.group_order,
                &b.group_name,
                b.order,
                &b.name,
                &b.id,
            ))
        });
        let version = hex::encode(Sha256::digest(serde_json::to_vec(&abilities)?));
        Ok(Self { abilities, version })
    }

    pub fn version(&self) -> &str {
        &self.version
    }

    fn present(ability: &Ability) -> bool {
        // Presence alone cannot retain an old executable declaration after an
        // operator disables or edits its manifest. Unreadable/changed bytes fail closed.
        [ability.path.as_path(), ability.path.parent().unwrap_or(ability.path.as_path()),
            ability.path.parent().and_then(Path::parent).unwrap_or(ability.path.as_path())]
            .into_iter().all(|path|fs::symlink_metadata(path).is_ok_and(|m|m.file_type().is_dir()))
            && read_bytes(&ability.path.join(format!("{}.json", ability.id)), MAX_MANIFEST_BYTES)
                .is_ok_and(|bytes| hex::encode(Sha256::digest(&bytes)) == ability.manifest_digest)
    }

    // Only cached, validated bundle IDs can select an asset. Live edits withdraw
    // old guidance instead of importing instructions without a catalog reload.
    fn skill_live(ability: &Ability, kind: &str, name: &str, text: &str) -> bool {
        let (path, limit) = match kind {
            "ability" if name == ability.id => (ability.path.join(format!("{name}.skill.md")), MAX_SKILL_BYTES),
            "tool" if valid_id(name) => (ability.path.join("tools").join(format!("{name}.skill.md")), MAX_SKILL_BYTES),
            "selectable" if valid_id(name) => (ability.path.join("skills").join(format!("{name}.skill.md")), MAX_SELECTABLE_SKILL_BYTES),
            _ => return false,
        };
        path.parent().is_some_and(|parent|
            fs::symlink_metadata(parent).is_ok_and(|m| m.file_type().is_dir()))
            && read_bytes(&path, limit).is_ok_and(|bytes| bytes == text.as_bytes())
    }

    /// Bounded validity fingerprint of cached manifests and skill assets only.
    /// A changed file cannot become trusted guidance; it merely invalidates the
    /// run-local schema/guidance cache until a validated reload adopts its bytes.
    pub fn live_version(&self) -> String {
        let validity = self.abilities.iter().map(|ability| {
            let present = Self::present(ability);
            let mut assets = Vec::new();
            if let Some(text) = &ability.skill {
                assets.push(json!(["ability", ability.id,
                    present && Self::skill_live(ability, "ability", &ability.id, text)]));
            }
            for (name, text) in &ability.tool_skills {
                assets.push(json!(["tool", name, present && Self::skill_live(ability, "tool", name, text)]));
            }
            for (name, text) in &ability.selectable_skills {
                assets.push(json!(["selectable", name, present && Self::skill_live(ability, "selectable", name, text)]));
            }
            json!([ability.id, present, assets])
        }).collect::<Vec<_>>();
        hex::encode(Sha256::digest(serde_json::to_vec(&json!([self.version, validity]))
            .expect("Cached catalog validity is serializable")))
    }

    fn bundled_candidates<'a>(
        &'a self, ability_id: &str, visibility: &Value, state: &Value,
        allow_ability: &impl Fn(&str) -> bool, allow_tool: &impl Fn(&str) -> bool,
    ) -> Result<(&'a Ability, Vec<(&'static str, &'a str, &'a String)>)> {
        if !valid_id(ability_id) { bail!("Invalid bundled ability ID"); }
        let ability = self.abilities.iter().find(|a| a.id == ability_id)
            .filter(|a| Self::available(a, visibility, state)
                && Self::skill_mode(visibility, ability_id) != "off"
                && allow_ability(ability_id)
                && a.tools.iter().any(|tool| Self::supported(tool)
                    && Self::tool_mode(visibility, tool) != "off" && allow_tool(tool)))
            .context("Bundled skills unavailable under current access")?;
        let mut assets = Vec::new();
        if let Some(text) = &ability.skill { assets.push(("ability", ability.id.as_str(), text)); }
        for (name, text) in &ability.tool_skills {
            if Self::supported(name) && Self::tool_mode(visibility, name) != "off" && allow_tool(name) {
                assets.push(("tool", name.as_str(), text));
            }
        }
        for (name, text) in &ability.selectable_skills { assets.push(("selectable", name.as_str(), text)); }
        assets.retain(|(kind, name, text)| Self::skill_live(ability, kind, name, text));
        Ok((ability, assets))
    }

    pub(crate) fn bundled_skill_list(
        &self, ability_id: &str, visibility: &Value, state: &Value,
        allow_ability: impl Fn(&str) -> bool, allow_tool: impl Fn(&str) -> bool,
    ) -> Result<Vec<Value>> {
        let (ability, assets) = self.bundled_candidates(ability_id, visibility, state, &allow_ability, &allow_tool)?;
        Ok(assets.into_iter().map(|(kind, name, text)| json!({
            "ability_id":ability.id,"kind":kind,"skill_id":name,
            "bytes":text.len(),"sha256":hex::encode(Sha256::digest(text.as_bytes()))
        })).collect())
    }

    pub(crate) fn bundled_skill_load(
        &self, ability_id: &str, kind: &str, skill_id: &str, visibility: &Value, state: &Value,
        allow_ability: impl Fn(&str) -> bool, allow_tool: impl Fn(&str) -> bool,
    ) -> Result<Value> {
        if !valid_id(skill_id) || !matches!(kind, "ability" | "tool" | "selectable") {
            bail!("Invalid bundled skill ID or kind");
        }
        let (ability, assets) = self.bundled_candidates(ability_id, visibility, state, &allow_ability, &allow_tool)?;
        let (_, _, text) = assets.into_iter().find(|(k, name, _)| *k == kind && *name == skill_id)
            .context("Bundled skill unavailable under current access")?;
        Ok(json!({"ability_id":ability.id,"kind":kind,"skill_id":skill_id,
            "bytes":text.len(),"sha256":hex::encode(Sha256::digest(text.as_bytes())),
            "text":text,"source_kind":"untrusted_bundled_guidance","activates":false}))
    }

    fn executable(ability: &Ability) -> bool {
        Self::present(ability) && matches!(ability.readiness, Readiness::Ready | Readiness::Partial)
            && matches!(ability.kind, EntryKind::Ability | EntryKind::VirtualCore)
    }

    pub fn ids(&self) -> Vec<String> {
        self.abilities.iter().filter(|ability| Self::present(ability))
            .map(|ability| ability.id.clone()).collect()
    }

    pub fn known(&self, id: &str) -> bool {
        self.abilities.iter().any(|ability| ability.id == id && Self::present(ability))
    }

    fn supported(name: &str) -> bool {
        crate::tools::schemas().iter().any(|schema| schema["name"] == name)
    }

    fn allowed(policy: &Value, name: &str) -> bool {
        matches!(
            policy["rules"][name].as_str(),
            Some("allow" | "require_approval")
        )
    }

    fn contains_id(list: &Value, id: &str) -> bool {
        list.as_array()
            .is_some_and(|items| items.iter().any(|item| item.as_str() == Some(id)))
    }

    fn suppressed(state: &Value, id: &str) -> bool {
        Self::contains_id(&state["suppressed"], id)
    }

    fn active(state: &Value, id: &str) -> bool {
        Self::contains_id(&state["active"], id)
    }

    fn is_off(visibility: &Value, id: &str) -> bool {
        visibility["visibility"][id] == "off"
    }

    fn skill_mode<'a>(visibility: &'a Value, id: &str) -> &'a str {
        visibility["skill_visibility"][id].as_str().unwrap_or("visible")
    }

    fn tool_mode<'a>(visibility: &'a Value, name: &str) -> &'a str {
        visibility["tool_modes"][name].as_str().unwrap_or("visible")
    }

    pub fn has_tool(&self, ability_id: &str, tool_name: &str) -> bool {
        self.abilities.iter().any(|ability|
            ability.id == ability_id && Self::present(ability) && ability.tools.iter().any(|tool| tool == tool_name))
    }

    fn is_visible(visibility: &Value, id: &str) -> bool {
        visibility["visibility"][id] == "visible"
    }

    fn available(ability: &Ability, visibility: &Value, state: &Value) -> bool {
        Self::executable(ability)
            && !Self::is_off(visibility, &ability.id)
            && !Self::suppressed(state, &ability.id)
            && (Self::is_visible(visibility, &ability.id) || Self::active(state, &ability.id))
    }

    fn describe(ability: &Ability, policy: &Value, visibility: &Value, state: &Value) -> Value {
        let tools = ability
            .tools
            .iter()
            .filter(|name| Self::executable(ability) && Self::supported(name) && Self::allowed(policy, name) && Self::tool_mode(visibility, name) != "off")
            .map(|name| json!({"name":name,"policy":policy["rules"][name],
                        "mode":Self::tool_mode(visibility,name),"supported":Self::supported(name)}))
            .collect::<Vec<_>>();
        let visible = Self::is_visible(visibility, &ability.id);
        let off = Self::is_off(visibility, &ability.id);
        json!({"id":ability.id,"group":ability.group,"group_name":ability.group_name,
            "name":ability.name,"description":ability.description,
            "kind":ability.kind,"readiness":ability.readiness,"config":ability.config,
            "skill_summary":ability.skill_summary,"migration_note":ability.migration_note,
            "icon":ability.icon,"color":ability.color,"tools":tools,
            "has_skill":ability.skill.as_ref().is_some_and(|text| Self::skill_live(ability, "ability", &ability.id, text)),
            "selectable_skills":ability.selectable_skills.iter().filter(|(name, text)| Self::skill_live(ability, "selectable", name, text)).map(|(name, _)| name).collect::<Vec<_>>(),
            "skill_visibility":Self::skill_mode(visibility, &ability.id),
            "visibility":if off {"off"} else if visible {"visible"} else {"discoverable"},
            "active":Self::active(state, &ability.id)})
    }

    pub fn catalog_with(&self, policy: &Value, visibility: &Value, state: &Value) -> Vec<Value> {
        self.abilities
            .iter()
            .filter(|ability| {
                Self::executable(ability)
                    && !Self::is_off(visibility, &ability.id)
                    && !Self::suppressed(state, &ability.id)
                    && ability.tools.iter().any(|name| Self::supported(name) && Self::allowed(policy, name) && Self::tool_mode(visibility,name) != "off")
            })
            .map(|ability| Self::describe(ability, policy, visibility, state))
            .collect()
    }

    pub fn configuration_catalog(&self, policy: &Value, visibility: &Value) -> Vec<Value> {
        self.abilities
            .iter()
            .filter(|ability| Self::present(ability))
            .map(|ability| {
                let mut item = Self::describe(ability, policy, visibility, &Value::Null);
                item["available"] = json!(Self::executable(ability) && ability.tools.iter().any(|name| Self::supported(name)));
                item["tools"] = json!(ability.tools.iter().map(|name| {
                    json!({"name":name,"policy":policy["rules"][name],
                        "mode":Self::tool_mode(visibility,name),"supported":Self::supported(name)})
                }).collect::<Vec<_>>());
                item
            })
            .collect()
    }

    /// Installed owners of a tool, independent of visibility or activation.
    /// A shared tool name must meet every owning ability permission.
    pub fn tool_abilities(&self,name:&str,_visibility:&Value,_state:&Value,
        _loaded:&Value) -> Vec<String> {
        if !Self::supported(name) {return Vec::new();}
        self.abilities.iter().filter(|ability| {
            Self::executable(ability) && ability.tools.iter().any(|tool|tool==name)
        }).map(|ability|ability.id.clone()).collect()
    }

    /// Separate installed support from the effective agent/session visibility.
    /// The two gates do not include operator, tier, or caller permissions.
    pub fn tool_gate(
        &self,
        name: &str,
        visibility: &Value,
        state: &Value,
        loaded: &Value,
    ) -> (bool, bool) {
        let supported = Self::supported(name);
        let mut available = false;
        let mut visible = false;
        for ability in &self.abilities {
            if !Self::executable(ability) || !ability.tools.iter().any(|tool| tool == name) {
                continue;
            }
            available |= supported;
            visible |= supported
                && match Self::tool_mode(visibility, name) {
                    "off" => false,
                    "discoverable" => Self::contains_id(loaded, &ability.id),
                    _ => true,
                }
                && Self::available(ability, visibility, state);
        }
        (available, visible)
    }

    pub fn tool_available(
        &self,
        name: &str,
        visibility: &Value,
        state: &Value,
        loaded: &Value,
    ) -> bool {
        let (available, visible) = self.tool_gate(name, visibility, state, loaded);
        available && visible
    }

    #[cfg(test)]
    pub fn tool_loaded(&self, name: &str, loaded: &Value) -> bool {
        self.abilities.iter().any(|ability| {
            Self::present(ability)
                && ability.tools.iter().any(|tool| tool == name)
                && Self::contains_id(loaded, &ability.id)
        })
    }

    pub fn schemas_with(
        &self,
        policy: &Value,
        loaded: &Value,
        visibility: &Value,
        state: &Value,
    ) -> Vec<Value> {
        let mut schemas = vec![
            json!({"type":"function","name":"list_abilities",
                "description":"Find available abilities and their tool policies before using workspace tools.",
                "parameters":{"type":"object","properties":{"query":{"type":"string"}},
                    "required":[],"additionalProperties":false}}),
            json!({"type":"function","name":"load_ability",
                "description":"Load an ability by ID and read its skill and tool guidance.",
                "parameters":{"type":"object","properties":{"ability_id":{"type":"string"}},
                    "required":["ability_id"],"additionalProperties":false}}),
        ];
        schemas.extend(crate::tools::schemas().into_iter().filter(|tool| {
            let name = tool["name"].as_str().unwrap_or("");
            Self::allowed(policy, name) && self.tool_available(name, visibility, state, loaded)
        }));
        schemas
    }

    #[cfg(test)]
    pub fn schemas(&self, policy: &Value, loaded: &Value) -> Vec<Value> {
        let state = json!({"active":loaded,"suppressed":[]});
        self.schemas_with(policy, loaded, &Value::Null, &state)
    }

    pub fn visible_guidance(&self, policy: &Value, visibility: &Value, state: &Value) -> String {
        self.visible_guidance_filtered(policy, visibility, state, |_| true, |_| true)
    }

    /// Assemble model-facing guidance using live ability and tool permissions.
    /// Denied abilities are excluded before their skill or tool text is read.
    pub fn visible_guidance_filtered(
        &self,
        policy: &Value,
        visibility: &Value,
        state: &Value,
        allow_ability: impl Fn(&str) -> bool,
        allow_tool: impl Fn(&str) -> bool,
    ) -> String {
        const LIMIT: usize = 24 * 1024;
        const MARKER_RESERVE: usize = 256;
        let mut text = String::new();
        for ability in &self.abilities {
            if !Self::executable(ability)
                || !allow_ability(&ability.id)
                || Self::is_off(visibility, &ability.id)
                || !ability.tools.iter().any(|tool| {
                    Self::supported(tool) && Self::allowed(policy, tool) && allow_tool(tool)
                })
                || Self::suppressed(state, &ability.id)
                || !(Self::is_visible(visibility, &ability.id) || Self::active(state, &ability.id))
            {
                continue;
            }
            let mut part = format!("\n\nVisible ability {} ({}):", ability.id, ability.name);
            // Only typed saved settings enter guidance; they never grant use.
            for key in ["terminal_caution","destructive_caution"] {
                if let Some(value)=visibility["settings"][&ability.id][key].as_str()
                    .filter(|v|matches!(*v,"cautious"|"balanced"|"trusted")) {
                    part.push_str(&format!("\nConfigured {key}: {value}. Live Rust policy and approval remain authoritative."));
                }
            }

            // SKILL-LIFETIME: activated discoverable guidance is rebuilt from the
            // catalog, independent of transcript compaction. Off and suppressed
            // remain authoritative; authorization was checked above.
            if Self::skill_mode(visibility, &ability.id) == "visible"
                || (Self::skill_mode(visibility, &ability.id) == "discoverable"
                    && Self::active(state, &ability.id)) {
                if let Some(skill) = ability.skill.as_ref().filter(|text| Self::skill_live(ability, "ability", &ability.id, text)) {
                    part.push_str("\n");
                    part.push_str(skill);
                }
            }
            for (tool, skill) in &ability.tool_skills {
                if Self::supported(tool) && Self::allowed(policy, tool)
                    && allow_tool(tool)
                    && Self::skill_live(ability, "tool", tool, skill)
                    && Self::tool_mode(visibility, tool) == "visible" {
                    part.push_str("\n\nTool ");
                    part.push_str(tool);
                    part.push_str(":\n");
                    part.push_str(skill);
                }
            }
            if part.len() <= (LIMIT - MARKER_RESERVE).saturating_sub(text.len()) {
                text.push_str(&part);
            } else {
                let marker = format!("\n\nAbility {} guidance omitted due to size. Use load_ability for its full skill.", ability.id);
                if marker.len() <= LIMIT.saturating_sub(text.len()) {
                    text.push_str(&marker);
                }
            }
        }
        text
    }

    /// Render a bounded inventory from the exact entries and schemas authorized
    /// for this model call. Discoverable abilities keep their tool schemas hidden.
    pub fn model_index(entries: &[Value], schemas: &[Value]) -> String {
        const LIMIT: usize = 8 * 1024;
        fn add(out: &mut String, line: &str) -> bool {
            // Reserve room for the truncation notice when a large catalog fills the menu.
            if out.len() + line.len() + 1 > LIMIT - 64 { return false; }
            out.push_str(line);
            out.push('\n');
            true
        }
        fn short(value: &str) -> String {
            value.split_whitespace().collect::<Vec<_>>().join(" ")
                .chars().take(160).collect()
        }
        let callable = schemas.iter().filter_map(|s| s["name"].as_str()).collect::<HashSet<_>>();
        let can_list = callable.contains("list_abilities");
        let can_load = callable.contains("load_ability");
        let mut discoverable = Vec::new();
        let mut ready_tools = BTreeMap::new();
        let mut loadable_tools = BTreeMap::new();
        for entry in entries {
            let Some(id) = entry["id"].as_str() else { continue };
            if entry["visibility"] == "discoverable" && entry["active"] != true {
                if can_load {
                    let name = short(entry["name"].as_str().unwrap_or(id));
                    let summary = short(entry["skill_summary"].as_str()
                        .or_else(|| entry["description"].as_str()).unwrap_or(""));
                    discoverable.push((id, name, summary));
                }
                continue;
            }
            if let Some(tools) = entry["tools"].as_array() {
                for tool in tools {
                    if let Some(name) = tool["name"].as_str() {
                        if is_discovery(name) { continue; }
                        if callable.contains(name) {
                            let description = schemas.iter().find(|s| s["name"] == name)
                                .and_then(|s| s["description"].as_str()).unwrap_or("");
                            ready_tools.insert(name.to_owned(), short(description));
                        } else if can_load && tool["mode"] == "discoverable" {
                            loadable_tools.insert(name.to_owned(), id.to_owned());
                        }
                    }
                }
            }
        }
        if !can_list && !can_load && discoverable.is_empty() && ready_tools.is_empty() {
            return String::new();
        }
        let mut out = String::new();
        add(&mut out, "# [TOOLS]");
        add(&mut out, "These tools can be called now. A call may still require approval.");
        if can_list { add(&mut out, "- list_abilities — check the live ability catalog"); }
        if can_load { add(&mut out, "- load_ability — load an ability's tools and how-to guidance"); }
        let mut omitted = false;
        for (name, description) in ready_tools {
            let row = if description.is_empty() { format!("- {name}") }
                else { format!("- {name} — {description}") };
            if !add(&mut out, &row) { omitted = true; break; }
        }
        if !loadable_tools.is_empty() {
            add(&mut out, "");
            add(&mut out, "## Load on demand");
            add(&mut out, "These tools are permitted but their schemas are withheld. Call load_ability with the owning ability ID before using one.");
            for (name, id) in loadable_tools {
                if !add(&mut out, &format!("- {name} — load ability {id}")) { omitted = true; break; }
            }
        }
        if !discoverable.is_empty() {
            add(&mut out, "");
            add(&mut out, "# [ABILITIES]");
            add(&mut out, "These abilities are available but not loaded. Call load_ability with an ID to reveal its tools and how-to guidance. An empty loaded list does not mean no abilities are available. Use list_abilities for the full current catalog.");
            for (id, name, summary) in discoverable {
                let row = if summary.is_empty() { format!("- {id} — {name}") }
                    else { format!("- {id} — {name}: {summary}") };
                if !add(&mut out, &row) { omitted = true; break; }
            }
        }
        if omitted {
            let notice = "More entries are available through list_abilities.\n";
            if out.len() + notice.len() <= LIMIT {
                out.push_str(notice);
            }
        }
        out
    }

    pub fn discover_with(
        &self,
        name: &str,
        args: &Value,
        run: &mut Value,
        policy: &Value,
        visibility: &Value,
        state: &Value,
    ) -> Value {
        match name {
            "list_abilities" => {
                let query = args["query"].as_str().unwrap_or("").trim().to_lowercase();
                if query.len() > 200 {
                    return json!({"status":"error","output":"Query is too long"});
                }
                let found = self
                    .catalog_with(policy, visibility, state)
                    .into_iter()
                    .filter(|ability| {
                        query.is_empty()
                            || ability["id"].as_str().unwrap_or("").contains(&query)
                            || ability["group_name"]
                                .as_str()
                                .unwrap_or("")
                                .to_lowercase()
                                .contains(&query)
                            || ability["name"]
                                .as_str()
                                .unwrap_or("")
                                .to_lowercase()
                                .contains(&query)
                            || ability["description"]
                                .as_str()
                                .unwrap_or("")
                                .to_lowercase()
                                .contains(&query)
                            || ability["tools"].as_array().is_some_and(|tools| {
                                tools.iter().any(|tool| {
                                    tool["name"].as_str().unwrap_or("").contains(&query)
                                })
                            })
                    })
                    .collect::<Vec<_>>();
                json!({"status":"ok","catalog_version":self.version,"abilities":found})
            }
            "load_ability" => {
                let Some(id) = args["ability_id"].as_str() else {
                    return json!({"status":"error","output":"ability_id is required"});
                };
                let Some(entry) = self.abilities.iter().find(|ability| ability.id == id && Self::present(ability)) else {
                    return json!({"status":"denied","output":"Ability unavailable"});
                };
                if !Self::executable(entry) {
                    return json!({"status":"denied","output":"Ability pending or non-executable"});
                }
                if Self::is_off(visibility, id) {
                    return json!({"status":"denied","output":"Ability disabled for this agent"});
                }
                if Self::suppressed(state, id) {
                    return json!({"status":"denied","output":"Ability suppressed in this session"});
                }
                let ability = Self::describe(entry, policy, visibility, state);
                if ability["tools"].as_array().is_none_or(Vec::is_empty) {
                    return json!({"status":"denied","output":"Ability unavailable under current policy"});
                }
                if !run["loaded_abilities"].is_array() {
                    run["loaded_abilities"] = json!([]);
                }
                let loaded = run["loaded_abilities"].as_array_mut().unwrap();
                if !loaded.iter().any(|item| item.as_str() == Some(id)) {
                    loaded.push(json!(id));
                }
                let permitted_skills = entry
                    .tool_skills
                    .iter()
                    .filter(|(tool, text)| Self::supported(tool) && Self::allowed(policy, tool) && Self::tool_mode(visibility, tool) != "off"
                        && Self::skill_live(entry, "tool", tool, text))
                    .collect::<BTreeMap<_, _>>();
                json!({"status":"ok","ability":ability,
                    "skill":if Self::skill_mode(visibility, id) == "off" {None} else {entry.skill.as_ref().filter(|text| Self::skill_live(entry, "ability", &entry.id, text))},
                    "tool_skills":permitted_skills,
                    "selectable_skills":entry.selectable_skills.iter().filter(|(name, text)| Self::skill_live(entry, "selectable", name, text)).map(|(name, _)| name).collect::<Vec<_>>(),
                    "catalog_version":self.version})
            }
            _ => json!({"status":"denied","output":"Unknown discovery operation"}),
        }
    }

    pub fn discover(&self, name: &str, args: &Value, run: &mut Value, policy: &Value) -> Value {
        self.discover_with(name, args, run, policy, &Value::Null, &Value::Null)
    }
}

pub fn is_discovery(name: &str) -> bool {
    matches!(name, "list_abilities" | "load_ability")
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn fixture() -> (TempDir, Catalog) {
        let root = tempfile::tempdir().unwrap();
        let group = root.path().join("Administrator");
        let ability = group.join("codebase_admin");
        fs::create_dir_all(ability.join("tools")).unwrap();
        fs::write(
            group.join("_group.json"),
            r#"{"name":"Administrator","order":1}"#,
        )
        .unwrap();
        fs::write(ability.join("codebase_admin.json"), r#"{
            "display_name":"Codebase Admin","kind":"ability",
            "description":"Inspect and edit an isolated workspace",
            "tools":["file_list","file_read","file_search","state_inspect","prompt_slot_read","prompt_slot_write","file_write","file_edit","workspace_seed","source_review","source_patch","promotion_propose","shell","python"]
        }"#).unwrap();
        fs::write(
            ability.join("codebase_admin.skill.md"),
            "Use scoped workspace tools.",
        )
        .unwrap();
        fs::write(
            ability.join("tools/file_edit.skill.md"),
            "Match one exact span.",
        )
        .unwrap();
        let catalog = Catalog::load(root.path()).unwrap();
        (root, catalog)
    }

    #[test]
    fn changed_manifest_withdraws_cached_loaded_tools_until_reload() {
        for change in ["pending", "remove_tool", "malformed"] {
            let (root, catalog) = fixture();
            let path = root.path().join("Administrator/codebase_admin/codebase_admin.json");
            let original = fs::read(&path).unwrap();
            let mut manifest: Value = serde_json::from_slice(&original).unwrap();
            let p = policy();
            let visibility = json!({"visibility":{"codebase_admin":"visible"}});
            let active = json!({"active":["codebase_admin"],"suppressed":[]});
            let loaded = json!(["codebase_admin"]);
            assert_eq!(catalog.tool_gate("file_read", &visibility, &active, &loaded), (true, true));
            match change {
                "pending" => manifest["readiness"] = json!("pending"),
                "remove_tool" => manifest["tools"].as_array_mut().unwrap().retain(|t| t != "file_read"),
                _ => {}
            }
            fs::write(&path, if change == "malformed" { b"{".to_vec() }
                else { serde_json::to_vec(&manifest).unwrap() }).unwrap();
            // All cached entry authority is withdrawn, not just the edited tool.
            assert_eq!(catalog.tool_gate("file_read", &visibility, &active, &loaded), (false, false));
            assert_eq!(catalog.tool_gate("file_write", &visibility, &active, &loaded), (false, false));
            assert!(!catalog.schemas_with(&p, &loaded, &visibility, &active)
                .iter().any(|s| s["name"] == "file_read" || s["name"] == "file_write"));
            assert!(!catalog.known("codebase_admin"));
            let mut run = json!({"loaded_abilities":loaded});
            let found = catalog.discover_with("list_abilities", &json!({}),
                &mut run, &p, &visibility, &active);
            assert!(found["abilities"].as_array().unwrap().is_empty());
            assert_eq!(catalog.discover_with("load_ability", &json!({"ability_id":"codebase_admin"}),
                &mut run, &p, &visibility, &active)["status"], "denied");
            if change == "remove_tool" {
                let reloaded = Catalog::load(root.path()).unwrap();
                assert_ne!(reloaded.version(), catalog.version());
                assert_eq!(reloaded.tool_gate("file_read", &visibility, &active, &loaded), (false, false));
                assert_eq!(reloaded.tool_gate("file_write", &visibility, &active, &loaded), (true, true));
            }
        }
    }

    #[test]
    fn bundled_skill_access_is_exact_scoped_and_withdraws_changed_assets() {
        let (root, _) = fixture();
        let folder = root.path().join("Administrator/codebase_admin");
        fs::create_dir(folder.join("skills")).unwrap();
        fs::write(folder.join("skills/ui_theming.skill.md"), "Scoped theme guidance").unwrap();
        let catalog = Catalog::load(root.path()).unwrap();
        let visible = json!({"visibility":{"codebase_admin":"visible"}});
        let active = json!({"active":["codebase_admin"],"suppressed":[]});
        let list = catalog.bundled_skill_list("codebase_admin", &visible, &active,
            |_| true, |tool| tool == "file_read").unwrap();
        assert_eq!(list.len(), 2);
        assert!(!list.iter().any(|s| s["kind"] == "tool"));
        let loaded = catalog.bundled_skill_load("codebase_admin", "selectable", "ui_theming",
            &visible, &active, |_| true, |_| true).unwrap();
        assert_eq!(loaded["text"], "Scoped theme guidance");
        assert_eq!(loaded["source_kind"], "untrusted_bundled_guidance");
        assert_eq!(loaded["activates"], false);
        assert_eq!(loaded["sha256"], hex::encode(Sha256::digest(b"Scoped theme guidance")));
        assert!(catalog.bundled_skill_load("codebase_admin", "selectable", "../ui_theming",
            &visible, &active, |_| true, |_| true).is_err());
        assert!(catalog.bundled_skill_load("codebase_admin", "tool", "file_edit",
            &visible, &active, |_| true, |tool| tool == "file_read").is_err());
        assert!(catalog.bundled_skill_list("codebase_admin", &visible, &active, |_| false, |_| true).is_err());
        let off = json!({"visibility":{"codebase_admin":"visible"},"skill_visibility":{"codebase_admin":"off"}});
        assert!(catalog.bundled_skill_list("codebase_admin", &off, &active, |_| true, |_| true).is_err());
        assert!(catalog.bundled_skill_list("codebase_admin", &visible,
            &json!({"active":["codebase_admin"],"suppressed":["codebase_admin"]}), |_| true, |_| true).is_err());
        assert!(catalog.bundled_skill_list("codebase_admin", &json!({}), &json!({}), |_| true, |_| true).is_err());

        let before = catalog.live_version();
        fs::write(folder.join("skills/ui_theming.skill.md"), "Unvalidated replacement").unwrap();
        assert_ne!(catalog.live_version(), before);
        assert!(catalog.bundled_skill_load("codebase_admin", "selectable", "ui_theming",
            &visible, &active, |_| true, |_| true).is_err());
        assert!(!catalog.bundled_skill_list("codebase_admin", &visible, &active, |_| true, |_| true)
            .unwrap().iter().any(|s| s["skill_id"] == "ui_theming"));
        let mut run = json!({"loaded_abilities":["codebase_admin"]});
        let discovery = catalog.discover_with("load_ability", &json!({"ability_id":"codebase_admin"}),
            &mut run, &policy(), &visible, &active);
        assert!(discovery["selectable_skills"].as_array().unwrap().is_empty());
        fs::remove_file(folder.join("codebase_admin.skill.md")).unwrap();
        fs::remove_file(folder.join("tools/file_edit.skill.md")).unwrap();
        let guidance = catalog.visible_guidance(&policy(), &visible, &active);
        assert!(!guidance.contains("Use scoped workspace tools."));
        assert!(!guidance.contains("Match one exact span."));
        let discovery = catalog.discover_with("load_ability", &json!({"ability_id":"codebase_admin"}),
            &mut run, &policy(), &visible, &active);
        assert!(discovery["skill"].is_null());
        assert!(discovery["tool_skills"].as_object().unwrap().is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn symlink_replacement_cannot_restore_cached_bundled_guidance() {
        use std::os::unix::fs::symlink;
        let (root, catalog) = fixture();
        let folder = root.path().join("Administrator/codebase_admin");
        let path = folder.join("codebase_admin.skill.md");
        let original = fs::read(&path).unwrap();
        let alternate = root.path().join("replacement.skill.md");
        fs::write(&alternate, original).unwrap();
        let before = catalog.live_version();
        fs::remove_file(&path).unwrap();
        symlink(&alternate, &path).unwrap();
        assert_ne!(catalog.live_version(), before);
        assert!(catalog.bundled_skill_load("codebase_admin", "ability", "codebase_admin",
            &json!({"visibility":{"codebase_admin":"visible"}}), &json!({}), |_| true, |_| true).is_err());
    }

    fn policy() -> Value {
        json!({"rules":{"file_list":"allow","file_read":"allow","file_search":"allow",
            "state_inspect":"allow","prompt_slot_read":"allow","prompt_slot_write":"require_approval","file_write":"require_approval","file_edit":"require_approval","workspace_seed":"require_approval","source_review":"allow","source_patch":"require_approval","promotion_propose":"allow",
            "shell":"require_approval","python":"deny"}})
    }

    #[test]
    fn model_index_advertises_discoverable_abilities_without_hidden_tools() {
        let (_root, catalog) = fixture();
        let policy = policy();
        let state = json!({"active":[],"suppressed":[]});
        let entries = catalog.catalog_with(&policy, &Value::Null, &state);
        let schemas = catalog.schemas_with(&policy, &json!([]), &Value::Null, &state);
        let index = Catalog::model_index(&entries, &schemas);
        assert!(index.contains("# [ABILITIES]"));
        assert!(index.contains("codebase_admin — Codebase Admin"));
        assert!(index.contains("An empty loaded list does not mean no abilities"));
        assert!(!index.contains("- file_read"));
        assert!(index.len() <= 8 * 1024);
        assert!(!index.contains("Use scoped workspace tools."));
        let crowded = Catalog::model_index(&vec![entries[0].clone(); 512], &schemas);
        assert!(crowded.len() <= 8 * 1024);
        assert!(crowded.contains("More entries are available through list_abilities."));

        let visible = json!({"visibility":{"codebase_admin":"visible"}});
        let entries = catalog.catalog_with(&policy, &visible, &state);
        let schemas = catalog.schemas_with(&policy, &json!([]), &visible, &state);
        let index = Catalog::model_index(&entries, &schemas);
        assert!(!index.contains("# [ABILITIES]"));
        assert!(index.contains("- file_read"));
        assert!(!index.contains("- python"));

        let tool_discoverable = json!({"visibility":{"codebase_admin":"visible"},
            "tool_modes":{"file_read":"discoverable"}});
        let entries = catalog.catalog_with(&policy, &tool_discoverable, &state);
        let schemas = catalog.schemas_with(&policy, &json!([]), &tool_discoverable, &state);
        let index = Catalog::model_index(&entries, &schemas);
        assert!(index.contains("## Load on demand"));
        assert!(index.contains("- file_read — load ability codebase_admin"));

        let suppressed = json!({"active":[],"suppressed":["codebase_admin"]});
        assert!(!Catalog::model_index(&catalog.catalog_with(&policy, &visible, &suppressed),
            &catalog.schemas_with(&policy, &json!([]), &visible, &suppressed))
            .contains("codebase_admin"));
        assert!(!Catalog::model_index(&[], &[]).contains("codebase_admin"));
    }

    #[test]
    fn manifest_and_skill_are_loaded_without_exposing_denied_tools() {
        let (_root, catalog) = fixture();
        let policy = policy();
        assert_eq!(catalog.schemas(&policy, &json!([])).len(), 2);
        let mut run = json!({"loaded_abilities":[]});
        let result = catalog.discover(
            "load_ability",
            &json!({"ability_id":"codebase_admin"}),
            &mut run,
            &policy,
        );
        assert_eq!(result["status"], "ok");
        assert_eq!(result["ability"]["group_name"], "Administrator");
        assert_eq!(result["skill"], "Use scoped workspace tools.");
        assert_eq!(result["tool_skills"]["file_edit"], "Match one exact span.");
        assert!(result["tool_skills"]["python"].is_null());
        assert!(catalog.tool_loaded("file_edit", &run["loaded_abilities"]));
        let names = catalog
            .schemas(&policy, &run["loaded_abilities"])
            .into_iter()
            .map(|schema| schema["name"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>();
        assert!(names.contains(&"file_edit".to_owned()));
        assert!(names.contains(&"state_inspect".to_owned()));
        assert!(names.contains(&"prompt_slot_read".to_owned()));
        assert!(names.contains(&"prompt_slot_write".to_owned()));
        assert!(!names.contains(&"python".to_owned()));
    }

    #[test]
    fn duplicate_tools_fail_closed() {
        let (root, _) = fixture();
        let path = root
            .path()
            .join("Administrator/codebase_admin/codebase_admin.json");
        fs::write(
            &path,
            r#"{"display_name":"Codebase Admin","kind":"ability",
            "description":"Run tools","tools":["shell","shell","python"]}"#,
        )
        .unwrap();
        assert!(Catalog::load(root.path()).is_err());
    }

    #[test]
    fn missing_catalog_or_ability_removes_tools_from_runtime() {
        let (root, catalog) = fixture();
        let missing = root.path().join("removed");
        let empty = Catalog::load(&missing).unwrap();
        let visibility = json!({"visibility":{"codebase_admin":"visible"}});
        let state = json!({"active":["codebase_admin"]});
        assert!(empty.configuration_catalog(&policy(), &visibility).is_empty());
        assert!(!empty.tool_available("file_read", &visibility, &state, &Value::Null));
        assert_ne!(empty.version(), catalog.version());
        fs::remove_dir_all(root.path().join("Administrator/codebase_admin")).unwrap();
        assert!(!catalog.tool_available("file_read", &visibility, &state, &Value::Null));
        assert!(catalog.configuration_catalog(&policy(), &visibility).is_empty());
        let partial = Catalog::load(root.path()).unwrap();
        assert!(!partial.tool_available("file_read", &visibility, &state, &Value::Null));
    }

    #[test]
    fn partial_readiness_exposes_only_compiled_tools_and_withdraws_on_edit() {
        let (root, _) = fixture();
        let path = root.path().join("Core/base");
        fs::create_dir_all(&path).unwrap();
        let manifest = path.join("base.json");
        fs::write(&manifest, r#"{"display_name":"Base","kind":"virtual_core",
            "description":"Partial core","readiness":"partial",
            "tools":["calculate","register_webhook"]}"#).unwrap();
        let catalog = Catalog::load(root.path()).unwrap();
        let policy = json!({"rules":{"calculate":"allow","register_webhook":"allow"}});
        let visibility = json!({"visibility":{"base":"visible"}});
        let state = json!({"active":["base"]});
        let item = catalog.configuration_catalog(&policy, &visibility)
            .into_iter().find(|entry| entry["id"] == "base").unwrap();
        assert_eq!(item["readiness"], "partial");
        assert_eq!(item["available"], true);
        assert!(catalog.tool_available("calculate", &visibility, &state, &Value::Null));
        assert!(!catalog.tool_available("register_webhook", &visibility, &state, &Value::Null));
        fs::write(&manifest, r#"{"display_name":"Base","kind":"virtual_core",
            "description":"Disabled","readiness":"pending","tools":["calculate"]}"#).unwrap();
        assert!(!catalog.tool_available("calculate", &visibility, &state, &Value::Null));
        let disabled = Catalog::load(root.path()).unwrap();
        assert!(!disabled.tool_available("calculate", &visibility, &state, &Value::Null));
    }

    #[test]
    fn catalog_only_v2_tool_is_listed_but_never_executable() {
        let (root, _) = fixture();
        let path = root.path().join("Administrator/computer_control");
        fs::create_dir_all(&path).unwrap();
        fs::write(path.join("computer_control.json"),
            r#"{"display_name":"Computer Control","kind":"ability",
                "description":"Unsupported Computer Control","tools":["computer_screenshot"]}"#).unwrap();
        fs::write(path.join("computer_control.skill.md"), "Control the excluded attached computer.").unwrap();
        let catalog = Catalog::load(root.path()).unwrap();
        let policy = policy();
        let visibility = json!({"visibility":{"computer_control":"visible"}});
        let item = catalog.configuration_catalog(&policy, &visibility)
            .into_iter().find(|item| item["id"] == "computer_control").unwrap();
        assert_eq!(item["available"], false);
        assert_eq!(item["tools"][0]["supported"], false);
        assert!(!catalog.catalog_with(&policy, &visibility, &Value::Null)
            .iter().any(|item| item["id"] == "computer_control"));
        assert!(!catalog.visible_guidance(&policy, &visibility, &Value::Null)
            .contains("Control the excluded attached computer."));
        let mut run = json!({"loaded_abilities":[]});
        assert_eq!(catalog.discover_with("load_ability", &json!({"ability_id":"computer_control"}),
            &mut run, &policy, &visibility, &Value::Null)["status"], "denied");
        assert!(!catalog.tool_available("computer_screenshot", &visibility,
            &json!({"active":["computer_control"],"suppressed":[]}), &Value::Null));
    }

    #[test]
    fn tool_gate_distinguishes_missing_tools_from_hidden_tools() {
        let (_root, catalog) = fixture();
        let state = json!({"active":[],"suppressed":[]});
        let hidden = json!({"visibility":{"codebase_admin":"off"}});
        assert_eq!(catalog.tool_gate("file_read",&hidden,&state,&Value::Null),
            (true,false));
        assert_eq!(catalog.tool_gate("missing",&hidden,&state,&Value::Null),
            (false,false));
        let visible = json!({"visibility":{"codebase_admin":"visible"}});
        assert_eq!(catalog.tool_gate("file_read",&visible,&state,&Value::Null),
            (true,true));
        assert_eq!(catalog.tool_abilities("file_read",&visible,&state,&Value::Null),
            vec!["codebase_admin".to_owned()]);
        assert_eq!(catalog.tool_abilities("file_read",&hidden,&state,&Value::Null),
            vec!["codebase_admin".to_owned()]);
        let disabled_tool = json!({"visibility":{"codebase_admin":"visible"},
            "tool_modes":{"file_read":"off"}});
        assert_eq!(catalog.tool_gate("file_read",&disabled_tool,&state,&Value::Null),
            (true,false));
    }

    #[test]
    fn catalog_version_changes_with_skill() {
        let (root, catalog) = fixture();
        fs::write(
            root.path()
                .join("Administrator/codebase_admin/codebase_admin.skill.md"),
            "New skill guidance",
        )
        .unwrap();
        assert_ne!(
            catalog.version(),
            Catalog::load(root.path()).unwrap().version()
        );
    }

    #[test]
    fn visible_guidance_and_schema_respect_suppression() {
        let (_root, catalog) = fixture();
        let policy = policy();
        let visibility = json!({"revision":1,"visibility":{"codebase_admin":"visible"}});
        let state = json!({"revision":0,"active":[],"suppressed":[]});
        let names = catalog
            .schemas_with(&policy, &json!([]), &visibility, &state)
            .into_iter()
            .map(|v| v["name"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>();
        assert!(names.contains(&"file_edit".to_owned()));
        assert!(catalog
            .visible_guidance(&policy, &visibility, &state)
            .contains("Use scoped workspace tools."));
        let suppressed = json!({"revision":1,"active":[],"suppressed":["codebase_admin"]});
        let names = catalog
            .schemas_with(
                &policy,
                &json!(["codebase_admin"]),
                &visibility,
                &suppressed,
            )
            .into_iter()
            .map(|v| v["name"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>();
        assert!(!names.contains(&"file_edit".to_owned()));
        assert!(catalog
            .visible_guidance(&policy, &visibility, &suppressed)
            .is_empty());
        let mut run = json!({"loaded_abilities":[]});
        let result = catalog.discover_with(
            "load_ability",
            &json!({"ability_id":"codebase_admin"}),
            &mut run,
            &policy,
            &visibility,
            &suppressed,
        );
        assert_eq!(result["status"], "denied");
        assert!(run["loaded_abilities"].as_array().unwrap().is_empty());
    }

    #[test]
    fn visible_guidance_filters_denied_ability_and_tool_text() {
        let (_root, catalog) = fixture();
        let policy = policy();
        let visibility = json!({"visibility":{"codebase_admin":"visible"}});
        let state = json!({"active":[],"suppressed":[]});
        let normal = catalog.visible_guidance(&policy, &visibility, &state);
        assert!(normal.contains("Use scoped workspace tools."));
        assert!(normal.contains("Match one exact span."));

        let denied_ability = catalog.visible_guidance_filtered(
            &policy, &visibility, &state, |_| false, |_| true);
        assert!(denied_ability.is_empty());

        let denied_tool = catalog.visible_guidance_filtered(
            &policy, &visibility, &state, |_| true, |tool| tool != "file_edit");
        assert!(denied_tool.contains("Use scoped workspace tools."));
        assert!(!denied_tool.contains("Match one exact span."));

        let all_tools_denied = catalog.visible_guidance_filtered(
            &policy, &visibility, &state, |_| true, |_| false);
        assert!(all_tools_denied.is_empty());
    }

    #[test]
    fn persisted_active_discoverable_ability_exposes_schemas() {
        let (_root, catalog) = fixture();
        let policy = policy();
        let state = json!({"revision":1,"active":["codebase_admin"],"suppressed":[]});
        let names = catalog
            .schemas_with(&policy, &json!([]), &Value::Null, &state)
            .into_iter()
            .map(|v| v["name"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>();
        assert!(names.contains(&"file_edit".to_owned()));
        assert!(catalog
            .visible_guidance(&policy, &Value::Null, &state)
            .contains("Use scoped workspace tools."));
        let cleared = json!({"revision":2,"active":[],"suppressed":[]});
        assert!(!catalog.tool_available(
            "file_edit",
            &Value::Null,
            &cleared,
            &json!(["codebase_admin"])
        ));
        let revoked =
            catalog.schemas_with(&policy, &json!(["codebase_admin"]), &Value::Null, &cleared);
        assert!(!revoked.iter().any(|schema| schema["name"] == "file_edit"));
        assert!(catalog.known("codebase_admin"));
        assert!(!catalog.known("unknown"));
    }

    #[test]
    fn agent_off_revokes_loaded_ability_but_keeps_configuration_row() {
        let (_root, catalog) = fixture();
        let policy = policy();
        let visibility = json!({"revision":1,"visibility":{"codebase_admin":"off"}});
        let state = json!({"revision":1,"active":["codebase_admin"],"suppressed":[]});
        assert_eq!(catalog.configuration_catalog(&policy, &visibility)[0]["visibility"], "off");
        assert!(catalog.catalog_with(&policy, &visibility, &state).is_empty());
        assert!(!catalog.tool_available("file_edit", &visibility, &state, &json!(["codebase_admin"])));
        assert!(!catalog.schemas_with(&policy, &json!(["codebase_admin"]), &visibility, &state)
            .iter().any(|schema| schema["name"] == "file_edit"));
        assert!(catalog.visible_guidance(&policy, &visibility, &state).is_empty());
        let mut run = json!({"loaded_abilities":["codebase_admin"]});
        assert_eq!(catalog.discover_with("load_ability", &json!({"ability_id":"codebase_admin"}),
            &mut run, &policy, &visibility, &state)["status"], "denied");
    }

    #[test]
    fn bundled_skill_mode_is_independent_of_ability_mode() {
        let (_root, catalog) = fixture();
        let policy = policy();
        let state = json!({"active":[],"suppressed":[]});
        let mut visibility = json!({"visibility":{"codebase_admin":"visible"},
            "skill_visibility":{"codebase_admin":"off"}});
        assert!(!catalog.visible_guidance(&policy, &visibility, &state)
            .contains("Use scoped workspace tools."));
        let mut run = json!({"loaded_abilities":[]});
        assert!(catalog.discover_with("load_ability", &json!({"ability_id":"codebase_admin"}),
            &mut run, &policy, &visibility, &state)["skill"].is_null());
        visibility["skill_visibility"]["codebase_admin"] = json!("discoverable");
        assert!(!catalog.visible_guidance(&policy, &visibility, &state)
            .contains("Use scoped workspace tools."));
        assert_eq!(catalog.discover_with("load_ability", &json!({"ability_id":"codebase_admin"}),
            &mut run, &policy, &visibility, &state)["skill"], "Use scoped workspace tools.");
    }

    // SKILL-LIFETIME-REGRESSION: activation survives transcript folding but not unload.
    #[test]
    fn activated_skill_is_independent_of_transcript() {
        let (_root,catalog)=fixture();let p=policy();
        let v=json!({"visibility":{"codebase_admin":"discoverable"},"skill_visibility":{"codebase_admin":"discoverable"}});
        let active=json!({"active":["codebase_admin"],"suppressed":[]});
        assert!(catalog.visible_guidance(&p,&v,&active).contains("Use scoped workspace tools."));
        let suppressed=json!({"active":[],"suppressed":["codebase_admin"]});
        assert!(!catalog.visible_guidance(&p,&v,&suppressed).contains("Use scoped workspace tools."));
    }

    #[test]
    fn typed_pending_entries_never_expose_tools_or_guidance() {
        let (root, _) = fixture();
        for (id, kind) in [("core_entry", "virtual_core"), ("connection", "oauth"),
            ("chat_channel", "channel"), ("background", "service"), ("future", "placeholder")] {
            let path = root.path().join("Other").join(id);
            fs::create_dir_all(&path).unwrap();
            fs::write(path.join(format!("{id}.json")), serde_json::to_vec(&json!({
                "display_name":id,"description":"Catalog only","kind":kind,
                "readiness":if kind == "placeholder" {"placeholder"} else {"pending"},
                "tools":[], "config":{"settings":[]}
            })).unwrap()).unwrap();
            fs::write(path.join(format!("{id}.skill.md")), "Unsafe v2 instructions").unwrap();
        }
        let catalog = Catalog::load(root.path()).unwrap();
        assert_eq!(catalog.configuration_catalog(&policy(), &Value::Null).len(), 6);
        for id in ["core_entry", "connection", "chat_channel", "background", "future"] {
            let vis = json!({"visibility":{id:"visible"}});
            let mut run = json!({"loaded_abilities":[]});
            assert_eq!(catalog.discover_with("load_ability", &json!({"ability_id":id}),
                &mut run, &policy(), &vis, &Value::Null)["status"], "denied");
            assert_eq!(run["loaded_abilities"], json!([]));
            assert!(!catalog.visible_guidance(&policy(), &vis, &Value::Null).contains("Unsafe v2"));
        }
    }

    #[test]
    fn installed_tool_ownership_and_identifiers_fail_closed() {
        let (root, _) = fixture();
        let path = root.path().join("Other/duplicate");
        fs::create_dir_all(&path).unwrap();
        for tool in ["file_read", "../../escape"] {
            fs::write(path.join("duplicate.json"), serde_json::to_vec(&json!({
                "display_name":"Duplicate","description":"Invalid owner","kind":"ability",
                "tools":[tool]
            })).unwrap()).unwrap();
            assert!(Catalog::load(root.path()).is_err());
        }
    }

    #[test]
    fn unsupported_allow_rule_does_not_expose_catalog_or_skill() {
        let (root, _) = fixture();
        let path = root.path().join("Other/legacy");
        fs::create_dir_all(path.join("tools")).unwrap();
        fs::write(path.join("legacy.json"), r#"{"display_name":"Legacy","description":"Pending","kind":"ability","tools":["v2_only"]}"#).unwrap();
        fs::write(path.join("legacy.skill.md"), "Unsafe guidance").unwrap();
        let catalog = Catalog::load(root.path()).unwrap();
        let p = json!({"rules":{"v2_only":"allow"}});
        let v = json!({"visibility":{"legacy":"visible"}});
        assert!(catalog.catalog_with(&p, &v, &Value::Null).is_empty());
        assert!(catalog.visible_guidance(&p, &v, &Value::Null).is_empty());
    }

    #[test]
    fn malformed_and_oversized_assets_fail_closed() {
        let (root, _) = fixture();
        let path = root.path().join("Administrator/codebase_admin/codebase_admin.json");
        let original = fs::read(&path).unwrap();
        for bytes in [b"{".to_vec(), vec![b' '; MAX_MANIFEST_BYTES as usize + 1]] {
            fs::write(&path, bytes).unwrap();
            assert!(Catalog::load(root.path()).is_err());
        }
        fs::write(&path, original).unwrap();
        let skill = root.path().join("Administrator/codebase_admin/tools/file_edit.skill.md");
        fs::write(&skill, vec![b'x'; MAX_SKILL_BYTES as usize + 1]).unwrap();
        assert!(Catalog::load(root.path()).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn symlink_assets_fail_closed() {
        use std::os::unix::fs::symlink;
        let (root, _) = fixture();
        let skill = root.path().join("Administrator/codebase_admin/tools/file_edit.skill.md");
        fs::remove_file(&skill).unwrap();
        symlink(root.path().join("missing"), &skill).unwrap();
        assert!(Catalog::load(root.path()).is_err());
    }

    #[test]
    fn production_seed_manifest_loads() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("plugins/abilities");
        let catalog = Catalog::load(&root).unwrap();
        assert_eq!(catalog.abilities.len(), 82);
        assert_eq!(catalog.abilities.iter().filter(|entry| Catalog::executable(entry)).count(), 10);
        assert_eq!(catalog.abilities.iter().filter(|entry| entry.readiness == Readiness::Ready).count(), 9);
        assert_eq!(catalog.abilities.iter().filter(|entry| entry.readiness == Readiness::Partial).count(), 1);
        let base = catalog.abilities.iter().find(|entry| entry.id == "base").unwrap();
        let mut installed: Vec<_> = base.tools.iter().filter(|name| Catalog::supported(name)).map(String::as_str).collect();
        installed.sort();
        assert_eq!(installed, ["calculate", "compact_context", "edit_own_prompt", "get_date", "get_task_checkpoint", "get_time", "list_skills", "load_skill", "present_chat_component", "present_plan", "read_attachment", "read_own_prompt", "recall_compacted", "remove_own_skill", "save_own_skill", "search_this_session", "session_search", "set_execution_mode", "similar_sessions", "update_task_checkpoint"]);
        assert!(catalog.abilities.iter().any(|ability| ability.id == "codebase_admin"));
        assert!(catalog.abilities.iter().any(|ability| ability.id == "diagnostics"));
        assert!(catalog.abilities.iter().any(|ability| ability.id == "terminal_control"));
    }
    #[test]
    fn web_access_loading_policy_and_visibility_gates() {
        let catalog=Catalog::load(&Path::new(env!("CARGO_MANIFEST_DIR")).join("plugins/abilities")).unwrap();
        let mut p=crate::policy_default();
        let mut v=json!({"visibility":{"web_access":"discoverable"}});
        let mut run=json!({"loaded_abilities":[]});
        let state=json!({"active":[],"suppressed":[]});
        assert!(!catalog.tool_available("maps_geocode",&v,&state,&json!([])));
        let result=catalog.discover_with("load_ability",&json!({"ability_id":"web_access"}),&mut run,&p,&v,&state);
        assert_eq!(result["status"],"ok");
        assert!(result["tool_skills"]["maps_geocode"].as_str().unwrap().contains("great-circle"));
        let active=json!({"active":["web_access"],"suppressed":[]});
        assert!(catalog.tool_available("maps_geocode",&v,&active,&run["loaded_abilities"]));
        assert_eq!(catalog.tool_abilities("maps_geocode",&v,&active,&run["loaded_abilities"]),vec!["web_access"]);
        p["rules"]["web_search"]=json!("deny");
        assert!(!catalog.schemas_with(&p,&run["loaded_abilities"],&v,&active).iter().any(|s|s["name"]=="web_search"));
        v["visibility"]["web_access"]=json!("off");
        assert!(!catalog.tool_available("maps_geocode",&v,&active,&run["loaded_abilities"]));
    }

}
