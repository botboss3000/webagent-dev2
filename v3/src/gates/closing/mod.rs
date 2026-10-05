//! The closing gate records status and a short notification intent before publication.
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};
use serde::{Deserialize, Serialize};
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

/// Agent preferences guide the private closing notification.
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AgentClosing {
    pub notification_guidance: String,
}
impl AgentClosing {
    pub fn validate(&self) -> Result<()> {
        if self.notification_guidance.len() > 4096 {
            bail!("Notification guidance must be at most 4096 bytes");
        }
        Ok(())
    }
}

pub async fn agent_closing(store: &crate::store::Store, agent: &str) -> Result<AgentClosing> {
    let view = store.for_agent(agent).await?;
    let blob: Vec<u8> = sqlx::query_scalar("SELECT metadata FROM agents WHERE id=?")
        .bind(agent).fetch_one(&view.agent_pool).await?;
    let metadata = view.unseal(&blob)?;
    let settings = if metadata["closing_message"].is_null() { AgentClosing::default() }
        else { serde_json::from_value(metadata["closing_message"].clone())? };
    settings.validate()?;
    Ok(settings)
}

pub fn guidance(config: &ClosingConfig, settings: &AgentClosing) -> String {
    let mut text = config.guidance.clone();
    if !settings.notification_guidance.trim().is_empty() {
        text.push_str("\nFor the private closing notification only, follow this agent guidance:\n");
        text.push_str(&settings.notification_guidance);
    }
    text
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
    #[test]
    fn agent_guidance_is_bounded_and_keeps_the_fixed_protocol() {
        let config=crate::gate_registry::closing_config().unwrap();
        let settings=AgentClosing{notification_guidance:"Name the next step".into()};
        settings.validate().unwrap();
        let combined=guidance(&config,&settings);
        assert!(combined.starts_with(&config.guidance));
        assert!(combined.contains("Name the next step"));
        assert!(AgentClosing{notification_guidance:"x".repeat(4097)}.validate().is_err());
    }
}
