//! Packaged gate descriptors define which module can run at each fixed Rust checkpoint.
//! Operators select modules in workflow JSON; a descriptor never grants tools or publication.
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeSet;
use crate::workflow::{Checkpoint, Policy};

pub const MESSAGE_VISIBILITY: &str = "message_visibility_v1";
pub const CLOSING_STATUS: &str = "closing_status_v1";
pub const PUBLICATION_SIZE: &str = "publication_size_guard_v1";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Runtime { Rust, PythonGate }

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub schema_version: u32,
    pub id: String,
    pub checkpoint: Checkpoint,
    pub runtime: Runtime,
    pub config: Value,
}

#[derive(Debug, Clone)]
pub struct Selection {
    pub id: String,
    pub optional: bool,
    pub manifest: Manifest,
}

fn source(id: &str) -> Result<String> {
    match id {
        MESSAGE_VISIBILITY | CLOSING_STATUS => {
            // The host's v3/data/config/gates file is mounted at /state/config/gates.
            // Tests read that same checkout file; no packaged fallback can silently
            // override an operator edit or hide a missing integral configuration.
            let root = if cfg!(test) {
                std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data")
            } else {
                std::path::PathBuf::from(std::env::var("V3_STATE_ROOT")
                    .unwrap_or_else(|_| "/state".into()))
            };
            let path = root.join("config/gates").join(format!("{id}.json"));
            Ok(std::fs::read_to_string(path)?)
        }
        PUBLICATION_SIZE => Ok(include_str!("../../plugins/gates/publication_size_guard_v1/gate.json").into()),
        _ => bail!("Unknown gate module"),
    }
}

pub fn manifest(id: &str) -> Result<Manifest> {
    let item: Manifest = serde_json::from_str(&source(id)?)?;
    validate_manifest(id, &item)?;
    Ok(item)
}

fn validate_manifest(id: &str, item: &Manifest) -> Result<()> {
    if item.schema_version != 1 || item.id != id {
        bail!("Invalid gate manifest identity");
    }
    // Each built-in descriptor is bounded and tied to its implementation. A future
    // plugin adapter can add IDs here only after its effects and data access are reviewed.
    match id {
        MESSAGE_VISIBILITY => {
            if item.checkpoint != Checkpoint::ModelDispatch || item.runtime != Runtime::Rust {
                bail!("Invalid message-visibility gate binding");
            }
            let config: VisibilityConfig = serde_json::from_value(item.config.clone())?;
            if config.tool_name != "publish_main_message"
                || config.guidance.is_empty() || config.guidance.len() > 4096 {
                bail!("Invalid message-visibility gate config");
            }
        }
        CLOSING_STATUS => {
            if item.checkpoint != Checkpoint::Publication || item.runtime != Runtime::Rust {
                bail!("Invalid closing-status gate binding");
            }
            let config: ClosingConfig = serde_json::from_value(item.config.clone())?;
            let expected = ["needs_input", "complete", "error", "partial", "unverified"];
            if config.tool_name != "set_closer_status"
                || config.flags.iter().map(String::as_str).collect::<Vec<_>>() != expected
                || config.fallback_status != "unverified"
                || !(1..=240).contains(&config.notification_max_chars)
                || !(1..=50).contains(&config.notification_max_words)
                || config.guidance.is_empty() || config.guidance.len() > 4096
                || config.fallback_notification.is_empty()
                || config.fallback_notification.chars().count() > config.notification_max_chars
                || config.fallback_notification.split_whitespace().count() > config.notification_max_words {
                bail!("Invalid closing-status gate config");
            }
        }
        PUBLICATION_SIZE => {
            if item.checkpoint != Checkpoint::Publication || item.runtime != Runtime::PythonGate {
                bail!("Invalid publication-size gate binding");
            }
            let config: SizeConfig = serde_json::from_value(item.config.clone())?;
            if !(1..=262144).contains(&config.max_text_bytes) {
                bail!("Invalid publication-size gate config");
            }
        }
        _ => unreachable!(),
    }
    Ok(())
}

pub use crate::gate_controls::{ClosingConfig, VisibilityConfig};
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SizeConfig { pub max_text_bytes: usize }

