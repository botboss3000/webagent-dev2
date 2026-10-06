//! The initial gate owns bounded working updates visible before the final reply.
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};
use serde::{Deserialize, Serialize};
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VisibilityConfig { pub tool_name: String, pub guidance: String }

/// The agent selects one publication path; the server still owns the manual protocol.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PromotionMode { None, Manual, Auto }

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AgentVisibility { pub mode: PromotionMode, pub guidance: String }
impl Default for AgentVisibility {
    fn default() -> Self { Self { mode: PromotionMode::Manual, guidance: String::new() } }
}
impl AgentVisibility {
    pub fn validate(&self) -> Result<()> {
        if self.guidance.len() > 4096 { bail!("Additional guidance must be at most 4096 bytes"); }
        Ok(())
    }
    pub fn from_stored(value: &Value) -> Result<Self> {
        if value.is_null() { return Ok(Self::default()); }
        if value.get("mode").is_some() { return Ok(serde_json::from_value(value.clone())?); }
        // Existing agents used a boolean. The retired format field is intentionally
        // ignored: Ask/Plan/Auto prompts now supply call-scoped formatting.
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Legacy { enabled: bool, guidance: String, #[serde(default)] format: String }
        let legacy: Legacy = serde_json::from_value(value.clone())?;
        let _ = legacy.format;
        Ok(Self { mode: if legacy.enabled { PromotionMode::Manual } else { PromotionMode::None },
            guidance: legacy.guidance })
    }
}
pub async fn agent_visibility(store: &crate::store::Store, agent: &str) -> Result<AgentVisibility> {
    let view = store.for_agent(agent).await?;
    let blob: Vec<u8> = sqlx::query_scalar("SELECT metadata FROM agents WHERE id=?")
        .bind(agent).fetch_one(&view.agent_pool).await?;
    let metadata = view.unseal(&blob)?;
    let settings = AgentVisibility::from_stored(&metadata["message_visibility"])?;
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
        let defaults=AgentVisibility::default();assert_eq!(defaults.mode,PromotionMode::Manual);assert!(defaults.guidance.is_empty());
        assert!(AgentVisibility{mode:PromotionMode::None,guidance:"x".repeat(4096)}.validate().is_ok());
        assert!(AgentVisibility{mode:PromotionMode::Auto,guidance:"é".repeat(2049)}.validate().is_err());
        assert_eq!(AgentVisibility::from_stored(&json!({"enabled":true,"guidance":"ok","format":"retired"})).unwrap().mode,PromotionMode::Manual);
        assert_eq!(AgentVisibility::from_stored(&json!({"enabled":false,"guidance":"ok"})).unwrap().mode,PromotionMode::None);
        assert_eq!(AgentVisibility::from_stored(&json!({"mode":"auto","guidance":"ok"})).unwrap().mode,PromotionMode::Auto);
        assert!(AgentVisibility::from_stored(&json!({"mode":"auto","guidance":"ok","format":"retired"})).is_err());
        assert!(AgentVisibility::from_stored(&json!({"enabled":true,"guidance":"ok","fixed_protocol":"override"})).is_err());
        assert!(AgentVisibility::from_stored(&json!({"mode":"both","guidance":"ok"})).is_err());
    }
    #[test]
    fn working_update_is_bounded_and_exact() {
        assert_eq!(working_text(&json!({"text":"Progress"})).unwrap(),"Progress");
        assert!(working_text(&json!({"text":" ","extra":1})).is_err());
        assert!(working_text(&json!({"text":"x".repeat(16385)})).is_err());
    }
}
