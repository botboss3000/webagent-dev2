//! Independent ChatGPT subscription transport. Never reads v2 credentials.
use anyhow::{anyhow, bail, Result};
use futures_util::StreamExt;
use serde_json::{json, Value};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

mod deepseek;
mod capabilities;
mod media;

const ISSUER: &str = "https://auth.openai.com";
const CLIENT_ID: &str = "app_EMoamEEZ73f0CkXaXp7hrann";
const RESPONSES: &str = "https://chatgpt.com/backend-api/codex/responses";
const SUBSCRIPTION_MODELS: &str = "https://chatgpt.com/backend-api/codex/models";
// Discovery uses a current Codex client version so newly released models can appear.
const CODEX_MODEL_CLIENT_VERSION: &str = "0.160.1";
const MAX_FRAME: usize = 16 * 1024 * 1024;
const MAX_INSTRUCTIONS: usize = 96 * 1024;
pub const DEFAULT_INSTRUCTIONS: &str = "You are WebAgent, a helpful assistant. Use only the tools made available to you. When asked what abilities or tools are available, check list_abilities; an empty loaded list does not mean the catalog is empty. When a task needs workspace access, search list_abilities using a few task words (or no query for the full permitted catalog), then load_ability to reveal its tools. For development source work, file tools, approved shell and Python commands start in /live-checkout, including its v3 subtree and v2 source. Their reach is determined by container OS permissions; an empty session workspace does not mean source is unavailable. In read-only mode explain that edits require a write-capable mode.";

#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderId {
    OpenaiSubscription,
    Deepseek,
    OpenaiApi,
    Openrouter,
    Abacus,
    Gemini,
    Groq,
    Together,
    Mistral,
    Fireworks,
    Xai,
    Perplexity,
    Deepinfra,
    Typesafe,
}


impl ProviderId {
    /// Installation-owned endpoints only. Client catalog payloads cannot override these.
    pub fn chat_endpoint(self) -> Option<&'static str> {
        match self {
            Self::OpenaiSubscription | Self::Typesafe => None,
            Self::Deepseek => Some("https://api.deepseek.com/chat/completions"),
            Self::OpenaiApi => Some("https://api.openai.com/v1/chat/completions"),
            Self::Openrouter => Some("https://openrouter.ai/api/v1/chat/completions"),
            Self::Abacus => Some("https://routellm.abacus.ai/v1/chat/completions"),
            Self::Gemini => Some("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
            Self::Groq => Some("https://api.groq.com/openai/v1/chat/completions"),
            Self::Together => Some("https://api.together.xyz/v1/chat/completions"),
            Self::Mistral => Some("https://api.mistral.ai/v1/chat/completions"),
            Self::Fireworks => Some("https://api.fireworks.ai/inference/v1/chat/completions"),
            Self::Xai => Some("https://api.x.ai/v1/chat/completions"),
            Self::Perplexity => Some("https://api.perplexity.ai/chat/completions"),
            Self::Deepinfra => Some("https://api.deepinfra.com/v1/openai/chat/completions"),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ProviderSelection {
    pub provider: ProviderId,
    pub model: String,
}

impl ProviderSelection {
    /// Model-only legacy settings select the original subscription adapter.
    /// Explicit invalid provider values must never silently select a transport.
    pub fn parse(value: &Value, default_model: &str) -> Result<Self> {
        if !value.is_object() {
            bail!("Invalid provider selection");
        }
        let provider = match value.get("provider") {
            None => ProviderId::OpenaiSubscription,
            Some(value) => serde_json::from_value(value.clone())
                .map_err(|_| anyhow!("Unsupported provider identifier"))?,
        };
        let model = match value.get("model") {
            None => default_model,
            Some(value) => value.as_str().ok_or_else(|| anyhow!("Invalid model"))?,
        };
        if !crate::platform_catalog::valid_model(model) {
            bail!("Invalid model");
        }
        Ok(Self {
            provider,
            model: model.to_owned(),
        })
    }

    pub fn value(&self) -> Value {
        serde_json::to_value(self).expect("Provider selection serialization")
    }
}

/// Sanitized transport error; no provider payload, token or user input escapes.
#[derive(Debug)]
pub struct ProviderFailure {
    pub kind: crate::model_selection::FailureKind,
    pub response_started: bool,
    detail: String,
    pub transport: Option<TransportDiagnostic>,
    pub http_status: Option<u16>,
}
impl std::fmt::Display for ProviderFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str(&self.detail) }
}
impl std::error::Error for ProviderFailure {}
pub(crate) fn typed_failure(kind: crate::model_selection::FailureKind, started: bool, detail: impl Into<String>) -> anyhow::Error {
    ProviderFailure { kind, response_started: started, detail: detail.into(), transport: None, http_status: None }.into()
}
/// Allowlisted transport facts, never reqwest Display/Debug (which may contain URLs).
#[derive(Clone, Debug, serde::Serialize)]
pub struct TransportDiagnostic {
    pub timeout: bool, pub connect: bool, pub body: bool, pub decode: bool,
    pub request: bool, pub status: Option<u16>, pub io_kind: Option<String>,
}
fn transport_failure(error:reqwest::Error,started:bool,detail:&str)->anyhow::Error {
    use std::error::Error;
    let mut source=error.source(); let mut io_kind=None;
    while let Some(cause)=source {
        if let Some(io)=cause.downcast_ref::<std::io::Error>() {io_kind=Some(format!("{:?}",io.kind()));break;}
        source=cause.source();
    }
    ProviderFailure {kind:crate::model_selection::FailureKind::Transient,response_started:started,
        detail:detail.into(),http_status:error.status().map(|s|s.as_u16()),transport:Some(TransportDiagnostic {timeout:error.is_timeout(),connect:error.is_connect(),
        body:error.is_body(),decode:error.is_decode(),request:error.is_request(),status:error.status().map(|s|s.as_u16()),io_kind})}.into()
}
fn failure_kind(value: &Value) -> crate::model_selection::FailureKind {
    use crate::model_selection::FailureKind::*;
    match failure_detail(value) {
        "Selected model is unavailable or unsupported for this account" => ModelUnavailable,
        "Provider authorization is invalid or expired; reconnect ChatGPT" => InvalidCredentials,
        "Provider rate limit exceeded" | "Provider internal error" => Transient,
        "Provider usage limit exceeded" => QuotaExhausted,
        "Provider context limit exceeded" | "Provider rejected the request format or parameters" => MalformedRequest,
        _ => Protocol,
    }
}
#[derive(Clone)]
pub struct Provider {
    client: reqwest::Client,
    #[cfg(test)]
    responses_endpoint: Option<String>,
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn required<'a>(value: &'a Value, name: &str) -> Result<&'a str> {
    value
        .get(name)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| anyhow!("Provider authorization missing required field"))
}

