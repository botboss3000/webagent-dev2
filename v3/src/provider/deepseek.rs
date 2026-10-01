//! DeepSeek's OpenAI-compatible chat API, translated to the runtime's Responses items.
use super::*;
use crate::model_selection::FailureKind;
use std::collections::BTreeMap;

fn text(value: &Value) -> String {
    if let Some(s) = value.as_str() { return s.to_owned(); }
    value.as_array().map(|items| items.iter().filter_map(|v| v["text"].as_str()).collect::<Vec<_>>().join("")).unwrap_or_default()
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
                if !matches!(role,"user"|"assistant"|"system"|"developer") { bail!("Invalid DeepSeek message role"); }
                let mut message = json!({"role":if role=="developer" {"system"} else {role},"content":text(&item["content"])});
                if let Some(reasoning) = item.get("deepseek_reasoning") { message["reasoning_content"] = reasoning.clone(); }
                messages.push(message);
            },
            _ => bail!("Unsupported DeepSeek input item"),
        }
    }
    let mut body = json!({"model":model,"messages":messages,"stream":true,"max_tokens":8192});
    if !tools.is_empty() {
        body["tools"] = json!(tools.into_iter().map(|t| json!({"type":"function","function":{
            "name":t["name"],"description":t["description"],"parameters":t["parameters"]}})).collect::<Vec<_>>());
        body["tool_choice"] = json!("auto");
    }
    Ok(body)
}

#[derive(Default)]
struct Completion { content: String, reasoning: String, calls: BTreeMap<u64,Value> }
impl Completion {
    fn push(&mut self, event: &Value) -> Result<(String, bool)> {
        if event.get("error").is_some() { bail!("DeepSeek stream reported an error"); }
        let Some(choice) = event["choices"].as_array().and_then(|v| v.first()) else { return Ok((String::new(),false)); };
        let delta = &choice["delta"];
        let content = delta["content"].as_str().unwrap_or("").to_owned();
        self.content.push_str(&content);
        self.reasoning.push_str(delta["reasoning_content"].as_str().unwrap_or(""));
        if let Some(calls) = delta["tool_calls"].as_array() {
            for part in calls {
                let index = part["index"].as_u64().ok_or_else(|| anyhow!("Invalid DeepSeek tool index"))?;
                if index > 127 { bail!("DeepSeek tool-call limit exceeded"); }
                let call = self.calls.entry(index).or_insert_with(||json!({"type":"function_call","call_id":"","name":"","arguments":""}));
                for (target, source) in [("call_id",&part["id"]),("name",&part["function"]["name"]),("arguments",&part["function"]["arguments"])] {
                    if let Some(s) = source.as_str() { call[target] = json!(format!("{}{s}",call[target].as_str().unwrap_or(""))); }
                }
            }
        }
        if self.content.len()+self.reasoning.len()+self.calls.values().map(|c|c.to_string().len()).sum::<usize>() > MAX_FRAME { bail!("DeepSeek completion exceeded limit"); }
        let finished = match choice["finish_reason"].as_str() {
            None => false,
            Some("stop"|"tool_calls") => true,
            Some("length") => bail!("DeepSeek response reached token limit"),
            Some(_) => bail!("DeepSeek completion was not successful"),
        };
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
            serde_json::from_str::<Value>(required(&call,"arguments")?).map_err(|_|anyhow!("Invalid DeepSeek tool arguments"))?;
            call["deepseek_reasoning"] = json!(self.reasoning);
            output.push(call);
        }
        Ok(json!({"status":"completed","output":output}))
    }
}
impl Provider {
    pub(super) async fn complete_deepseek(&self, tokens: &Value, model: &str, input: Vec<Value>, tools: Vec<Value>, instructions: &str, delta: mpsc::Sender<String>, cancel: CancellationToken) -> Result<Value> {
        if instructions.is_empty() || instructions.len()>MAX_INSTRUCTIONS { return Err(typed_failure(FailureKind::MalformedRequest,false,"Invalid provider instructions")); }
        let body = request_body(model,input,tools,instructions).map_err(|_|typed_failure(FailureKind::MalformedRequest,false,"Invalid DeepSeek request"))?;
        #[cfg(not(test))]
        let endpoint = "https://api.deepseek.com/chat/completions";
        #[cfg(test)]
        let endpoint = self.responses_endpoint.as_deref().unwrap_or("https://api.deepseek.com/chat/completions");
        let request = self.client.post(endpoint).bearer_auth(required(tokens,"api_key")?).header("Accept","text/event-stream").json(&body);
        let response = tokio::select! {
            biased;
            _ = cancel.cancelled() => return Err(typed_failure(FailureKind::Cancelled,false,"Provider request cancelled")),
            result = tokio::time::timeout(Duration::from_secs(60),request.send()) => result
                .map_err(|_|typed_failure(FailureKind::Transient,false,"DeepSeek connection timed out"))?
                .map_err(|_|typed_failure(FailureKind::Transient,false,"DeepSeek connection failed"))?
        };
        if !response.status().is_success() { return Err(response_failure(response,&cancel).await); }
        let mut stream = response.bytes_stream();
        let mut frames = Frames::default(); let mut completion = Completion::default(); let mut started = false;
        loop {
            let chunk = tokio::select! {
                biased;
                _ = cancel.cancelled() => return Err(typed_failure(FailureKind::Cancelled,started,"Provider request cancelled")),
                result = tokio::time::timeout(Duration::from_secs(120),stream.next()) => result.map_err(|_|typed_failure(FailureKind::Transient,started,"DeepSeek stream stalled"))?
            };
            let chunk = chunk.ok_or_else(||typed_failure(FailureKind::Transient,started,"DeepSeek stream ended before completion"))?
                .map_err(|_|typed_failure(FailureKind::Transient,started,"DeepSeek stream interrupted"))?;
            for event in frames.push(&chunk).map_err(|_|typed_failure(FailureKind::Protocol,started,"Invalid DeepSeek stream event"))? {
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
                if finished { return completion.result().map_err(|e|typed_failure(FailureKind::Protocol,started,e.to_string())); }
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