pub fn visibility_config() -> Result<VisibilityConfig> {
    Ok(serde_json::from_value(manifest(MESSAGE_VISIBILITY)?.config)?)
}
pub fn closing_config() -> Result<ClosingConfig> {
    Ok(serde_json::from_value(manifest(CLOSING_STATUS)?.config)?)
}

pub fn parse_selection(checkpoint: Checkpoint, configured: &str) -> Result<Selection> {
    let (optional, id) = match configured.strip_prefix("optional:") {
        Some(id) => (true, id),
        None => (false, configured),
    };
    let item = manifest(id)?;
    if item.checkpoint != checkpoint || (optional && item.runtime != Runtime::PythonGate) {
        bail!("Gate module cannot run at this checkpoint or in optional mode");
    }
    Ok(Selection { id: id.to_owned(), optional, manifest: item })
}

/// Preserve operator order, but reject duplicate IDs even if one spelling is optional.
pub fn selected(policy: &Policy, checkpoint: Checkpoint) -> Result<Vec<Selection>> {
    let name = serde_json::to_value(checkpoint)?.as_str().unwrap().to_owned();
    let mut seen = BTreeSet::new();
    let mut result = Vec::new();
    if let Some(configured) = policy.modules.get(&name) {
        if configured.len() > 8 { bail!("Too many gate modules at checkpoint"); }
        for raw in configured {
            let selection = parse_selection(checkpoint, raw)?;
            if !seen.insert(selection.id.clone()) { bail!("Duplicate gate module"); }
            result.push(selection);
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn packaged_manifests_are_bounded_and_typed() {
        let visibility = visibility_config().unwrap();
        assert_eq!(visibility.tool_name, "publish_main_message");
        assert!(visibility.guidance.contains("publish_main_message"));
        let closer = closing_config().unwrap();
        assert_eq!(closer.fallback_status, "unverified");
        assert!(closer.notification_max_chars <= 240);
        assert_eq!(manifest(PUBLICATION_SIZE).unwrap().runtime, Runtime::PythonGate);
        assert!(closer.notification_max_words <= 50);
    }
    #[test]
    fn malformed_integral_and_plugin_configs_fail_closed() {
        let mut close=manifest(CLOSING_STATUS).unwrap();
        close.config["notification_max_words"]=json!(0);
        assert!(validate_manifest(CLOSING_STATUS,&close).is_err());
        close.config["notification_max_words"]=json!(5);
        close.config["fallback_status"]=json!("complete");
        assert!(validate_manifest(CLOSING_STATUS,&close).is_err());
        let mut visibility=manifest(MESSAGE_VISIBILITY).unwrap();
        visibility.config["guidance"]=json!("");
        assert!(validate_manifest(MESSAGE_VISIBILITY,&visibility).is_err());
        let mut size=manifest(PUBLICATION_SIZE).unwrap();
        size.config["max_text_bytes"]=json!(262145);
        assert!(validate_manifest(PUBLICATION_SIZE,&size).is_err());
        size.config["max_text_bytes"]=json!(8192);
        size.checkpoint=Checkpoint::ModelDispatch;
        assert!(validate_manifest(PUBLICATION_SIZE,&size).is_err());
    }
    #[test]
    fn selection_rejects_unknown_duplicates_and_wrong_checkpoint() {
        for modules in [
            json!({"publication":["unknown"]}),
            json!({"publication":[PUBLICATION_SIZE, format!("optional:{PUBLICATION_SIZE}")]}),
            json!({"model_dispatch":[CLOSING_STATUS]}),
            json!({"publication":[format!("optional:{CLOSING_STATUS}")]}),
        ] {
            assert!(Policy::resolve(Some(&json!({"modules":modules}))).is_err());
        }
        let policy=Policy::resolve(Some(&json!({"modules":{
            "model_dispatch":[MESSAGE_VISIBILITY],
            "publication":[CLOSING_STATUS,format!("optional:{PUBLICATION_SIZE}")]
        }}))).unwrap();
        assert_eq!(selected(&policy,Checkpoint::Publication).unwrap().len(),2);
    }
}