// Only fixed diagnostic labels leave the transport: provider bodies can echo
// credentials, account identifiers, or user input. Never persist their text.
fn failure_detail(value: &Value) -> &'static str {
    let error = value.get("error")
        .or_else(|| value.get("response").and_then(|r| r.get("error")))
        .unwrap_or(value);
    let code = error["code"].as_str().unwrap_or("");
    let message = error["message"].as_str().unwrap_or("").to_ascii_lowercase();
    if code == "model_not_found"
        || (message.contains("model") && (message.contains("not supported")
            || message.contains("unsupported") || message.contains("does not exist")
            || message.contains("not available"))) {
        "Selected model is unavailable or unsupported for this account"
    } else if matches!(code, "invalid_api_key" | "token_expired" | "invalid_token")
        || message.contains("token expired") || message.contains("token has expired") {
        "Provider authorization is invalid or expired; reconnect ChatGPT"
    } else if code == "rate_limit_exceeded" || message.contains("rate limit") {
        "Provider rate limit exceeded"
    } else if code == "insufficient_quota" || message.contains("usage limit") {
        "Provider usage limit exceeded"
    } else if code == "context_length_exceeded" || message.contains("context length") {
        "Provider context limit exceeded"
    } else if code == "invalid_request_error" || message.contains("unsupported parameter") {
        "Provider rejected the request format or parameters"
    } else if code == "server_error" {
        "Provider internal error"
    } else if value["response"]["incomplete_details"]["reason"] == "max_output_tokens" {
        "Provider output limit reached"
    } else {
        "Provider rejected or did not complete the request (details withheld)"
    }
}

async fn response_failure(response: reqwest::Response, cancel: &CancellationToken) -> anyhow::Error {
    let status = response.status().as_u16();
    // Bound time and bytes even when an error endpoint sends an endless body.
    let mut stream = response.bytes_stream();
    let read = async {
        let mut body = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.ok()?;
            if body.len() + chunk.len() > 16 * 1024 { return None; }
            body.extend_from_slice(&chunk);
        }
        serde_json::from_slice::<Value>(&body).ok()
    };
    let value = tokio::select! {
        biased;
        _ = cancel.cancelled() => return typed_failure(crate::model_selection::FailureKind::Cancelled, false, "Provider request cancelled"),
        result = tokio::time::timeout(Duration::from_secs(5), read) => result.ok().flatten(),
    };
    let detail = match status {
        401 => "Provider authorization is invalid or expired; reconnect ChatGPT",
        403 => "Provider denied access to this account or model",
        429 => "Provider rate or usage limit exceeded",
        _ => value.as_ref().map(failure_detail).unwrap_or("Provider rejected the request (details withheld)"),
    };
    use crate::model_selection::FailureKind::*;
    let kind = match status {
        401 => InvalidCredentials,
        403 => PolicyRejected,
        429 if value.as_ref().is_some_and(|v| failure_kind(v) == QuotaExhausted) => QuotaExhausted,
        408 | 429 | 500..=599 => Transient,
        _ => value.as_ref().map(failure_kind).unwrap_or(MalformedRequest),
    };
    ProviderFailure {kind,response_started:false,detail:format!("Provider HTTP {status}: {detail}"),
        transport:None,http_status:Some(status)}.into()
}

