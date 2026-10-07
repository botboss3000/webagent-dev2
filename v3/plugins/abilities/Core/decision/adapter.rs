//! Native TypeSafe decision adapter. Rust owns model authority and credentials.
use crate::{App, session_agent_id};
use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{sync::OnceLock, time::Duration};
use tokio_util::sync::CancellationToken;

const ENDPOINT: &str = "https://api.typesafe.ai/v1/systemone";
const MAX_REQUEST: usize = 64 * 1024;
const MAX_RESPONSE: usize = 512 * 1024;

pub fn owns(name: &str) -> bool { name == "decide_with_jev" }

pub fn schemas() -> Vec<Value> {
    vec![json!({"type":"function","name":"decide_with_jev",
        "description":"Ask the assigned TypeSafe Jev Decision model typed questions about supplied state; it cannot read files or call tools",
        "parameters":{"type":"object","properties":{"request_json":{"type":"string",
            "description":"JSON System One request with state, named choice/score/noul questions, and optional model matching the assigned Decision model",
            "minLength":2,"maxLength":65536}},"required":["request_json"],"additionalProperties":false}})]
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Args { request_json: String }

fn valid_content(value: &Value) -> bool { value.is_string() || value.is_object() || value.is_array() }

fn validate(raw: &str, assigned_model: &str) -> Result<Value> {
    if raw.len() > MAX_REQUEST { bail!("Decision request exceeds 64 KiB"); }
    let mut body: Value = serde_json::from_str(raw).context("Invalid Decision JSON")?;
    let object = body.as_object_mut().context("Decision request must be an object")?;
    if object.keys().any(|key| !matches!(key.as_str(), "state" | "questions" | "model")) {
        bail!("Unsupported Decision request field");
    }
    let state = object.get("state").context("Decision state required")?;
    if !valid_content(state) { bail!("Decision state must be text, object, or array"); }
    if let Some(model) = object.get("model") {
        if model.as_str() != Some(assigned_model) { bail!("Decision model must match the assigned role"); }
    }
    let questions = object.get("questions").and_then(Value::as_object)
        .context("Named Decision questions required")?;
    if !(1..=32).contains(&questions.len()) { bail!("Decision needs 1 to 32 questions"); }
    for (name, question) in questions {
        if name.is_empty() || name.len() > 100 { bail!("Invalid Decision question name"); }
        let q = question.as_object().context("Decision question must be an object")?;
        if q.keys().any(|key| !matches!(key.as_str(), "type" | "instructions" | "criteria")) {
            bail!("Unsupported Decision question field");
        }
        if q.get("instructions").is_some_and(|v| !v.is_null() && !valid_content(v)) {
            bail!("Invalid Decision instructions");
        }
        match q.get("type").and_then(Value::as_str) {
            Some("choice") => {
                let criteria = q.get("criteria").and_then(Value::as_object).context("Choice criteria required")?;
                if criteria.is_empty() || criteria.len() > 64 || criteria.keys().any(|key| key.is_empty()) {
                    bail!("Choice needs 1 to 64 named options");
                }
                if criteria.values().any(|v| !v.is_null() && !valid_content(v)) { bail!("Invalid choice criteria"); }
            }
            Some("score") => {
                let criteria = q.get("criteria").and_then(Value::as_array).context("Score rubric required")?;
                if criteria.is_empty() || criteria.len() > 64 || criteria.iter().any(|v| !valid_content(v)) {
                    bail!("Score needs 1 to 64 ordered levels");
                }
            }
            Some("noul") => {
                if q.get("criteria").is_some_and(|v| !v.is_null() && (!v.is_object()
                    || v.as_object().is_some_and(|o| o.keys().any(|k| !matches!(k.as_str(), "true" | "false"))))) {
                    bail!("Invalid noul criteria");
                }
            }
            _ => bail!("Decision question type must be choice, score, or noul"),
        }
    }
    object.insert("model".into(), json!(assigned_model));
    Ok(body)
}

fn validate_answer(body: &Value, answer: &Value) -> Result<()> {
    let expected = body["questions"].as_object().context("Questions missing")?;
    let answers = answer["answers"].as_object().context("Decision answers missing")?;
    if answers.len() != expected.len() { bail!("Decision answer count mismatch"); }
    for (name, question) in expected {
        let value = answers.get(name).context("Decision answer missing")?;
        let kind = question["type"].as_str().context("Question type missing")?;
        if value["type"] != kind { bail!("Decision answer type mismatch"); }
        match kind {
            "choice" if value["choice"].as_str().is_none()
                || value["confidence"].as_f64().is_none()
                || !value["probabilities"].is_object() => bail!("Malformed choice answer"),
            "score" if value["score"].as_f64().is_none()
                || value["confidence"].as_f64().is_none()
                || !value["legend"].is_object()
                || !value["probabilities"].is_object() => bail!("Malformed score answer"),
            "noul" if value["noul"].as_f64().is_none() => bail!("Malformed noul answer"),
            _ => {}
        }
    }
    if answer["model"].as_str().is_none() || !answer["usage"].is_object() {
        bail!("Decision response metadata missing");
    }
    Ok(())
}

fn client() -> Result<&'static reqwest::Client> {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    if let Some(client) = CLIENT.get() { return Ok(client); }
    let client = reqwest::Client::builder().https_only(true).no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(5)).timeout(Duration::from_secs(20)).build()?;
    let _ = CLIENT.set(client);
    Ok(CLIENT.get().expect("Decision client initialized"))
}

