//! Parallel voice conversation: transient recordings/questions, explicit durable work admission.
use super::*;
use base64::{engine::general_purpose::STANDARD, Engine};
use std::sync::OnceLock;
#[derive(Default)]
struct Lane {
    expires: u64,
    capture: Option<Capture>,
    completed: Option<(String, String)>,
    proposal: Option<Value>,
    history: Vec<Value>,
    busy: bool,
}
struct Capture {
    id: String,
    seq: u64,
    last_hash: String,
    bytes: Vec<u8>,
    run: Value,
    speaking: bool,
    voiced_ms: f64,
    silence_ms: f64,
    partial_bytes: usize,
}
static LANES: OnceLock<Mutex<HashMap<String, Lane>>> = OnceLock::new();
fn lanes() -> &'static Mutex<HashMap<String, Lane>> {
    LANES.get_or_init(|| Mutex::new(HashMap::new()))
}
fn lane_key(app: &App, sid: &str) -> String {
    format!("{}\0{sid}", app.store.current_user_id())
}
fn get_lane<'a>(all: &'a mut HashMap<String, Lane>, key: &str) -> Result<&'a mut Lane, ApiError> {
    all.retain(|_, v| v.expires > now() || v.busy);
    if !all.contains_key(key) && all.len() >= 64 {
        return Err(conflict("Voice is busy; try again shortly"));
    }
    let lane = all.entry(key.to_owned()).or_default();
    lane.expires = now() + 1800;
    Ok(lane)
}
fn wav(pcm: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(pcm.len() + 44);
    out.extend(b"RIFF");
    out.extend(((pcm.len() + 36) as u32).to_le_bytes());
    out.extend(b"WAVEfmt ");
    out.extend(16u32.to_le_bytes());
    out.extend(1u16.to_le_bytes());
    out.extend(1u16.to_le_bytes());
    out.extend(16000u32.to_le_bytes());
    out.extend(32000u32.to_le_bytes());
    out.extend(2u16.to_le_bytes());
    out.extend(16u16.to_le_bytes());
    out.extend(b"data");
    out.extend((pcm.len() as u32).to_le_bytes());
    out.extend(pcm);
    out
}
async fn transcribe(app: &App, run: &Value, bytes: &[u8], mime: &str) -> Result<String, ApiError> {
    let (selection, tokens) = media_lane::lane_selection(app, run).await?;
    let text = app
        .provider
        .transcribe(&selection, &tokens, bytes, mime, app.stop.child_token())
        .await
        .map_err(|_| {
            ApiError(
                StatusCode::BAD_GATEWAY,
                "Recording could not be transcribed by the configured Voice model".into(),
            )
        })?;
    media_lane::lane_selection(app, run).await?;
    Ok(text)
}
pub(crate) async fn voice_audio(
    State(app): State<App>,
    Path(sid): Path<String>,
    Json(body): Json<Value>,
) -> Api {
    let op = required(&body, "op", 16)?;
    let k = lane_key(&app, &sid);
    if op == "start" {
        let run = media_lane::lane_run(&app, &sid, model_selection::ModelRole::Voice).await?;
        media_lane::lane_selection(&app, &run).await?;
        if run["selection_plan"]["candidates"][0]["transport"] == "openai_subscription" {
            return Err(conflict(
                "Voice recording requires an audio-capable API model connection",
            ));
        }
        let mut all = lanes().lock().await;
        let lane = get_lane(&mut all, &k)?;
        if lane.busy || lane.capture.is_some() {
            return Err(conflict("A voice turn is already active"));
        }
        let id = uuid();
        lane.completed = None;
        lane.capture = Some(Capture {
            id: id.clone(),
            seq: 0,
            last_hash: String::new(),
            bytes: vec![],
            run,
            speaking: false,
            voiced_ms: 0.,
            silence_ms: 0.,
            partial_bytes: 0,
        });
        return Ok(Json(
            json!({"capture_id":id,"sample_rate":16000,"ephemeral":true}),
        ));
    }
    let id = required(&body, "capture_id", 64)?;
    if op == "cancel" {
        let mut all = lanes().lock().await;
        let lane = get_lane(&mut all, &k)?;
        if lane.capture.as_ref().is_some_and(|c| c.id == id) {
            lane.capture = None;
        }
        return Ok(Json(json!({"cancelled":true,"ephemeral":true})));
    }
    if op == "chunk" {
        let data = required(&body, "data", 128 * 1024)?;
        let bytes = STANDARD
            .decode(data)
            .map_err(|_| bad("Invalid PCM frame"))?;
        if bytes.is_empty() || bytes.len() % 2 != 0 || bytes.len() > 64 * 1024 {
            return Err(bad("Invalid PCM frame bounds"));
        }
        let sequence = body["seq"]
            .as_u64()
            .ok_or_else(|| bad("PCM sequence required"))?;
        let hash = hex::encode(Sha256::digest(&bytes));
        let mut all = lanes().lock().await;
        let lane = get_lane(&mut all, &k)?;
        let c = lane
            .capture
            .as_mut()
            .filter(|c| c.id == id)
            .ok_or_else(|| conflict("Recording expired; start a new voice turn"))?;
        if sequence == c.seq && hash == c.last_hash {
            return Ok(Json(json!({"seq":c.seq,"replayed":true,"ephemeral":true})));
        }
        if sequence != c.seq + 1 || c.bytes.len() + bytes.len() > 16000 * 2 * 30 {
            return Err(bad("PCM sequence or recording limit exceeded"));
        }
        let ms = bytes.len() as f64 / 32.;
        let rms = (bytes
            .chunks_exact(2)
            .map(|v| {
                let sample = i16::from_le_bytes([v[0], v[1]]) as f64 / 32768.;
                sample * sample
            })
            .sum::<f64>()
            / (bytes.len() / 2) as f64)
            .sqrt();
        if !c.speaking {
            c.voiced_ms = if rms >= 0.018 { c.voiced_ms + ms } else { 0. };
            if c.voiced_ms >= 120. {
                c.speaking = true;
            }
        } else {
            c.silence_ms = if rms >= 0.018 { 0. } else { c.silence_ms + ms };
        }
        c.bytes.extend(bytes);
        c.seq = sequence;
        c.last_hash = hash;
        let end = c.speaking && c.silence_ms >= 700. || c.bytes.len() >= 16000 * 2 * 30;
        let speaking = c.speaking;
        let partial =
            if c.speaking && !end && c.bytes.len().saturating_sub(c.partial_bytes) >= 16000 * 2 * 3
            {
                c.partial_bytes = c.bytes.len();
                Some((c.run.clone(), wav(&c.bytes)))
            } else {
                None
            };
        drop(all);
        let partial_text = if let Some((run, bytes)) = partial {
            transcribe(&app, &run, &bytes, "audio/wav").await.ok()
        } else {
            None
        };
        return Ok(Json(
            json!({"seq":sequence,"speaking":speaking,"turn_end":end,"partial":partial_text,"ephemeral":true}),
        ));
    }
    if !matches!(op, "finish" | "recording") {
        return Err(bad("Unknown voice audio operation"));
    }
    let mut all = lanes().lock().await;
    let lane = get_lane(&mut all, &k)?;
    if let Some((done, text)) = &lane.completed {
        if done == id {
            return Ok(Json(
                json!({"text":text,"capture_id":id,"ephemeral":true,"replayed":true}),
            ));
        }
    }
    if lane.busy {
        return Err(conflict("Voice is still transcribing"));
    }
    if !lane.capture.as_ref().is_some_and(|c| c.id == id) {
        return Err(conflict("Recording expired; start a new voice turn"));
    }
    let c = lane.capture.take().unwrap();
    if op == "finish" && !c.speaking {
        lane.completed = Some((id.to_owned(), String::new()));
        return Ok(Json(json!({"text":"","capture_id":id,"ephemeral":true})));
    }
    lane.busy = true;
    let recording = if op == "recording" {
        let data = body["data"].as_str().unwrap_or("");
        if data.len() > 14 * 1024 * 1024 {
            lane.busy = false;
            return Err(bad("Recording exceeds 10 MB"));
        }
        let bytes = STANDARD.decode(data).map_err(|_| {
            lane.busy = false;
            bad("Invalid recording")
        })?;
        (
            bytes,
            body["media_type"]
                .as_str()
                .unwrap_or("")
                .split(';')
                .next()
                .unwrap_or("")
                .to_owned(),
        )
    } else {
        (wav(&c.bytes), "audio/wav".to_owned())
    };
    drop(all);
    let result = transcribe(&app, &c.run, &recording.0, &recording.1).await;
    let mut all = lanes().lock().await;
    let lane = get_lane(&mut all, &k)?;
    lane.busy = false;
    match result {
        Ok(text) => {
            lane.completed = Some((id.to_owned(), text.clone()));
            Ok(Json(json!({"text":text,"capture_id":id,"ephemeral":true})))
        }
        Err(error) => Err(error),
    }
}
fn words(utterance: &str) -> String {
    utterance
        .trim()
        .trim_end_matches(['.', '!', '?'])
        .to_ascii_lowercase()
}
fn yes(text: &str) -> bool {
    matches!(
        text,
        "yes"
            | "yeah"
            | "yep"
            | "sure"
            | "okay"
            | "ok"
            | "do it"
            | "go ahead"
            | "confirm"
            | "approve"
            | "approved"
    )
}
fn no(text: &str) -> bool {
    matches!(
        text,
        "no" | "nope"
            | "nah"
            | "never mind"
            | "nevermind"
            | "reject"
            | "don't"
            | "do not"
            | "cancel"
    )
}
fn work(text: &str) -> bool {
    let text = text
        .strip_prefix("please ")
        .or_else(|| text.strip_prefix("can you "))
        .or_else(|| text.strip_prefix("could you "))
        .or_else(|| text.strip_prefix("would you "))
        .unwrap_or(text)
        .trim_start_matches("please ");
    [
        "build",
        "create",
        "implement",
        "fix",
        "write",
        "make",
        "add",
        "remove",
        "update",
        "run",
        "test",
        "deploy",
        "research",
        "find",
        "open",
        "send",
        "generate",
    ]
    .contains(&text.split_whitespace().next().unwrap_or(""))
}
fn question(text: &str) -> bool {
    [
        "what", "why", "when", "where", "who", "how", "is", "are", "can", "could", "do", "does",
        "did", "explain",
    ]
    .contains(&text.split_whitespace().next().unwrap_or(""))
        || text.starts_with("tell me")
}
fn control(text: &str) -> Option<&'static str> {
    let text = text.strip_prefix("please ").unwrap_or(text);
    let mut split = text.split_whitespace();
    let verb = split.next()?;
    let tail = split.collect::<Vec<_>>().join(" ");
    if [
        "",
        "run",
        "task",
        "work",
        "job",
        "agent",
        "the run",
        "the task",
        "the work",
        "the job",
        "the agent",
        "this",
        "what you're doing",
        "what you are doing",
    ]
    .contains(&tail.as_str())
    {
        match verb {
            "stop" | "cancel" | "abort" => return Some("cancel"),
            "pause" => return Some("pause"),
            "resume" | "continue" => return Some("resume"),
            _ => {}
        }
    }
    if matches!(text, "hold on" | "wait a moment" | "wait a second") {
        Some("pause")
    } else {
        None
    }
}
async fn voice_target(app: &App, sid: &str) -> Result<Option<Value>, ApiError> {
    let view = app
        .store
        .active_runs()
        .await?
        .into_iter()
        .find(|r| r["session_id"] == sid && !terminal(status(r)));
    if let Some(view) = view {
        Ok(Some(app.run(view["id"].as_str().unwrap()).await?))
    } else {
        Ok(None)
    }
}
pub(crate) async fn voice_route(
    State(app): State<App>,
    Path(sid): Path<String>,
    Json(body): Json<Value>,
) -> Api {
    let utterance = required(&body, "utterance", 8192)?.trim().to_owned();
    let text = words(&utterance);
    let _guard = app.gate.lock().await;
    let target = voice_target(&app, &sid).await?;
    let mode = session_mode(&app, &sid).await?;
    let k = lane_key(&app, &sid);
    let mut all = lanes().lock().await;
    let lane = get_lane(&mut all, &k)?;
    if lane.busy {
        return Err(conflict("Voice is still answering"));
    }
    if yes(&text) || no(&text) {
        let proposal = lane
            .proposal
            .as_mut()
            .filter(|p| {
                p["expires"].as_u64().unwrap_or(0) > now()
                    && p["state"] == "pending"
                    && body["proposal_id"] == p["id"]
            })
            .ok_or_else(|| conflict("No matching pending voice proposal; nothing was changed"))?;
        if no(&text) {
            proposal["state"] = json!("rejected");
            return Ok(Json(
                json!({"action":"noop","answer":"The current work is unchanged.","proposal":proposal,"ephemeral":true}),
            ));
        }
        if proposal["target_run_id"]
            != target
                .as_ref()
                .map(|r| r["id"].clone())
                .unwrap_or(Value::Null)
            || proposal["target_revision"]
                != target
                    .as_ref()
                    .map(|r| r["run_revision"].clone())
                    .unwrap_or(Value::Null)
            || proposal["mode_revision"] != mode["revision"]
        {
            lane.proposal = None;
            return Err(conflict(
                "Run or mode changed; repeat the voice request before confirming",
            ));
        }
        proposal["state"] = json!("ready");
        return Ok(Json(
            json!({"action":"dispatch_work","ticket":proposal["id"],"command":proposal["command"],"ephemeral":true}),
        ));
    }
    if let Some(action) = control(&text) {
        let Some(run) = target else {
            return Ok(Json(
                json!({"action":"noop","answer":"There is no active or paused run to control.","ephemeral":true}),
            ));
        };
        let ticket = uuid();
        lane.proposal = Some(
            json!({"id":ticket,"state":"ready","control":action,"target_run_id":run["id"],"target_revision":run["run_revision"],"mode_revision":mode["revision"],"expires":now()+120}),
        );
        return Ok(Json(
            json!({"action":"interrupt","ticket":ticket,"control_action":action,"ephemeral":true}),
        ));
    }
    if text
        .split_whitespace()
        .any(|w| matches!(w, "status" | "progress"))
        || matches!(
            text.as_str(),
            "what's happening"
                | "what is happening"
                | "what are you doing"
                | "how is it going"
                | "are you done"
                | "is it done"
        )
    {
        return Ok(Json(
            json!({"action":"answer","answer":target.as_ref().map(|r|format!("The run is {}.",status(r))).unwrap_or_else(||"There is no active run.".into()),"response_source":"system","ephemeral":true}),
        ));
    }
    let modifies = text.split_whitespace().any(|w| {
        matches!(
            w,
            "instead"
                | "change"
                | "modify"
                | "revise"
                | "switch"
                | "replace"
                | "remove"
                | "rename"
                | "update"
                | "also"
                | "add"
                | "include"
                | "use"
        )
    });
    let is_work = work(&text) || target.is_some() && modifies;
    let is_question = question(&text) || utterance.ends_with('?');
    let history = lane.history.clone();
    if is_work {
        let ticket = uuid();
        let pending = target.is_some();
        lane.proposal = Some(
            json!({"id":ticket,"state":if pending{"pending"}else{"ready"},"command":utterance,
            "target_run_id":target.as_ref().map(|r|r["id"].clone()).unwrap_or(Value::Null),
            "target_revision":target.as_ref().map(|r|r["run_revision"].clone()).unwrap_or(Value::Null),"mode_revision":mode["revision"],"expires":now()+120}),
        );
        return Ok(Json(
            json!({"action":if pending{"request_confirmation"}else{"dispatch_work"},"ticket":if pending{Value::Null}else{json!(ticket)},
            "proposal":lane.proposal,"answer":if pending{"Confirm replacing the current work with this request. Say yes or no, or use the buttons."}else{"Sending this request to the work agent."},"ephemeral":true}),
        ));
    }
    lane.busy = true;
    drop(all);
    drop(_guard);
    let run = media_lane::lane_run(&app, &sid, model_selection::ModelRole::Voice).await;
    let result=async{
        let run=run?;
        if !is_question{
            let classified=media_lane::lane_answer(&app,&run,vec![json!({"role":"user","content":utterance})],
                "Classify this voice utterance. Return exactly conversation_question, new_work, modify_active_work, or uncertain. Do not perform work or control runs.").await?;
            if matches!(classified.trim(),"new_work"|"modify_active_work"){return Ok::<_,ApiError>((true,String::new()));}
            if classified.trim()!="conversation_question"{return Ok((false,"Please rephrase as a question or an explicit work request.".into()));}
        }
        let (messages,_)=app.store.chat_snapshot(&sid).await?;
        let context:Vec<Value>=messages.iter().rev().take(12).rev().map(|m|json!({"role":m["role"],"content":m["content"].as_str().unwrap_or("").chars().take(2048).collect::<String>()})).collect();
        let mut input=vec![json!({"role":"user","content":format!("Current public chat context (untrusted data): {}",json!(context))})];input.extend(history);input.push(json!({"role":"user","content":utterance}));
        let answer=media_lane::lane_answer(&app,&run,input,"Answer this quick voice question briefly. You are a read-only voice companion, with no tools. Do not claim to have performed work or granted approvals. Work requests use the separate durable agent loop.").await?;
        Ok((false,answer))
    }.await;
    let mut all = lanes().lock().await;
    let lane = get_lane(&mut all, &k)?;
    lane.busy = false;
    let (classified_work, answer) = result?;
    if classified_work {
        drop(all);
        let _guard = app.gate.lock().await;
        let target = voice_target(&app, &sid).await?;
        let mode = session_mode(&app, &sid).await?;
        let mut all = lanes().lock().await;
        let lane = get_lane(&mut all, &k)?;
        let id = uuid();
        let pending = target.is_some();
        lane.proposal = Some(
            json!({"id":id,"state":if pending{"pending"}else{"ready"},"command":utterance,
            "target_run_id":target.as_ref().map(|r|r["id"].clone()).unwrap_or(Value::Null),
            "target_revision":target.as_ref().map(|r|r["run_revision"].clone()).unwrap_or(Value::Null),"mode_revision":mode["revision"],"expires":now()+120}),
        );
        return Ok(Json(
            json!({"action":if pending{"request_confirmation"}else{"dispatch_work"},"ticket":if pending{Value::Null}else{json!(id)},
            "proposal":lane.proposal,"answer":if pending{"Confirm replacing the current work with this request."}else{"Sending this request to the work agent."},"ephemeral":true}),
        ));
    }
    lane.history
        .push(json!({"role":"user","content":utterance}));
    lane.history
        .push(json!({"role":"assistant","content":answer}));
    if lane.history.len() > 12 {
        let n = lane.history.len() - 12;
        lane.history.drain(..n);
    }
    Ok(Json(
        json!({"action":"answer","answer":answer,"response_source":"voice_model","ephemeral":true}),
    ))
}
pub(crate) async fn voice_control(
    State(app): State<App>,
    Path(sid): Path<String>,
    Json(body): Json<Value>,
) -> Api {
    let id = required(&body, "ticket", 64)?.to_owned();
    let receipt_key = format!("receipt:voice:{sid}:{id}");
    if let Some(saved) = app.store.get(&receipt_key).await? {
        return Ok(Json(saved));
    }
    if let Some(saved) = app
        .store
        .get(&format!("receipt:message:{sid}:voice-{id}"))
        .await?
    {
        return Ok(Json(
            json!({"message":"Sent to the work agent.","run_id":saved["result"]["run_id"],"ephemeral":false,"replayed":true}),
        ));
    }
    let k = lane_key(&app, &sid);
    let proposal = {
        let mut all = lanes().lock().await;
        let lane = get_lane(&mut all, &k)?;
        let p = lane
            .proposal
            .as_ref()
            .filter(|p| {
                p["id"] == id && p["state"] == "ready" && p["expires"].as_u64().unwrap_or(0) > now()
            })
            .ok_or_else(|| conflict("Voice action expired or requires confirmation"))?
            .clone();
        if lane.busy {
            return Err(conflict("Voice action already executing"));
        }
        lane.busy = true;
        p
    };
    let result=async{
        let _guard=app.gate.lock().await;
        let target=voice_target(&app,&sid).await?;
        let expected=proposal["target_run_id"].clone();
        if expected!=target.as_ref().map(|r|r["id"].clone()).unwrap_or(Value::Null)
            ||proposal["target_revision"]!=target.as_ref().map(|r|r["run_revision"].clone()).unwrap_or(Value::Null)
            ||proposal["mode_revision"]!=session_mode(&app,&sid).await?["revision"]{
            return Err(conflict("Run or mode changed; repeat the voice request"));
        }
        drop(_guard);
        let action=proposal["control"].as_str();
        if let Some(rid)=expected.as_str(){
            let Json(result)=command(State(app.clone()),Path((rid.to_owned(),action.unwrap_or("cancel").to_owned())),
                Json(json!({"client_request_id":format!("voice-{id}"),"expected_run_revision":proposal["target_revision"]}))).await?;
            if result["uncertain"]==true{return Err(conflict("Action outcome is uncertain; review recovery in chat before continuing"));}
            if action.is_some(){return Ok(Json(json!({"message":format!("Run {}.",action.unwrap()),"run":result,"ephemeral":true})));}
        }
        let (_,Json(sent))=send(State(app.clone()),Path(sid.clone()),Json(json!({"text":proposal["command"],"client_message_id":format!("voice-{id}")}))).await?;
        Ok(Json(json!({"message":"Sent to the work agent.","run_id":sent["run_id"],"ephemeral":false})))
    }.await;
    let mut all = lanes().lock().await;
    let lane = get_lane(&mut all, &k)?;
    lane.busy = false;
    if let Ok(Json(value)) = &result {
        if let Some(p) = lane.proposal.as_mut() {
            p["state"] = json!("accepted");
        }
        drop(all);
        app.store.put(&receipt_key, value).await?;
    }
    result
}