async fn checked_json(response: reqwest::Response) -> Result<Value> {
    if !response.status().is_success() {
        bail!("Provider authorization HTTP {}", response.status().as_u16());
    }
    let value: Value = response
        .json()
        .await
        .map_err(|_| anyhow!("Invalid provider authorization response"))?;
    if !value.is_object() {
        bail!("Invalid provider authorization response");
    }
    Ok(value)
}

fn subscription_model_list(value: &Value) -> Result<Value> {
    let models = value["models"].as_array()
        .ok_or_else(|| anyhow!("Invalid subscription model catalog"))?;
    let visible: Vec<Value> = models.iter().filter_map(|model| {
        if model["visibility"] != "list" { return None; }
        let id = model["slug"].as_str()?;
        if id.is_empty() || id.len() > 200 || !id.bytes().all(|b| b.is_ascii_graphic()) {
            return None;
        }
        let name = model["display_name"].as_str().filter(|s| !s.is_empty()).unwrap_or(id);
        // Subscription chat models retain the v2 Vision fallback when upstream omits modalities.
        let mut row = model.clone();
        row["name"] = json!(name);
        let fallback = json!({"input_modalities":["text","image"],"output_modalities":["text"]});
        Some(capabilities::normalize(id,&row,&fallback))
    }).collect();
    Ok(json!({"models":visible}))
}

#[cfg(test)]
#[test]
fn subscription_catalog_only_lists_visible_models_in_server_order() {
    let catalog=json!({"models":[{"slug":"new-gpt","display_name":"New GPT","visibility":"list"},
        {"slug":"hidden-gpt","visibility":"hide"},{"slug":"older-gpt","visibility":"list"}]});
    let result = subscription_model_list(&catalog).unwrap();
    assert_eq!(result["models"][0]["id"],"new-gpt");
    assert_eq!(result["models"][1]["id"],"older-gpt");
    assert_eq!(result["models"].as_array().unwrap().len(),2);
    assert_eq!(result["models"][0]["image_capable"],true);
    assert_eq!(result["models"][0]["image_out_capable"],false);
    assert!(subscription_model_list(&json!({})).is_err());
}

// JWT claims are metadata only; authentication is performed by the provider.
fn claims(token: &str) -> Value {
    let Some(encoded) = token.split('.').nth(1) else {
        return json!({});
    };
    let mut bytes = Vec::new();
    let mut bits: u32 = 0;
    let mut count = 0;
    for ch in encoded.bytes() {
        let n = match ch {
            b'A'..=b'Z' => ch - b'A',
            b'a'..=b'z' => ch - b'a' + 26,
            b'0'..=b'9' => ch - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            b'=' => break,
            _ => return json!({}),
        };
        bits = (bits << 6) | u32::from(n);
        count += 6;
        if count >= 8 {
            count -= 8;
            bytes.push((bits >> count) as u8);
            bits &= (1 << count) - 1;
        }
    }
    serde_json::from_slice(&bytes).unwrap_or_else(|_| json!({}))
}

fn decorate(mut tokens: Value) -> Result<Value> {
    required(&tokens, "access_token")?;
    let mut payload = claims(tokens.get("id_token").and_then(Value::as_str).unwrap_or(""));
    if !payload.is_object() || payload.as_object().is_some_and(|o| o.is_empty()) {
        payload = claims(required(&tokens, "access_token")?);
    }
    let auth = &payload["https://api.openai.com/auth"];
    for (key, claim) in [
        ("account_id", "chatgpt_account_id"),
        ("residency", "chatgpt_compute_residency"),
        ("plan_type", "chatgpt_plan_type"),
    ] {
        let value = payload[claim]
            .as_str()
            .or_else(|| auth[claim].as_str())
            .or_else(|| {
                if key == "account_id" {
                    payload["organizations"][0]["id"].as_str()
                } else {
                    None
                }
            });
        if let Some(value) = value.filter(|v| !v.is_empty() && *v != "no_constraint") {
            tokens[key] = json!(value);
        }
    }
    if let Some(email) = payload["email"].as_str() {
        tokens["email"] = json!(email);
    }
    tokens["expires_at"] = json!(now() + tokens["expires_in"].as_u64().unwrap_or(3600));
    Ok(tokens)
}

