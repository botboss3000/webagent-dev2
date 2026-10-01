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
const MAX_ABILITIES: usize = 128;

#[derive(Clone, Debug, Serialize)]
struct Ability {
    id: String,
    #[serde(skip_serializing)]
    path: PathBuf,
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
    tools: Vec<String>,
    skill: Option<String>,
    tool_skills: BTreeMap<String, String>,
}

#[derive(Deserialize)]
struct Manifest {
    display_name: String,
    kind: String,
    description: String,
    tools: Vec<String>,
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
    fs::read(path).with_context(|| format!("Cannot read ability file: {}", path.display()))
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    let data = read_bytes(path, MAX_MANIFEST_BYTES)?;
    serde_json::from_slice(&data)
        .with_context(|| format!("Invalid ability manifest: {}", path.display()))
}

fn optional_skill(path: &Path, limit: u64) -> Result<Option<String>> {
    if !path.exists() {
        return Ok(None);
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
                if !folder.file_type()?.is_dir() {
                    continue;
                }
                let id = folder.file_name().to_string_lossy().to_string();
                if !valid_id(&id) || !ids.insert(id.clone()) {
                    bail!("Invalid or duplicate ability identifier: {id}");
                }
                let ability_path = folder.path();
                let manifest: Manifest = read_json(&ability_path.join(format!("{id}.json")))?;
                if manifest.kind != "ability"
                    || manifest.placeholder
                    || manifest.display_name.is_empty()
                    || manifest.display_name.len() > 100
                    || manifest.description.is_empty()
                    || manifest.description.len() > 1000
                    || manifest.tools.is_empty()
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
                let mut local = HashSet::new();
                let mut tool_skills = BTreeMap::new();
                for tool in &manifest.tools {
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
                let skill = optional_skill(
                    &ability_path.join(format!("{id}.skill.md")),
                    MAX_SKILL_BYTES,
                )?;
                abilities.push(Ability {
                    id,
                    path: ability_path,
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
                    tools: manifest.tools,
                    skill,
                    tool_skills,
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
        ability.path.is_dir() && ability.path.join(format!("{}.json", ability.id)).is_file()
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
        Self::present(ability)
            && !Self::is_off(visibility, &ability.id)
            && !Self::suppressed(state, &ability.id)
            && (Self::is_visible(visibility, &ability.id) || Self::active(state, &ability.id))
    }

    fn describe(ability: &Ability, policy: &Value, visibility: &Value, state: &Value) -> Value {
        let tools = ability
            .tools
            .iter()
            .filter(|name| Self::allowed(policy, name) && Self::tool_mode(visibility, name) != "off")
            .map(|name| json!({"name":name,"policy":policy["rules"][name],
                        "mode":Self::tool_mode(visibility,name),"supported":Self::supported(name)}))
            .collect::<Vec<_>>();
        let visible = Self::is_visible(visibility, &ability.id);
        let off = Self::is_off(visibility, &ability.id);
        json!({"id":ability.id,"group":ability.group,"group_name":ability.group_name,
            "name":ability.name,"description":ability.description,
            "skill_summary":ability.skill_summary,"migration_note":ability.migration_note,
            "icon":ability.icon,"color":ability.color,"tools":tools,
            "has_skill":ability.skill.is_some(),
            "skill_visibility":Self::skill_mode(visibility, &ability.id),
            "visibility":if off {"off"} else if visible {"visible"} else {"discoverable"},
            "active":Self::active(state, &ability.id)})
    }

    pub fn catalog_with(&self, policy: &Value, visibility: &Value, state: &Value) -> Vec<Value> {
        self.abilities
            .iter()
            .filter(|ability| {
                Self::present(ability)
                    && !Self::is_off(visibility, &ability.id)
                    && !Self::suppressed(state, &ability.id)
                    && ability.tools.iter().any(|name| Self::allowed(policy, name) && Self::tool_mode(visibility,name) != "off")
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
                item["available"] = json!(ability.tools.iter().any(|name| Self::supported(name)));
                item["tools"] = json!(ability.tools.iter().map(|name| {
                    json!({"name":name,"policy":policy["rules"][name],
                        "mode":Self::tool_mode(visibility,name),"supported":Self::supported(name)})
                }).collect::<Vec<_>>());
                item
            })
            .collect()
    }

    pub fn tool_available(
        &self,
        name: &str,
        visibility: &Value,
        state: &Value,
        loaded: &Value,
    ) -> bool {
        self.abilities.iter().any(|ability| {
            Self::present(ability)
                && ability.tools.iter().any(|tool| tool == name)
                && Self::supported(name)
                && match Self::tool_mode(visibility, name) {
                    "off" => false,
                    "discoverable" => Self::contains_id(loaded, &ability.id),
                    _ => true,
                }
                && Self::available(ability, visibility, state)
        })
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
        const LIMIT: usize = 24 * 1024;
        const MARKER_RESERVE: usize = 256;
        let mut text = String::new();
        for ability in &self.abilities {
            if !Self::present(ability)
                || Self::is_off(visibility, &ability.id)
                || !ability.tools.iter().any(|tool| Self::allowed(policy, tool))
                || Self::suppressed(state, &ability.id)
                || !(Self::is_visible(visibility, &ability.id) || Self::active(state, &ability.id))
            {
                continue;
            }
            let mut part = format!("\n\nVisible ability {} ({}):", ability.id, ability.name);
            if Self::skill_mode(visibility, &ability.id) == "visible" {
                if let Some(skill) = &ability.skill {
                    part.push_str("\n");
                    part.push_str(skill);
                }
            }
            for (tool, skill) in &ability.tool_skills {
                if Self::allowed(policy, tool) && Self::tool_mode(visibility, tool) == "visible" {
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
                    .filter(|(tool, _)| Self::allowed(policy, tool) && Self::tool_mode(visibility, tool) != "off")
                    .collect::<BTreeMap<_, _>>();
                json!({"status":"ok","ability":ability,
                    "skill":if Self::skill_mode(visibility, id) == "off" {None} else {entry.skill.as_ref()},
                    "tool_skills":permitted_skills,"catalog_version":self.version})
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

    fn policy() -> Value {
        json!({"rules":{"file_list":"allow","file_read":"allow","file_search":"allow",
            "state_inspect":"allow","prompt_slot_read":"allow","prompt_slot_write":"require_approval","file_write":"require_approval","file_edit":"require_approval","workspace_seed":"require_approval","source_review":"allow","source_patch":"require_approval","promotion_propose":"allow",
            "shell":"require_approval","python":"deny"}})
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
    fn catalog_only_v2_tool_is_listed_but_never_executable() {
        let (root, _) = fixture();
        let path = root.path().join("Administrator/diagnostics");
        fs::create_dir_all(&path).unwrap();
        fs::write(path.join("diagnostics.json"),
            r#"{"display_name":"Diagnostics","kind":"ability",
                "description":"V2 diagnostics","tools":["read_diagnostics"]}"#).unwrap();
        fs::write(path.join("diagnostics.skill.md"), "Inspect host diagnostics.").unwrap();
        let catalog = Catalog::load(root.path()).unwrap();
        let policy = policy();
        let visibility = json!({"visibility":{"diagnostics":"visible"}});
        let item = catalog.configuration_catalog(&policy, &visibility)
            .into_iter().find(|item| item["id"] == "diagnostics").unwrap();
        assert_eq!(item["available"], false);
        assert_eq!(item["tools"][0]["supported"], false);
        assert!(!catalog.catalog_with(&policy, &visibility, &Value::Null)
            .iter().any(|item| item["id"] == "diagnostics"));
        assert!(!catalog.visible_guidance(&policy, &visibility, &Value::Null)
            .contains("Inspect host diagnostics."));
        let mut run = json!({"loaded_abilities":[]});
        assert_eq!(catalog.discover_with("load_ability", &json!({"ability_id":"diagnostics"}),
            &mut run, &policy, &visibility, &Value::Null)["status"], "denied");
        assert!(!catalog.tool_available("read_diagnostics", &visibility,
            &json!({"active":["diagnostics"],"suppressed":[]}), &Value::Null));
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

    #[test]
    fn production_seed_manifest_loads() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("plugins/abilities");
        let catalog = Catalog::load(&root).unwrap();
        assert_eq!(catalog.abilities.len(), 8);
        assert!(catalog.abilities.iter().any(|ability| ability.id == "codebase_admin"));
        assert!(catalog.abilities.iter().any(|ability| ability.id == "diagnostics"));
        assert!(catalog.abilities.iter().any(|ability| ability.id == "terminal_control"));
    }
}