pub(crate) async fn voice_capabilities(State(app): State<App>, Path(sid): Path<String>) -> Api {
    let voice = media_lane::lane_run(&app, &sid, model_selection::ModelRole::Voice).await;
    let configured = if let Ok(run) = voice {
        media_lane::lane_selection(&app, &run).await.is_ok()
            && run["selection_plan"]["candidates"][0]["transport"] != "openai_subscription"
    } else {
        false
    };
    Ok(Json(
        json!({"voice_configured":configured,"streaming":true,"tts_available":std::env::var("V3_VOICE_TTS_API_KEY").is_ok_and(|key|!key.trim().is_empty()),"browser_fallback":true}),
    ))
}
fn speech_text(text: &str) -> String {
    let mut result = String::new();
    for (index, part) in text.split("\u{60}\u{60}\u{60}").enumerate() {
        if index % 2 == 0 {
            result.push_str(part);
        } else {
            result.push_str(" Code omitted. ");
        }
    }
    let mut redact_next = false;
    let result = result
        .split_whitespace()
        .map(|word| {
            let lower = word.to_ascii_lowercase();
            if redact_next {
                redact_next = false;
                return "[redacted]";
            }
            if lower == "bearer" {
                redact_next = true;
                return "[redacted]";
            }
            if lower.starts_with("http://") || lower.starts_with("https://") {
                return "link omitted";
            }
            if lower.starts_with("sk-") && word.len() > 12
                || lower.starts_with("api_key")
                || lower.starts_with("token=")
            {
                return "[redacted]";
            }
            word
        })
        .collect::<Vec<_>>()
        .join(" ");
    result.chars().take(1800).collect()
}
pub(crate) async fn voice_speech(
    State(app): State<App>,
    Path(sid): Path<String>,
    Json(body): Json<Value>,
) -> Api {
    let text = speech_text(required(&body, "text", 8192)?);
    if text.is_empty() {
        return Err(bad("Speech text is empty"));
    }
    if app.shutoff().await? {
        return Err(conflict("Global shutoff is active"));
    }
    // Optional installation-owned speech service; no v2 keys or client URLs/models.
    let key = std::env::var("V3_VOICE_TTS_API_KEY")
        .ok()
        .filter(|key| !key.trim().is_empty())
        .ok_or_else(|| {
            ApiError(
                StatusCode::SERVICE_UNAVAILABLE,
                "Server speech is unavailable; browser speech can be used".into(),
            )
        })?;
    let model = std::env::var("V3_VOICE_TTS_MODEL").unwrap_or_else(|_| "gpt-4o-mini-tts".into());
    let bytes = app
        .provider
        .speak(&key, &model, &text, app.stop.child_token())
        .await
        .map_err(|_| {
            ApiError(
                StatusCode::BAD_GATEWAY,
                "Server speech did not complete; browser speech can be used".into(),
            )
        })?;
    let agent = session_agent_id(&app, &sid).await?;
    if !app
        .store
        .live_access(app.store.current_user_id(), &agent)
        .await?
        .admitted()
    {
        return Err(conflict("Voice access changed"));
    }
    Ok(Json(
        json!({"data":STANDARD.encode(bytes),"media_type":"audio/mpeg","ephemeral":true}),
    ))
}