// PROVIDER-BORROWED-BODY: serialize borrowed dispatch data directly into the
// HTTP body, avoiding an additional owned Value tree of history and schemas.
// Wire fields remain identical to the stateless Responses contract.
#[derive(serde::Serialize)]
struct RequestBody<'a> {
    model: &'a str,
    input: &'a [Value],
    instructions: &'a str,
    stream: bool,
    store: bool,
    include: [&'static str; 1],
    prompt_cache_key: &'a str,
    parallel_tool_calls: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    tools: Option<&'a [Value]>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_choice: Option<&'static str>,
}
fn request_body<'a>(model: &'a str, input: &'a [Value], tools: &'a [Value], session: &'a str, instructions: &'a str) -> RequestBody<'a> {
    RequestBody { model, input, instructions, stream: true, store: false,
        include: ["reasoning.encrypted_content"], prompt_cache_key: session,
        parallel_tool_calls: true, tools: (!tools.is_empty()).then_some(tools),
        tool_choice: (!tools.is_empty()).then_some("auto") }
}

// Parse bytes rather than UTF-8 chunks: a network boundary may split a character.
#[derive(Default)]
struct Frames {
    buffer: Vec<u8>,
    done: bool,
}
impl Frames {
    fn push(&mut self, chunk: &[u8]) -> Result<Vec<Value>> {
        self.buffer.extend_from_slice(chunk);
        if self.buffer.len() > MAX_FRAME {
            bail!("Provider stream frame exceeded limit");
        }
        let mut values = Vec::new();
        loop {
            let delimiter = self
                .buffer
                .windows(2)
                .position(|w| w == b"\n\n")
                .map(|p| (p, 2))
                .or_else(|| {
                    self.buffer
                        .windows(4)
                        .position(|w| w == b"\r\n\r\n")
                        .map(|p| (p, 4))
                });
            let Some((end, width)) = delimiter else {
                break;
            };
            let frame = self.buffer.drain(..end + width).collect::<Vec<_>>();
            let frame = std::str::from_utf8(&frame)
                .map_err(|_| anyhow!("Invalid provider stream encoding"))?;
            let data = frame
                .lines()
                .filter_map(|l| l.strip_prefix("data:").map(str::trim_start))
                .collect::<Vec<_>>()
                .join("\n");
            if data == "[DONE]" {self.done=true;}
            if data.is_empty() || data == "[DONE]" {
                continue;
            }
            values.push(
                serde_json::from_str(&data)
                    .map_err(|_| anyhow!("Invalid provider stream event"))?,
            );
        }
        Ok(values)
    }
}

