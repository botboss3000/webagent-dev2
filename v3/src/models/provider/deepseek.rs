//! Shared fixed-endpoint OpenAI-compatible transport, including DeepSeek.
use super::*;
use crate::model_selection::FailureKind;
use std::collections::BTreeMap;

fn text(value: &Value) -> String {
    if let Some(s) = value.as_str() { return s.to_owned(); }
    value.as_array().map(|items| items.iter().filter_map(|v| v["text"].as_str()).collect::<Vec<_>>().join("")).unwrap_or_default()
}
fn message_content(value:&Value)->Result<Value>{
    let Some(parts)=value.as_array() else{return Ok(json!(text(value)))};
    if !parts.iter().any(|part|part["type"]=="input_image"){return Ok(json!(text(value)));}
    parts.iter().map(|part|match part["type"].as_str(){
        Some("input_text"|"text"|"output_text")=>Ok(json!({"type":"text","text":part["text"]})),
        Some("input_image")=>Ok(json!({"type":"image_url","image_url":{"url":required(part,"image_url")?}})),
        _=>bail!("Unsupported media part")
    }).collect::<Result<Vec<_>>>().map(|parts|json!(parts))
}
fn request_body(model: &str, input: Vec<Value>, tools: Vec<Value>, instructions: &str) -> Result<Value> {
    let mut messages = vec![json!({"role":"system","content":instructions})];
    for item in input {
        match item["type"].as_str().unwrap_or("message") {
            "reasoning" => {},
            "function_call" => {
                let call = json!({"id":required(&item,"call_id")?,"type":"function","function":{
                    "name":required(&item,"name")?,"arguments":required(&item,"arguments")?}});
                if messages.last().is_some_and(|v| v["role"] == "assistant") {
                    let last = messages.last_mut().unwrap();
                    if !last["tool_calls"].is_array() { last["tool_calls"] = json!([]); }
                    last["tool_calls"].as_array_mut().unwrap().push(call);
                } else {
                    let mut message = json!({"role":"assistant","content":null,"tool_calls":[call]});
                    if let Some(reasoning) = item.get("deepseek_reasoning") { message["reasoning_content"] = reasoning.clone(); }
                    messages.push(message);
                }
            },
            "function_call_output" => messages.push(json!({"role":"tool","tool_call_id":required(&item,"call_id")?,"content":text(&item["output"])})),
            "message" => {
                let role = item["role"].as_str().unwrap_or("user");
                if !matches!(role,"user"|"assistant"|"system"|"developer") { bail!("Invalid Provider message role"); }
                let mut message = json!({"role":if role=="developer" {"system"} else {role},"content":message_content(&item["content"])?});
                if let Some(reasoning) = item.get("deepseek_reasoning") { message["reasoning_content"] = reasoning.clone(); }
                messages.push(message);
            },
            _ => bail!("Unsupported Provider input item"),
        }
    }
    let mut body = json!({"model":model,"messages":messages,"stream":true,"stream_options":{"include_usage":true},"max_tokens":8192});
    if !tools.is_empty() {
        body["tools"] = json!(tools.into_iter().map(|t| json!({"type":"function","function":{
            "name":t["name"],"description":t["description"],"parameters":t["parameters"]}})).collect::<Vec<_>>());
        body["tool_choice"] = json!("auto");
    }
    Ok(body)
}

