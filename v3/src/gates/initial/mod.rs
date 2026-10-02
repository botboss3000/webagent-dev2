//! The initial gate owns bounded working updates visible before the final reply.
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};
use serde::Deserialize;
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VisibilityConfig { pub tool_name: String, pub guidance: String }

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
    fn working_update_is_bounded_and_exact() {
        assert_eq!(working_text(&json!({"text":"Progress"})).unwrap(),"Progress");
        assert!(working_text(&json!({"text":" ","extra":1})).is_err());
        assert!(working_text(&json!({"text":"x".repeat(16385)})).is_err());
    }
}