impl Default for Provider {
    fn default() -> Self {
        Self::new()
    }
}
impl Provider {
    pub fn new() -> Self {
        Self {
            client: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(20))
                .user_agent("webagent-v3/0.1")
                .build()
                .expect("HTTP client initialization"),
            #[cfg(test)]
            responses_endpoint: None,
        }
    }

    #[cfg(test)]
    pub fn for_test(endpoint: String) -> Self {
        let mut provider = Self::new();
        provider.responses_endpoint = Some(endpoint);
        provider
    }

    fn responses_endpoint(&self) -> &str {
        #[cfg(test)]
        if let Some(endpoint) = self.responses_endpoint.as_deref() {
            return endpoint;
        }
        RESPONSES
    }

    pub async fn device_start(&self) -> Result<Value> {
        let response = self
            .client
            .post(format!("{ISSUER}/api/accounts/deviceauth/usercode"))
            .timeout(Duration::from_secs(30))
            .json(&json!({"client_id": CLIENT_ID}))
            .send()
            .await
            .map_err(|_| anyhow!("Provider authorization connection failed"))?;
        let mut device = checked_json(response).await?;
        required(&device, "device_auth_id")?;
        required(&device, "user_code")?;
        device["verification_url"] = json!(format!("{ISSUER}/codex/device"));
        device["expires_at"] = json!(now() + 900);
        device["expires_in_seconds"] = json!(900);
        let interval = device["interval"]
            .as_u64()
            .or_else(|| device["interval"].as_str().and_then(|v| v.parse().ok()))
            .unwrap_or(5);
        device["interval"] = json!(interval.max(1) + 3);
        Ok(device)
    }

    pub async fn device_poll(&self, device: &Value) -> Result<Option<Value>> {
        if device["expires_at"].as_u64().unwrap_or(0) <= now() {
            bail!("Device authorization expired");
        }
        let response = self.client.post(format!("{ISSUER}/api/accounts/deviceauth/token"))
            .timeout(Duration::from_secs(30))
            .json(&json!({"device_auth_id": required(device, "device_auth_id")?, "user_code": required(device, "user_code")?}))
            .send().await.map_err(|_| anyhow!("Provider authorization connection failed"))?;
        if matches!(response.status().as_u16(), 403 | 404) {
            return Ok(None);
        }
        let authorization = checked_json(response).await?;
        let tokens = self
            .token_form(&[
                ("grant_type", "authorization_code"),
                ("code", required(&authorization, "authorization_code")?),
                (
                    "redirect_uri",
                    "https://auth.openai.com/deviceauth/callback",
                ),
                ("client_id", CLIENT_ID),
                ("code_verifier", required(&authorization, "code_verifier")?),
            ])
            .await?;
        Ok(Some(decorate(tokens)?))
    }

    async fn token_form(&self, fields: &[(&str, &str)]) -> Result<Value> {
        checked_json(
            self.client
                .post(format!("{ISSUER}/oauth/token"))
                .timeout(Duration::from_secs(30))
                .form(fields)
                .send()
                .await
                .map_err(|_| anyhow!("Provider authorization connection failed"))?,
        )
        .await
    }

    pub async fn refresh(&self, tokens: &Value) -> Result<Value> {
        let fresh = self
            .token_form(&[
                ("grant_type", "refresh_token"),
                ("refresh_token", required(tokens, "refresh_token")?),
                ("client_id", CLIENT_ID),
            ])
            .await?;
        let mut merged = tokens.clone();
        for (key, value) in fresh
            .as_object()
            .ok_or_else(|| anyhow!("Invalid provider token response"))?
        {
            merged[key] = value.clone();
        }
        decorate(merged)
    }

    /// Query the account-scoped Codex catalog used by the subscription transport.
    /// Never return a token or provider error body to the UI.
    pub async fn subscription_models(&self, tokens: &Value) -> Result<Value> {
        let mut request = self.client.get(SUBSCRIPTION_MODELS)
            .query(&[("client_version", CODEX_MODEL_CLIENT_VERSION)])
            .bearer_auth(required(tokens, "access_token")?)
            .header("originator", "webagent")
            .timeout(Duration::from_secs(20));
        if let Some(account_id) = tokens["account_id"].as_str().filter(|id| !id.is_empty()) {
            request = request.header("ChatGPT-Account-Id", account_id);
        }
        let response = request.send().await.map_err(|_| anyhow!("Subscription model catalog unavailable"))?;
        if !response.status().is_success() {
            bail!("Subscription model catalog unavailable");
        }
        let mut stream = response.bytes_stream();
        let mut body = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| anyhow!("Subscription model catalog unavailable"))?;
            if body.len() + chunk.len() > 2 * 1024 * 1024 {
                bail!("Subscription model catalog too large");
            }
            body.extend_from_slice(&chunk);
        }
        let value: Value = serde_json::from_slice(&body)
            .map_err(|_| anyhow!("Invalid subscription model catalog"))?;
        subscription_model_list(&value)
    }

    /// Typed dispatch boundary; every supported transport must be explicit here.
    pub async fn complete_selected(
        &self,
        selection: &ProviderSelection,
        tokens: &Value,
        input: Vec<Value>,
        tools: Vec<Value>,
        session: &str,
        delta: mpsc::Sender<String>,
        cancel: CancellationToken,
    ) -> Result<Value> {
        self.complete_selected_with_instructions(
            selection,
            tokens,
            input,
            tools,
            session,
            DEFAULT_INSTRUCTIONS,
            delta,
            cancel,
        )
        .await
    }

    pub async fn complete_selected_with_instructions(
        &self,
        selection: &ProviderSelection,
        tokens: &Value,
        input: Vec<Value>,
        tools: Vec<Value>,
        session: &str,
        instructions: &str,
        delta: mpsc::Sender<String>,
        cancel: CancellationToken,
    ) -> Result<Value> {
        match selection.provider {
            ProviderId::OpenaiSubscription => {
                self.complete_with_instructions(
                    tokens,
                    &selection.model,
                    input,
                    tools,
                    session,
                    instructions,
                    delta,
                    cancel,
                )
                .await
            },
            provider => self.complete_compatible(provider, tokens, &selection.model, input, tools, instructions, delta, cancel).await,
        }
    }

    pub async fn complete(
        &self,
        tokens: &Value,
        model: &str,
        input: Vec<Value>,
        tools: Vec<Value>,
        session: &str,
        delta: mpsc::Sender<String>,
        cancel: CancellationToken,
    ) -> Result<Value> {
        self.complete_with_instructions(
            tokens,
            model,
            input,
            tools,
            session,
            DEFAULT_INSTRUCTIONS,
            delta,
            cancel,
        )
        .await
    }

    async fn complete_with_instructions(
        &self,
        tokens: &Value,
        model: &str,
        input: Vec<Value>,
        tools: Vec<Value>,
        session: &str,
        instructions: &str,
        delta: mpsc::Sender<String>,
        cancel: CancellationToken,
    ) -> Result<Value> {
        let mut response_started = false;
        if instructions.is_empty() || instructions.len() > MAX_INSTRUCTIONS {
            return Err(typed_failure(crate::model_selection::FailureKind::MalformedRequest, false, "Invalid provider instructions"));
        }
        if cancel.is_cancelled() {
            return Err(typed_failure(crate::model_selection::FailureKind::Cancelled, response_started, "Provider request cancelled"));
        }
        let mut request = self
            .client
            .post(self.responses_endpoint())
            .bearer_auth(required(tokens, "access_token")?)
            .header("originator", "webagent")
            .header("session-id", session)
            .header("Accept", "text/event-stream")
            .json(&request_body(model, &input, &tools, session, instructions));
        for (key, header) in [
            ("account_id", "ChatGPT-Account-Id"),
            ("residency", "x-openai-internal-codex-residency"),
        ] {
            if let Some(value) = tokens[key].as_str().filter(|v| !v.is_empty()) {
                request = request.header(header, value);
            }
        }
        let response = tokio::select! {
            biased;
            _ = cancel.cancelled() => return Err(typed_failure(crate::model_selection::FailureKind::Cancelled, response_started, "Provider request cancelled")),
            response = tokio::time::timeout(Duration::from_secs(60), request.send()) =>
                response.map_err(|_| typed_failure(crate::model_selection::FailureKind::Transient, false, "Provider connection timed out"))?
                    .map_err(|e| transport_failure(e, false, "Provider connection failed"))?
        };
        if !response.status().is_success() {
            return Err(response_failure(response, &cancel).await);
        }
        let mut stream = response.bytes_stream();
        let mut frames = Frames::default();
        let mut completed_items = Vec::<Value>::new();
        // STREAM-RETENTION-BOUND: frame limits alone do not bound a sequence of
        // completed items. Cap retained fallback output and diagnostic cardinality.
        let mut completed_bytes = 0usize;
        let mut event_kinds = std::collections::BTreeMap::<String, usize>::new();
        loop {
            let chunk = tokio::select! {
            biased;
                _ = cancel.cancelled() => return Err(typed_failure(crate::model_selection::FailureKind::Cancelled, response_started, "Provider request cancelled")),
                chunk = tokio::time::timeout(Duration::from_secs(120), stream.next()) =>
                    chunk.map_err(|_| typed_failure(crate::model_selection::FailureKind::Transient, response_started, "Provider stream stalled"))?
            };
            let Some(chunk) = chunk else {
                return Err(typed_failure(crate::model_selection::FailureKind::Transient, response_started, "Provider stream ended before completion"));
            };
            let chunk = chunk.map_err(|e| transport_failure(e, response_started, "Provider stream interrupted"))?;
            for event in frames.push(&chunk).map_err(|e| typed_failure(crate::model_selection::FailureKind::Protocol, response_started, e.to_string()))? {
                let event_type = event["type"].as_str().unwrap_or("unknown");
                if event_type.len() <= 128 && (event_kinds.contains_key(event_type) || event_kinds.len() < 64) {
                    *event_kinds.entry(event_type.to_owned()).or_default() += 1;
                }
                if event["type"].as_str().is_some_and(|t| t.starts_with("response.")
                    && !matches!(t, "response.failed" | "response.incomplete")) {
                    response_started = true;
                }
                match event["type"].as_str().unwrap_or("") {
                    "response.output_text.delta" => {
                        if let Some(text) = event["delta"].as_str() {
                            tokio::select! {
                            biased;
                                                _ = cancel.cancelled() => return Err(typed_failure(crate::model_selection::FailureKind::Cancelled, response_started, "Provider request cancelled")),
                                                sent = delta.send(text.to_owned()) => { sent.map_err(|_| typed_failure(crate::model_selection::FailureKind::Protocol, response_started, "Stream consumer disconnected"))?; }
                                            }
                        }
                    }
                    "response.output_item.done" => {
                        if let Some(item) = event.get("item").filter(|item| item.is_object()) {
                            completed_bytes = completed_bytes.saturating_add(crate::context_accounting::json_size(item)?);
                            if completed_items.len() >= 4096 || completed_bytes > MAX_FRAME {
                                return Err(typed_failure(crate::model_selection::FailureKind::Protocol, response_started, "Provider retained output exceeded limit"));
                            }
                            completed_items.push(item.clone());
                        }
                    }
                    "response.completed" => {
                        let mut response = event["response"].clone();
                        if !response["output"].is_array() || response["status"] != "completed" {
                            return Err(typed_failure(crate::model_selection::FailureKind::Protocol, response_started, "Invalid provider completion"));
                        }
                        if response["output"].as_array().is_some_and(Vec::is_empty) && !completed_items.is_empty() {
                            response["output"] = json!(completed_items);
                        }
                        if response["output"].as_array().is_some_and(Vec::is_empty) {
                            tracing::warn!(events=?event_kinds, "Provider completed with empty output");
                        }
                        return Ok(response);
                    }
                    "response.failed" | "response.incomplete" | "error" => {
                        return Err(typed_failure(failure_kind(&event), response_started, format!("Provider did not complete response: {}", failure_detail(&event))))
                    }
                    _ => {}
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn provider_failures_use_fixed_labels_without_echoing_secrets() {
        assert_eq!(failure_detail(&json!({"error":{"code":"model_not_found","message":"secret account model"}})),
            "Selected model is unavailable or unsupported for this account");
        assert_eq!(failure_detail(&json!({"response":{"error":{"code":"rate_limit_exceeded"}}})),
            "Provider rate limit exceeded");
        let label = failure_detail(&json!({"error":{"message":"Bearer secret-token user@example.com"}}));
        assert!(!label.contains("secret-token"));
        assert!(!label.contains("user@example.com"));
        assert_eq!(failure_detail(&json!({"response":{"incomplete_details":{"reason":"max_output_tokens"}}})),
            "Provider output limit reached");
    }

    #[tokio::test]
    async fn http_failure_reports_status_and_safe_category() {
        use axum::{http::StatusCode, routing::post, Json, Router};
        let app = Router::new().route("/responses", post(|| async {
            (StatusCode::BAD_REQUEST, Json(json!({"error":{"code":"model_not_found","message":"private-token"}})))
        }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap(); });
        let provider = Provider::for_test(format!("http://{address}/responses"));
        let (sender, _receiver) = mpsc::channel(1);
        let error = provider.complete(&json!({"access_token":"test-only"}), "fixture", vec![], vec![],
            "session", sender, CancellationToken::new()).await.unwrap_err().to_string();
        assert!(error.contains("HTTP 400"));
        assert!(error.contains("model is unavailable"));
        assert!(!error.contains("private-token"));
        server.abort();
    }


    #[test]
    fn split_unicode_and_crlf_preserve_events() {
        let bytes =
            "data: {\"type\":\"response.output_text.delta\",\"delta\":\"é\"}\r\n\r\n".as_bytes();
        let mut frames = Frames::default();
        let mut values = Vec::new();
        for byte in bytes {
            values.extend(frames.push(&[*byte]).unwrap());
        }
        assert_eq!(values.len(), 1);
        assert_eq!(values[0]["delta"], "é");
    }
    #[test]
    fn stateless_request_keeps_reasoning_and_tools() {
        let reasoning = json!({"type":"reasoning","encrypted_content":"opaque"});
        let input = vec![reasoning.clone()];
        let tools = vec![json!({"type":"function","name":"read"})];
        let body = serde_json::to_value(request_body(
            "test", &input, &tools, "session", DEFAULT_INSTRUCTIONS,
        )).unwrap();
        assert_eq!(body["input"][0], reasoning);
        assert_eq!(body["instructions"], DEFAULT_INSTRUCTIONS);
        assert_eq!(body["prompt_cache_key"], "session");
        assert_eq!(body["store"], false);
        assert_eq!(body["include"][0], "reasoning.encrypted_content");
        assert!(body.get("previous_response_id").is_none());
        assert!(body.get("max_output_tokens").is_none());
    }
    // PROVIDER-WIRE-PARITY: borrowed serialization must preserve omission rules
    // and exact values, including escaped Unicode and complete tool arguments.
    #[test]
    fn borrowed_request_matches_owned_wire_contract() {
        for tools in [vec![], vec![json!({"type":"function","name":"read","parameters":{"type":"object"}})]] {
            let input = vec![json!({"role":"user","content":"é\n\"text\""}),
                json!({"type":"function_call","call_id":"c","name":"read","arguments":"{\"path\":\"a\"}"})];
            let mut expected = json!({"model":"test","input":input,"instructions":"guide",
                "stream":true,"store":false,"include":["reasoning.encrypted_content"],
                "prompt_cache_key":"session","parallel_tool_calls":true});
            if !tools.is_empty() { expected["tools"] = json!(tools); expected["tool_choice"] = json!("auto"); }
            let actual: Value = serde_json::from_slice(&serde_json::to_vec(
                &request_body("test", &input, &tools, "session", "guide")).unwrap()).unwrap();
            assert_eq!(actual, expected);
        }
    }
    #[test]
    fn stream_bounds_and_invalid_events_fail() {
        assert!(Frames::default().push(b"data: broken\n\n").is_err());
        assert!(Frames::default().push(&vec![b'x'; MAX_FRAME + 1]).is_err());
    }
    #[test]
    fn jwt_metadata_decode() {
        assert_eq!(
            claims("a.eyJlbWFpbCI6ImFAZXhhbXBsZS5jb20ifQ.b")["email"],
            "a@example.com"
        );
        assert_eq!(claims("malformed"), json!({}));
    }

    #[tokio::test]
    async fn subscription_headers_and_completed_output_are_preserved() {
        use axum::{extract::State, http::HeaderMap, routing::post, Json, Router};
        use std::sync::Arc;
        use tokio::sync::Mutex;
        let captured = Arc::new(Mutex::new(None::<(HeaderMap, Value)>));
        let state = captured.clone();
        let app = Router::new().route("/responses", post(
            |State(state): State<Arc<Mutex<Option<(HeaderMap, Value)>>>>, headers: HeaderMap, Json(body): Json<Value>| async move {
                *state.lock().await = Some((headers, body));
                ([("content-type", "text/event-stream")], "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"output\":[{\"type\":\"reasoning\",\"encrypted_content\":\"opaque\"},{\"type\":\"function_call\",\"call_id\":\"call\",\"name\":\"read\",\"arguments\":\"{}\"}]}}\n\n")
            }
        )).with_state(state);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let provider = Provider::for_test(format!("http://{address}/responses"));
        let (sender, _receiver) = mpsc::channel(1);
        let selection = ProviderSelection::parse(&json!({"model":"fixture"}), "unused").unwrap();
        let output = provider
            .complete_selected_with_instructions(
                &selection,
                &json!({"access_token":"test-only","account_id":"account","residency":"eu"}),
                vec![json!({"role":"user","content":"hello"})],
                vec![],
                "session",
                "Pinned instructions for this run.",
                sender,
                CancellationToken::new(),
            )
            .await
            .unwrap();
        assert_eq!(output["output"][0]["encrypted_content"], "opaque");
        assert_eq!(output["output"][1]["call_id"], "call");
        let captured = captured.lock().await;
        let (headers, body) = captured.as_ref().unwrap();
        assert_eq!(headers["authorization"], "Bearer test-only");
        assert_eq!(headers["originator"], "webagent");
        assert_eq!(headers["session-id"], "session");
        assert_eq!(headers["chatgpt-account-id"], "account");
        assert_eq!(headers["x-openai-internal-codex-residency"], "eu");
        assert_eq!(body["store"], false);
        assert_eq!(body["instructions"], "Pinned instructions for this run.");
        assert_eq!(body["prompt_cache_key"], "session");
        assert_eq!(body["include"][0], "reasoning.encrypted_content");
        assert!(body.get("max_output_tokens").is_none());
        server.abort();
    }

    #[tokio::test]
    async fn streamed_output_items_fill_empty_completion() {
        use axum::{routing::post, Router};
        let app = Router::new().route("/responses", post(|| async {
            ([ ("content-type", "text/event-stream") ],
                "data: {\"type\":\"response.output_item.done\",\"item\":{\"type\":\"message\",\"content\":[{\"type\":\"output_text\",\"text\":\"hello\"}]}}\n\ndata: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"output\":[]}}\n\n")
        }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap(); });
        let provider = Provider::for_test(format!("http://{address}/responses"));
        let (sender, _receiver) = mpsc::channel(1);
        let output = provider.complete(&json!({"access_token":"test-only"}), "fixture",
            vec![], vec![], "session", sender, CancellationToken::new()).await.unwrap();
        assert_eq!(output["output"][0]["content"][0]["text"], "hello");
        server.abort();
    }

    #[tokio::test]
    async fn already_cancelled_request_never_connects() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let provider = Provider::for_test(format!(
            "http://{}/responses",
            listener.local_addr().unwrap()
        ));
        let cancel = CancellationToken::new();
        cancel.cancel();
        let (sender, _receiver) = mpsc::channel(1);
        let result = provider
            .complete(
                &json!({"access_token":"test-only"}),
                "fixture",
                vec![],
                vec![],
                "session",
                sender,
                cancel,
            )
            .await;
        assert!(result.is_err());
        assert!(
            tokio::time::timeout(Duration::from_millis(30), listener.accept())
                .await
                .is_err()
        );
    }

    #[test]
    fn legacy_selection_defaults_only_missing_provider() {
        let selection =
            ProviderSelection::parse(&json!({"model":"legacy-model"}), "default").unwrap();
        assert_eq!(selection.provider, ProviderId::OpenaiSubscription);
        assert_eq!(
            selection.value(),
            json!({"provider":"openai_subscription","model":"legacy-model"})
        );
        assert_eq!(
            ProviderSelection::parse(&json!({}), "default")
                .unwrap()
                .model,
            "default"
        );
        for provider in [json!("other"), json!(null), json!(7), json!("")] {
            assert!(ProviderSelection::parse(
                &json!({"provider":provider,"model":"valid"}),
                "default"
            )
            .is_err());
        }
    }
}

#[cfg(test)]
mod upstream_id_tests {
    #[test]
    fn selection_accepts_namespaced_models_without_accepting_controls() {
        use super::ProviderSelection;
        use serde_json::json;
        assert!(ProviderSelection::parse(&json!({"provider":"deepseek","model":"organization/model-name"}), "default").is_ok());
        for model in ["model\n", "model name", "model?token=x"] {
            assert!(ProviderSelection::parse(&json!({"provider":"deepseek","model":model}), "default").is_err());
        }
    }
}
