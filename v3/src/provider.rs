//! Independent ChatGPT subscription transport. Never reads v2 credentials.
use anyhow::{anyhow, bail, Result};
use futures_util::StreamExt;
use serde_json::{json, Value};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

const ISSUER: &str = "https://auth.openai.com";
const CLIENT_ID: &str = "app_EMoamEEZ73f0CkXaXp7hrann";
const RESPONSES: &str = "https://chatgpt.com/backend-api/codex/responses";
const MAX_FRAME: usize = 16 * 1024 * 1024;
const MAX_INSTRUCTIONS: usize = 96 * 1024;
pub const DEFAULT_INSTRUCTIONS: &str = "You are WebAgent, a helpful assistant. Use only the tools made available to you. When a task needs workspace access, use list_abilities to find a suitable ability and load_ability to make its tools available.";

#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderId {
    OpenaiSubscription,
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
        if model.is_empty()
            || model.len() > 128
            || !model
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "-._".contains(c))
        {
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

async fn response_failure(response: reqwest::Response, cancel: &CancellationToken) -> String {
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
        _ = cancel.cancelled() => return "Provider request cancelled".to_owned(),
        result = tokio::time::timeout(Duration::from_secs(5), read) => result.ok().flatten(),
    };
    let detail = match status {
        401 => "Provider authorization is invalid or expired; reconnect ChatGPT",
        403 => "Provider denied access to this account or model",
        429 => "Provider rate or usage limit exceeded",
        _ => value.as_ref().map(failure_detail).unwrap_or("Provider rejected the request (details withheld)"),
    };
    format!("Provider HTTP {status}: {detail}")
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

fn request_body(
    model: &str,
    input: Vec<Value>,
    tools: Vec<Value>,
    session: &str,
    instructions: &str,
) -> Value {
    let mut body = json!({
        "model": model, "input": input, "instructions": instructions,
        "stream": true, "store": false, "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": session, "parallel_tool_calls": false
    });
    if !tools.is_empty() {
        body["tools"] = json!(tools);
        body["tool_choice"] = json!("auto");
    }
    body
}

// Parse bytes rather than UTF-8 chunks: a network boundary may split a character.
#[derive(Default)]
struct Frames {
    buffer: Vec<u8>,
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
            }
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
        if instructions.is_empty() || instructions.len() > MAX_INSTRUCTIONS {
            bail!("Invalid provider instructions");
        }
        if cancel.is_cancelled() {
            bail!("Provider request cancelled");
        }
        let mut request = self
            .client
            .post(self.responses_endpoint())
            .bearer_auth(required(tokens, "access_token")?)
            .header("originator", "webagent")
            .header("session-id", session)
            .header("Accept", "text/event-stream")
            .json(&request_body(model, input, tools, session, instructions));
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
            _ = cancel.cancelled() => bail!("Provider request cancelled"),
            response = tokio::time::timeout(Duration::from_secs(60), request.send()) =>
                response.map_err(|_| anyhow!("Provider connection timed out"))?
                    .map_err(|_| anyhow!("Provider connection failed"))?
        };
        if !response.status().is_success() {
            bail!("{}", response_failure(response, &cancel).await);
        }
        let mut stream = response.bytes_stream();
        let mut frames = Frames::default();
        loop {
            let chunk = tokio::select! {
            biased;
                _ = cancel.cancelled() => bail!("Provider request cancelled"),
                chunk = tokio::time::timeout(Duration::from_secs(120), stream.next()) =>
                    chunk.map_err(|_| anyhow!("Provider stream stalled"))?
            };
            let Some(chunk) = chunk else {
                bail!("Provider stream ended before completion");
            };
            let chunk = chunk.map_err(|_| anyhow!("Provider stream interrupted"))?;
            for event in frames.push(&chunk)? {
                match event["type"].as_str().unwrap_or("") {
                    "response.output_text.delta" => {
                        if let Some(text) = event["delta"].as_str() {
                            tokio::select! {
                            biased;
                                                _ = cancel.cancelled() => bail!("Provider request cancelled"),
                                                sent = delta.send(text.to_owned()) => { sent.map_err(|_| anyhow!("Stream consumer disconnected"))?; }
                                            }
                        }
                    }
                    "response.completed" => {
                        let response = event["response"].clone();
                        if !response["output"].is_array() || response["status"] != "completed" {
                            bail!("Invalid provider completion");
                        }
                        return Ok(response);
                    }
                    "response.failed" | "response.incomplete" | "error" => {
                        bail!("Provider did not complete response: {}", failure_detail(&event))
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
        let body = request_body(
            "test",
            vec![reasoning.clone()],
            vec![json!({"type":"function","name":"read"})],
            "session",
            DEFAULT_INSTRUCTIONS,
        );
        assert_eq!(body["input"][0], reasoning);
        assert_eq!(body["instructions"], DEFAULT_INSTRUCTIONS);
        assert_eq!(body["prompt_cache_key"], "session");
        assert_eq!(body["store"], false);
        assert_eq!(body["include"][0], "reasoning.encrypted_content");
        assert!(body.get("previous_response_id").is_none());
        assert!(body.get("max_output_tokens").is_none());
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