async fn response_json(mut response: reqwest::Response) -> Result<Value> {
    if response.status() != reqwest::StatusCode::OK {
        bail!("TypeSafe HTTP {}", response.status().as_u16());
    }
    if response.content_length().is_some_and(|n| n > MAX_RESPONSE as u64) {
        bail!("TypeSafe response exceeds limit");
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.context("TypeSafe response unavailable")? {
        if bytes.len() + chunk.len() > MAX_RESPONSE { bail!("TypeSafe response exceeds limit"); }
        bytes.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&bytes).context("Invalid TypeSafe response JSON")
}

async fn work(app: &App, sid: &str, run_id: &str, args: &Value) -> Result<Value> {
    let input: Args = serde_json::from_value(args.clone()).context("Invalid Decision arguments")?;
    // Session ownership comes from Rust; the model cannot choose another agent or credential.
    let agent = session_agent_id(app, sid).await?;
    let run = app.run(run_id).await?;
    if run["session_id"] != sid || run["agent_id"] != agent || run["status"] != "running" {
        bail!("Decision run unavailable");
    }
    let user = app.store.current_user_id();
    let access = app.store.live_access(user, &agent).await?;
    if !access.admitted() { bail!("Agent access unavailable"); }
    let catalog = app.store.model_catalog().await?;
    let plan = catalog.resolve_role(&agent, crate::model_selection::ModelRole::Decision)
        .context("Assign a Decision model to this agent")?;
    let candidate = plan.authorize_candidate(0, &catalog.entries, &catalog.connections)?;
    if candidate.transport != crate::model_selection::Transport::Typesafe {
        bail!("Decision tool requires a TypeSafe model");
    }
    if candidate.platform.is_none() && !access.model_allowed(&candidate.upstream_model) {
        bail!("Decision model outside account tier");
    }
    let body = validate(&input.request_json, &candidate.upstream_model)?;
    let tokens = if let Some(pin) = &candidate.platform {
        app.store.platform_dispatch_tokens(user, &agent, pin).await?
    } else {
        app.store.get(&candidate.credential_ref).await?
            .context("Decision model API key is not configured")?
    };
    let key = tokens["api_key"].as_str().filter(|key| !key.is_empty())
        .context("Decision model API key is not configured")?;
    let response = client()?.post(ENDPOINT).bearer_auth(key).json(&body).send().await
        .map_err(|_| anyhow::anyhow!("TypeSafe request unavailable"))?;
    let answer = response_json(response).await?;
    validate_answer(&body, &answer)?;
    // A result from a revoked or replaced model must not return to the agent.
    let current = app.run(run_id).await?;
    let live = app.store.model_catalog().await?;
    if current["generation"] != run["generation"] || current["status"] != "running"
        || app.shutoff().await? || !app.store.live_access(user, &agent).await?.admitted()
        || live.resolve_role(&agent, crate::model_selection::ModelRole::Decision)
            .ok().and_then(|plan| plan.candidates.into_iter().next()).as_ref() != Some(candidate) {
        bail!("Decision authority changed");
    }
    Ok(json!({"model":answer["model"],"answers":answer["answers"],"usage":answer["usage"],
        "source_kind":"untrusted_model_decision"}))
}

pub async fn execute(app: &App, sid: &str, run_id: &str, args: &Value, cancel: CancellationToken) -> Result<Value> {
    if cancel.is_cancelled() { bail!("Decision cancelled"); }
    let result = tokio::select! {
        _ = cancel.cancelled() => bail!("Decision cancelled"),
        result = tokio::time::timeout(Duration::from_secs(25), work(app, sid, run_id, args)) =>
            result.unwrap_or_else(|_| Err(anyhow::anyhow!("TypeSafe request timed out"))),
    };
    Ok(match result {
        Ok(output) => json!({"status":"success","output":output}),
        Err(error) => json!({"status":"error","output":error.to_string()}),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_shapes_and_assigned_model_are_enforced() {
        let body = validate(r#"{"state":{"task":"chat footer"},"questions":{"file":{"type":"choice","criteria":{"a":"layout","b":"other"}},"score":{"type":"score","criteria":["no","maybe","yes"]},"yes":{"type":"noul","instructions":"Is it UI?"}}}"#, "jev-latest").unwrap();
        assert_eq!(body["model"], "jev-latest");
        assert!(validate(r#"{"state":"x","model":"other","questions":{"q":{"type":"noul"}}}"#, "jev-latest").is_err());
        assert!(validate(r#"{"state":"x","questions":{"q":{"type":"choice","criteria":[]}}}"#, "jev-latest").is_err());
        let answer = json!({"model":"jev-1.13.0","answers":{"file":{"type":"choice","choice":"a","confidence":0.8,"probabilities":{"a":0.8,"b":0.2}},"score":{"type":"score","score":1.5,"confidence":0.7,"legend":{"0":"no","1":"maybe","2":"yes"},"probabilities":{"0":0.1,"1":0.3,"2":0.6}},"yes":{"type":"noul","noul":0.9}},"usage":{"input_tokens":20,"output_tokens":5}});
        validate_answer(&body, &answer).unwrap();
        assert!(validate_answer(&body, &json!({"model":"jev","answers":{"file":{"type":"noul","noul":0.9}},"usage":{}})).is_err());
    }
}