#[derive(Default)]
struct Completion { content: String, reasoning: String, calls: BTreeMap<u64,Value>, usage:Value, finished:bool }
impl Completion {
    fn push(&mut self, event: &Value) -> Result<(String, bool)> {
        if event["usage"].is_object(){self.usage=event["usage"].clone();}
        if event.get("error").is_some() { bail!("Provider stream reported an error"); }
        let Some(choice) = event["choices"].as_array().and_then(|v| v.first()) else { return Ok((String::new(),false)); };
        let delta = &choice["delta"];
        let content = delta["content"].as_str().unwrap_or("").to_owned();
        self.content.push_str(&content);
        self.reasoning.push_str(delta["reasoning_content"].as_str().unwrap_or(""));
        if let Some(calls) = delta["tool_calls"].as_array() {
            for part in calls {
                let index = part["index"].as_u64().ok_or_else(|| anyhow!("Invalid Provider tool index"))?;
                if index > 127 { bail!("Provider tool-call limit exceeded"); }
                let call = self.calls.entry(index).or_insert_with(||json!({"type":"function_call","call_id":"","name":"","arguments":""}));
                for (target, source) in [("call_id",&part["id"]),("name",&part["function"]["name"]),("arguments",&part["function"]["arguments"])] {
                    if let Some(s) = source.as_str() { call[target] = json!(format!("{}{s}",call[target].as_str().unwrap_or(""))); }
                }
            }
        }
        if self.content.len()+self.reasoning.len()+self.calls.values().map(|c|c.to_string().len()).sum::<usize>() > MAX_FRAME { bail!("Provider completion exceeded limit"); }
        let finished = match choice["finish_reason"].as_str() {
            None => false,
            Some("stop"|"tool_calls") => true,
            Some("length") => bail!("Provider response reached token limit"),
            Some(_) => bail!("Provider completion was not successful"),
        };
        self.finished |= finished;
        Ok((content,finished))
    }
    fn result(self) -> Result<Value> {
        let mut output = Vec::new();
        // Keep text on the tool-call assistant message, not a second message
        // between its calls and tool results. Reasoner needs reasoning on continuations.
        let has_calls = !self.calls.is_empty();
        if !self.content.is_empty() || !has_calls {
            output.push(json!({"type":"message","role":"assistant","content":[{"type":"output_text","text":self.content}],"deepseek_reasoning":self.reasoning}));
        }
        for mut call in self.calls.into_values() {
            required(&call,"call_id")?; required(&call,"name")?;
            serde_json::from_str::<Value>(required(&call,"arguments")?).map_err(|_|anyhow!("Invalid Provider tool arguments"))?;
            call["deepseek_reasoning"] = json!(self.reasoning);
            output.push(call);
        }
        Ok(json!({"status":"completed","output":output,"usage":self.usage}))
    }
}
impl Provider {
    /// Fixed installation endpoints, bounded bytes/time, and no secrets in query strings.
    pub async fn discover_models(&self, provider:ProviderId, tokens:&Value, cancel:CancellationToken)->Result<Value> {
        let base=provider.chat_endpoint().ok_or_else(||anyhow!("Subscription discovery is unavailable"))?;
        let fixed=base.strip_suffix("chat/completions").ok_or_else(||anyhow!("Discovery unavailable"))?.to_owned()+"models";
        #[cfg(not(test))]
        let endpoint=fixed.as_str();
        #[cfg(test)]
        let endpoint=self.responses_endpoint.as_deref().unwrap_or(&fixed);
        let read=async {
            let response=self.client.get(endpoint).bearer_auth(required(tokens,"api_key")?)
                .header("Accept","application/json").send().await
                .map_err(|_|typed_failure(FailureKind::Transient,false,"Provider discovery connection failed"))?;
            if !response.status().is_success(){return Err(response_failure(response,&cancel).await);}
            let mut stream=response.bytes_stream();let mut bytes=Vec::new();
            while let Some(chunk)=stream.next().await {
                let chunk=chunk.map_err(|_|typed_failure(FailureKind::Transient,false,"Provider discovery interrupted"))?;
                if bytes.len()+chunk.len()>1024*1024 {return Err(typed_failure(FailureKind::Protocol,false,"Provider discovery exceeded limit"));}
                bytes.extend_from_slice(&chunk);
            }
            let data:Value=serde_json::from_slice(&bytes).map_err(|_|typed_failure(FailureKind::Protocol,false,"Invalid provider discovery response"))?;
            let rows=data["data"].as_array().ok_or_else(||typed_failure(FailureKind::Protocol,false,"Provider discovery listing unavailable"))?;
            let mut ids=std::collections::BTreeSet::new();
            let models:Vec<_>=rows.iter().filter_map(|row|row["id"].as_str())
                .filter(|id|crate::platform_catalog::valid_model(id)&&ids.insert((*id).to_owned()))
                .take(256).map(|id|json!({"id":id,"capabilities_verified":false})).collect();
            Ok(json!({"models":models,"provider_acceptance_verified":false}))
        };
        tokio::select! {
            biased;
            _=cancel.cancelled()=>Err(typed_failure(FailureKind::Cancelled,false,"Provider discovery cancelled")),
            result=tokio::time::timeout(Duration::from_secs(60),read)=>result.map_err(|_|typed_failure(FailureKind::Transient,false,"Provider discovery timed out"))?
        }
    }
    pub(super) async fn complete_compatible(&self, provider: ProviderId, tokens: &Value, model: &str, input: Vec<Value>, tools: Vec<Value>, instructions: &str, delta: mpsc::Sender<String>, cancel: CancellationToken) -> Result<Value> {
        if instructions.is_empty() || instructions.len()>MAX_INSTRUCTIONS { return Err(typed_failure(FailureKind::MalformedRequest,false,"Invalid provider instructions")); }
        let mut body = request_body(model,input,tools,instructions).map_err(|_|typed_failure(FailureKind::MalformedRequest,false,"Invalid Provider request"))?;
        if provider != ProviderId::Deepseek {
            for message in body["messages"].as_array_mut().unwrap() {
                message.as_object_mut().unwrap().remove("reasoning_content");
            }
        }
        let fixed_endpoint = provider.chat_endpoint().ok_or_else(||anyhow!("Provider does not use API keys"))?;
        #[cfg(not(test))]
        let endpoint = fixed_endpoint;
        #[cfg(test)]
        let endpoint = self.responses_endpoint.as_deref().unwrap_or(fixed_endpoint);
        let request = self.client.post(endpoint).bearer_auth(required(tokens,"api_key")?).header("Accept","text/event-stream").json(&body);
        let response = tokio::select! {
            biased;
            _ = cancel.cancelled() => return Err(typed_failure(FailureKind::Cancelled,false,"Provider request cancelled")),
            result = tokio::time::timeout(Duration::from_secs(60),request.send()) => result
                .map_err(|_|typed_failure(FailureKind::Transient,false,"Provider connection timed out"))?
                .map_err(|_|typed_failure(FailureKind::Transient,false,"Provider connection failed"))?
        };
        if !response.status().is_success() { return Err(response_failure(response,&cancel).await); }
        let mut stream = response.bytes_stream();
        let mut frames = Frames::default(); let mut completion = Completion::default(); let mut started = false;
        loop {
            let chunk = tokio::select! {
                biased;
                _ = cancel.cancelled() => return Err(typed_failure(FailureKind::Cancelled,started,"Provider request cancelled")),
                result = tokio::time::timeout(Duration::from_secs(120),stream.next()) => result.map_err(|_|typed_failure(FailureKind::Transient,started,"Provider stream stalled"))?
            };
            // Usage is often a final empty-choice frame after finish_reason.
            // Preserve providers which close a finished stream without [DONE].
            if chunk.is_none() && completion.finished {
                return completion.result().map_err(|e|typed_failure(FailureKind::Protocol,started,e.to_string()));
            }
            let chunk = chunk.ok_or_else(||typed_failure(FailureKind::Transient,started,"Provider stream ended before completion"))?
                .map_err(|_|typed_failure(FailureKind::Transient,started,"Provider stream interrupted"))?;
            for event in frames.push(&chunk).map_err(|_|typed_failure(FailureKind::Protocol,started,"Invalid Provider stream event"))? {
                // Any choice means generation began, even if only reasoning arrived.
                started |= event["choices"].as_array().is_some_and(|v|!v.is_empty());
                let (content,finished) = completion.push(&event).map_err(|e|typed_failure(FailureKind::Protocol,started,e.to_string()))?;
                if !content.is_empty() {
                    tokio::select! {
                        biased;
                        _ = cancel.cancelled() => return Err(typed_failure(FailureKind::Cancelled,started,"Provider request cancelled")),
                        sent = delta.send(content) => sent.map_err(|_|typed_failure(FailureKind::Protocol,started,"Stream consumer disconnected"))?
                    }
                }
                let _=finished;
            }
            if frames.done && completion.finished {
                return completion.result().map_err(|e|typed_failure(FailureKind::Protocol,started,e.to_string()));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn streams_text_with_the_deepseek_key_not_subscription_headers() {
        use axum::{extract::State,http::HeaderMap,routing::post,Json,Router};
        use std::sync::Arc;
        use tokio::sync::Mutex;
        let captured=Arc::new(Mutex::new(None::<(HeaderMap,Value)>));
        let app=Router::new().route("/chat",post(|State(state):State<Arc<Mutex<Option<(HeaderMap,Value)>>>>,headers:HeaderMap,Json(body):Json<Value>| async move {
            *state.lock().await=Some((headers,body));
            ([("content-type","text/event-stream")], "data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\" world\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n")
        })).with_state(captured.clone());
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();let addr=listener.local_addr().unwrap();
        let server=tokio::spawn(async move {axum::serve(listener,app).await.unwrap();});
        let provider=Provider::for_test(format!("http://{addr}/chat"));
        let (sender,mut receiver)=mpsc::channel(8);
        let selection=ProviderSelection::parse(&json!({"provider":"deepseek","model":"deepseek-chat"}),"unused").unwrap();
        let result=provider.complete_selected_with_instructions(&selection,&json!({"api_key":"test-key","access_token":"do-not-use","account_id":"do-not-send"}),vec![json!({"role":"user","content":"hi"})],vec![],"session","help",sender,CancellationToken::new()).await.unwrap();
        assert_eq!(result["output"][0]["content"][0]["text"],"Hello world");
        assert_eq!(receiver.recv().await.unwrap(),"Hello");assert_eq!(receiver.recv().await.unwrap()," world");
        let captured=captured.lock().await;let (headers,body)=captured.as_ref().unwrap();
        assert_eq!(headers["authorization"],"Bearer test-key");assert!(!headers.contains_key("chatgpt-account-id"));assert_eq!(body["model"],"deepseek-chat");
        server.abort();
    }
    #[tokio::test]
    async fn usage_after_finish_reason_is_retained(){
        use axum::{routing::post,Router};
        let app=Router::new().route("/chat",post(||async{
            ([("content-type","text/event-stream")],r#"data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}

data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":3,"prompt_tokens_details":{"cached_tokens":10}}}

data: [DONE]

"#)
        }));
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();let addr=listener.local_addr().unwrap();
        let server=tokio::spawn(async move{axum::serve(listener,app).await.unwrap();});
        let provider=Provider::for_test(format!("http://{addr}/chat"));
        let (tx,_rx)=mpsc::channel(8);
        let selection=ProviderSelection{provider:ProviderId::Deepseek,model:"fixture".into()};
        let result=provider.complete_selected_with_instructions(&selection,&json!({"api_key":"fixture"}),vec![],vec![],"s","help",tx,CancellationToken::new()).await.unwrap();
        assert_eq!(result["usage"]["prompt_tokens"],42);assert_eq!(result["usage"]["completion_tokens"],3);
        server.abort();
    }
    #[tokio::test]
    async fn discovery_returns_only_validated_ids_and_never_sends_keys_in_urls() {
        use axum::{routing::get,Router};
        let app=Router::new().route("/models",get(|headers:axum::http::HeaderMap,uri:axum::http::Uri|async move {
            assert_eq!(headers["authorization"],"Bearer discovery-key");assert!(uri.query().is_none());
            axum::Json(json!({"data":[{"id":"vendor/model","api_key":"must-not-project"},{"id":"vendor/model"},{"id":"bad\nmodel"},{"id":"valid"}]}))
        }));
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();let addr=listener.local_addr().unwrap();
        let server=tokio::spawn(async move {axum::serve(listener,app).await.unwrap();});
        let provider=Provider::for_test(format!("http://{addr}/models"));
        let result=provider.discover_models(ProviderId::Openrouter,&json!({"api_key":"discovery-key"}),CancellationToken::new()).await.unwrap();
        assert_eq!(result["models"].as_array().unwrap().len(),2);
        assert_eq!(result["models"][0]["id"],"vendor/model");assert!(!result.to_string().contains("must-not-project"));
        assert_eq!(result["provider_acceptance_verified"],false);
        let cancel=CancellationToken::new();cancel.cancel();
        assert!(provider.discover_models(ProviderId::Openrouter,&json!({"api_key":"discovery-key"}),cancel).await.is_err());
        server.abort();
    }
    #[tokio::test]
    async fn discovery_rejects_oversized_response() {
        use axum::{routing::get,Router};
        let app=Router::new().route("/models",get(||async {"x".repeat(1024*1024+1)}));
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();let addr=listener.local_addr().unwrap();
        let server=tokio::spawn(async move {axum::serve(listener,app).await.unwrap();});
        let provider=Provider::for_test(format!("http://{addr}/models"));
        let error=provider.discover_models(ProviderId::Openrouter,&json!({"api_key":"discovery-key"}),CancellationToken::new()).await.unwrap_err();
        assert!(error.to_string().contains("exceeded limit"));server.abort();
    }
    #[tokio::test]
    async fn fixed_providers_execute_with_isolated_api_credentials() {
        use axum::{extract::State,http::HeaderMap,routing::post,Json,Router};
        use std::sync::Arc;
        use tokio::sync::Mutex;
        let captured=Arc::new(Mutex::new(None::<(HeaderMap,Value)>));
        let app=Router::new().route("/chat",post(|State(state):State<Arc<Mutex<Option<(HeaderMap,Value)>>>>,headers:HeaderMap,Json(body):Json<Value>| async move {
            *state.lock().await=Some((headers,body));
            ([("content-type","text/event-stream")], "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\n")
        })).with_state(captured.clone());
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();let addr=listener.local_addr().unwrap();
        let server=tokio::spawn(async move {axum::serve(listener,app).await.unwrap();});
        let provider=Provider::for_test(format!("http://{addr}/chat"));
        for id in [ProviderId::OpenaiApi,ProviderId::Openrouter,ProviderId::Abacus,ProviderId::Gemini,
            ProviderId::Groq,ProviderId::Together,ProviderId::Mistral,ProviderId::Fireworks,
            ProviderId::Xai,ProviderId::Perplexity,ProviderId::Deepinfra] {
            let endpoint=reqwest::Url::parse(id.chat_endpoint().unwrap()).unwrap();
            assert_eq!(endpoint.scheme(),"https");assert!(endpoint.username().is_empty());
            let selection=ProviderSelection{provider:id,model:"vendor/model".into()};
            let (sender,_receiver)=mpsc::channel(8);
            let result=provider.complete_selected_with_instructions(&selection,
                &json!({"api_key":"isolated-key","access_token":"never-forward","account_id":"never-forward"}),
                vec![json!({"role":"assistant","content":"prior","deepseek_reasoning":"private reasoning"})],
                vec![],"session","help",sender,CancellationToken::new()).await.unwrap();
            assert_eq!(result["output"][0]["content"][0]["text"],"ok");
            let capture=captured.lock().await;let (headers,body)=capture.as_ref().unwrap();
            assert_eq!(headers["authorization"],"Bearer isolated-key");
            assert!(!headers.contains_key("chatgpt-account-id"));
            assert_eq!(body["model"],"vendor/model");
            assert!(body["messages"][1].get("reasoning_content").is_none());
            assert!(!body.to_string().contains("never-forward"));
        }
        server.abort();
    }
    #[tokio::test]
    async fn fixed_provider_redirect_cannot_forward_key() {
        use axum::{routing::post,Router};
        let app=Router::new().route("/chat",post(||async {
            (axum::http::StatusCode::TEMPORARY_REDIRECT,[("location","http://127.0.0.1:1/private")])
        }));
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();let addr=listener.local_addr().unwrap();
        let server=tokio::spawn(async move {axum::serve(listener,app).await.unwrap();});
        let provider=Provider::for_test(format!("http://{addr}/chat"));
        let (sender,_receiver)=mpsc::channel(8);
        let error=provider.complete_selected_with_instructions(&ProviderSelection{provider:ProviderId::Openrouter,model:"vendor/model".into()},
            &json!({"api_key":"never-leak-key"}),vec![],vec![],"session","help",sender,CancellationToken::new()).await.unwrap_err();
        assert!(!error.to_string().contains("never-leak-key"));
        assert!(!error.to_string().contains("private"));
        server.abort();
    }
    #[test]
    fn text_and_tool_calls_share_one_assistant_message() {
        let mut c=Completion::default();
        c.push(&json!({"choices":[{"delta":{"content":"checking","reasoning_content":"thinking","tool_calls":[{"index":0,"id":"call1","function":{"name":"test","arguments":"{}"}}]},"finish_reason":"tool_calls"}]})).unwrap();
        let result=c.result().unwrap();let mut input=result["output"].as_array().unwrap().clone();
        input.push(json!({"type":"function_call_output","call_id":"call1","output":"done"}));
        let body=request_body("deepseek-reasoner",input,vec![],"help").unwrap();
        assert_eq!(body["messages"].as_array().unwrap().len(),3);
        assert_eq!(body["messages"][1]["content"],"checking");assert_eq!(body["messages"][1]["tool_calls"][0]["id"],"call1");
        assert_eq!(body["messages"][2]["role"],"tool");
    }
    #[test]
    fn translates_tools_and_results() {
        let body=request_body("deepseek-chat",vec![json!({"role":"user","content":"hi"}),
            json!({"type":"function_call","call_id":"call1","name":"test","arguments":"{}","deepseek_reasoning":"thinking"}),
            json!({"type":"function_call_output","call_id":"call1","output":"ok"})],
            vec![json!({"type":"function","name":"test","description":"test","parameters":{"type":"object"}})],"help").unwrap();
        assert_eq!(body["messages"][2]["tool_calls"][0]["id"],"call1");
        assert_eq!(body["messages"][2]["reasoning_content"],"thinking");
        assert_eq!(body["messages"][3]["role"],"tool");
        assert_eq!(body["tools"][0]["function"]["name"],"test");
    }
    #[test]
    fn assembles_fragmented_tool_calls() {
        let mut c=Completion::default();
        c.push(&json!({"choices":[{"delta":{"reasoning_content":"think","tool_calls":[{"index":0,"id":"call1","function":{"name":"test","arguments":"{"}}]}}]})).unwrap();
        let (_,done)=c.push(&json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]},"finish_reason":"tool_calls"}]})).unwrap();
        assert!(done);let output=c.result().unwrap();assert_eq!(output["output"][0]["arguments"],"{}");assert_eq!(output["output"][0]["deepseek_reasoning"],"think");
    }
}
