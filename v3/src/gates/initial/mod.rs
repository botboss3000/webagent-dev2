//! The initial gate owns bounded working updates visible before the final reply.
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};
use serde::Deserialize;
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VisibilityConfig { pub tool_name: String, pub guidance: String }

/// Per-agent preferences never replace the server-owned gate protocol.
#[derive(Debug, Clone, Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub struct AgentVisibility { pub enabled: bool, pub guidance: String, #[serde(default)] pub format: String }
impl Default for AgentVisibility {
    fn default() -> Self { Self { enabled: true, guidance: String::new(), format: String::new() } }
}
impl AgentVisibility {
    pub fn validate(&self) -> Result<()> {
        if self.guidance.len() > 4096 { bail!("Additional guidance must be at most 4096 bytes"); }
        if self.format.len() > 4096 { bail!("Message format must be at most 4096 bytes"); }
        Ok(())
    }
}
pub async fn agent_visibility(store: &crate::store::Store, agent: &str) -> Result<AgentVisibility> {
    let view = store.for_agent(agent).await?;
    let blob: Vec<u8> = sqlx::query_scalar("SELECT metadata FROM agents WHERE id=?")
        .bind(agent).fetch_one(&view.agent_pool).await?;
    let metadata = view.unseal(&blob)?;
    let settings = if metadata["message_visibility"].is_null() { AgentVisibility::default() }
        else { serde_json::from_value(metadata["message_visibility"].clone())? };
    settings.validate()?;
    Ok(settings)
}

pub fn working_schema(config: &VisibilityConfig) -> Value {
    json!({"type":"function","name":config.tool_name,
        "description":"Publish a short user-visible working update while this run continues.",
        "parameters":{"type":"object","additionalProperties":false,
            "properties":{"text":{"type":"string","description":"Complete clean working update"}},
            "required":["text"]}})
}

pub fn working_text(args: &Value) -> Result<String> {
    let object=args.as_object().ok_or_else(||anyhow!("Working update must be an object"))?;
    if object.len()!=1 { bail!("Working update has unexpected fields"); }
    let text=object.get("text").and_then(Value::as_str)
        .ok_or_else(||anyhow!("Working update text is missing"))?;
    if text.trim().is_empty() || text.len()>16384 { bail!("Working update text is empty or too large"); }
    Ok(text.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn agent_visibility_is_additive_strict_and_byte_bounded() {
        let defaults=AgentVisibility::default();assert!(defaults.enabled);assert!(defaults.guidance.is_empty());assert!(defaults.format.is_empty());
        assert!(AgentVisibility{enabled:false,guidance:"x".repeat(4096),format:"Short lines".into()}.validate().is_ok());
        assert!(AgentVisibility{enabled:true,guidance:"é".repeat(2049),format:String::new()}.validate().is_err());
        assert!(AgentVisibility{enabled:true,guidance:String::new(),format:"x".repeat(4097)}.validate().is_err());
        assert_eq!(serde_json::from_value::<AgentVisibility>(json!({"enabled":true,"guidance":"ok"})).unwrap().format,"");
        assert!(serde_json::from_value::<AgentVisibility>(json!({"enabled":true,"guidance":"ok","fixed_protocol":"override"})).is_err());
        assert!(serde_json::from_value::<AgentVisibility>(json!({"enabled":"yes","guidance":"ok"})).is_err());
    }
    #[test]
    fn working_update_is_bounded_and_exact() {
        assert_eq!(working_text(&json!({"text":"Progress"})).unwrap(),"Progress");
        assert!(working_text(&json!({"text":" ","extra":1})).is_err());
        assert!(working_text(&json!({"text":"x".repeat(16385)})).is_err());
    }
}
