//! The closing gate records status and a short notification intent before publication.
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};
use serde::Deserialize;
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClosingConfig {
    pub tool_name: String,
    pub guidance: String,
    pub flags: Vec<String>,
    pub notification_max_chars: usize,
    pub notification_max_words: usize,
    pub fallback_status: String,
    pub fallback_notification: String,
}

pub fn closing_schema(config: &ClosingConfig) -> Value {
    json!({"type":"function","name":config.tool_name,
        "description":"Record the final reply status and a short notification intent before finishing.",
        "parameters":{"type":"object","additionalProperties":false,
            "properties":{"flag":{"type":"string","enum":config.flags},
                "notification":{"type":"string","description":"At most five words"}},
            "required":["flag","notification"]}})
}

pub fn closing_status(args: &Value, config: &ClosingConfig) -> Result<Value> {
    let object=args.as_object().ok_or_else(||anyhow!("Closing status must be an object"))?;
    if object.len()!=2 { bail!("Closing status has unexpected fields"); }
    let flag=object.get("flag").and_then(Value::as_str)
        .ok_or_else(||anyhow!("Closing flag is missing"))?;
    if !config.flags.iter().any(|item|item==flag) { bail!("Closing flag is unsupported"); }
    let notification=object.get("notification").and_then(Value::as_str)
        .ok_or_else(||anyhow!("Closing notification is missing"))?;
    if notification.trim().is_empty()
        || notification.chars().count()>config.notification_max_chars
        || notification.split_whitespace().count()>config.notification_max_words {
        bail!("Closing notification is empty or too long");
    }
    Ok(json!({"flag":flag,"notification":notification,"fallback":false}))
}

pub fn fallback_status(config: &ClosingConfig) -> Value {
    json!({"flag":config.fallback_status,
        "notification":config.fallback_notification,"fallback":true})
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn closer_rejects_unknown_or_long_status() {
        let config=crate::gate_registry::closing_config().unwrap();
        assert_eq!(closing_status(&json!({"flag":"complete","notification":"Done"}),&config)
            .unwrap()["flag"],"complete");
        assert!(closing_status(&json!({"flag":"unknown","notification":"Done"}),&config).is_err());
        assert!(closing_status(&json!({"flag":"complete","notification":"one two three four five six"}),&config).is_err());
        assert_eq!(fallback_status(&config)["flag"],"unverified");
    }
}
