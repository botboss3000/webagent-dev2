//! Public capability metadata is descriptive, never an entitlement or execution grant.
use serde_json::{json, Value};
use super::ProviderId;

fn modalities(row: &Value, direction: &str) -> Vec<String> {
    let key = format!("{direction}_modalities");
    let value = row.get("architecture").and_then(|a| a.get(&key))
        .or_else(|| row.get(&key)).or_else(|| row.get("modalities").and_then(|m| m.get(direction)));
    if let Some(items) = value.and_then(Value::as_array) {
        return items.iter().filter_map(Value::as_str).map(str::to_lowercase).collect();
    }
    if let Some(m) = row["architecture"]["modality"].as_str().or_else(|| row["modality"].as_str()) {
        let (input, output) = m.split_once("->").unwrap_or((m, ""));
        return (if direction == "input" { input } else { output }).split('+').map(str::to_lowercase).collect();
    }
    Vec::new()
}

pub(super) fn normalize(id: &str, row: &Value, fallback: &Value) -> Value {
    let mut input = modalities(row, "input");
    let mut output = modalities(row, "output");
    // Provider metadata wins; the public catalog fills missing directions only.
    if input.is_empty() { input = modalities(fallback, "input"); }
    if output.is_empty() { output = modalities(fallback, "output"); }
    let has = |items: &[String], name: &str| items.iter().any(|m| m == name);
    let methods = row["supportedGenerationMethods"].as_array();
    let method = |name: &str| methods.is_some_and(|items| items.iter().any(|m| m.as_str() == Some(name)));
    let lower = id.to_lowercase();
    let image_hint = ["dall-e", "dalle", "gpt-image", "flux", "sdxl", "stable-diffusion", "stable-image", "sd3", "imagen", "playground", "diffus"].iter().any(|h| lower.contains(h));
    let known = !input.is_empty() || !output.is_empty() || methods.is_some();
    let name = row["display_name"].as_str().or_else(|| row["displayName"].as_str()).or_else(|| row["name"].as_str()).unwrap_or(id);
    let mut result = json!({"id":id,"name":name,"display_name":name,
        "text_capable": row["text_capable"].as_bool().unwrap_or_else(|| if !output.is_empty() { has(&output,"text") } else { !image_hint || method("generateContent") }),
        "image_capable": row["image_capable"].as_bool().unwrap_or(false) || has(&input,"image") || has(&input,"vision"),
        "image_out_capable": row["image_out_capable"].as_bool().unwrap_or(false) || has(&output,"image") || method("generateImages") || (output.is_empty() && image_hint),
        "voice_capable": row["voice_capable"].as_bool().unwrap_or(false) || has(&input,"audio") || has(&output,"audio"),
        "video_capable": row["video_capable"].as_bool().unwrap_or(false) || has(&input,"video") || has(&output,"video"),
        "modality_known":known,"capabilities_verified":false});
    for (key, live, cached) in [
        ("context", row.get("context_length").or_else(||row.get("context_window")), fallback.get("limit").and_then(|v|v.get("context"))),
        ("max_output", row.get("max_output_tokens"), fallback.get("limit").and_then(|v|v.get("output"))),
        ("description", row.get("description"), fallback.get("description")),
    ] { if let Some(v) = live.or(cached).filter(|v| v.is_number() || v.is_string()) { result[key] = v.clone(); } }
    result
}

// Fixed public endpoints carry no provider credentials. Failures degrade to live metadata.
#[cfg(not(test))]
pub(super) async fn metadata(client: &reqwest::Client) -> (Value, Value) {
    use futures_util::StreamExt;
    use std::{sync::OnceLock, time::{Duration, Instant}};
    static CACHE: OnceLock<tokio::sync::Mutex<Option<(Instant, Value, Value)>>> = OnceLock::new();
    let mut cache = CACHE.get_or_init(||tokio::sync::Mutex::new(None)).lock().await;
    if let Some((at,a,b)) = &*cache { if at.elapsed() < Duration::from_secs(3600) { return (a.clone(),b.clone()); } }
    async fn fetch(client: &reqwest::Client, url: &str) -> Option<Value> {
        let read = async {
            let response = client.get(url).send().await.ok()?.error_for_status().ok()?;
            let mut stream = response.bytes_stream(); let mut bytes = Vec::new();
            while let Some(chunk) = stream.next().await {
                let chunk = chunk.ok()?;
                if bytes.len() + chunk.len() > 16 * 1024 * 1024 { return None; }
                bytes.extend_from_slice(&chunk);
            }
            serde_json::from_slice(&bytes).ok()
        };
        tokio::time::timeout(Duration::from_secs(8), read).await.ok().flatten()
    }
    let (a,b) = tokio::join!(fetch(client,"https://models.dev/api.json"),fetch(client,"https://openrouter.ai/api/v1/models"));
    let old = cache.as_ref();
    let a = a.unwrap_or_else(||old.map(|(_,a,_)|a.clone()).unwrap_or(Value::Null));
    let b = b.unwrap_or_else(||old.map(|(_,_,b)|b.clone()).unwrap_or(Value::Null));
    *cache = Some((Instant::now(),a.clone(),b.clone())); (a,b)
}
#[cfg(test)]
pub(super) async fn metadata(_: &reqwest::Client) -> (Value, Value) { (Value::Null,Value::Null) }

pub(super) fn lookup(id: &str, provider: ProviderId, catalogs: &(Value,Value)) -> Value {
    let hint = match provider { ProviderId::OpenaiSubscription|ProviderId::OpenaiApi=>"openai",ProviderId::Gemini=>"google",ProviderId::Deepseek=>"deepseek",ProviderId::Mistral=>"mistral",ProviderId::Xai=>"xai",_=>"" };
    let bare = id.rsplit('/').next().unwrap_or(id);
    let direct = catalogs.0[hint]["models"].get(id).or_else(||catalogs.0[hint]["models"].get(bare));
    // Gateways use vendor prefixes; only unique bare matches are safe fallbacks.
    let mut matches = Vec::new();
    if let Some(providers) = catalogs.0.as_object() {
        for provider in providers.values() {
            if let Some(models) = provider["models"].as_object() {
                for (key, value) in models { if key == id || key == bare { matches.push(value); } }
            }
        }
    }
    let mut result = direct.or_else(|| if matches.len() == 1 { Some(matches[0]) } else { None }).cloned().unwrap_or(Value::Null);
    let router = catalogs.1["data"].as_array().and_then(|rows|rows.iter().find(|row|row["id"] == id));
    if let Some(router) = router {
        if result.is_null() { return router.clone(); }
        // Keep models.dev as the backbone, filling absent modalities from OpenRouter.
        for direction in ["input","output"] {
            if modalities(&result,direction).is_empty() {
                let mods = modalities(router,direction);
                result["modalities"][direction] = json!(mods);
            }
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn structured_vision_image_voice_video_and_method_discovery() {
        let row = normalize("multimodal",&json!({"architecture":{"input_modalities":["text","image","video"],"output_modalities":["text","image","audio"]}}),&Value::Null);
        for key in ["text_capable","image_capable","image_out_capable","voice_capable","video_capable","modality_known"] { assert_eq!(row[key],true); }
        let image = normalize("imagen-3",&json!({"supportedGenerationMethods":["generateImages"]}),&Value::Null);
        assert_eq!(image["image_out_capable"],true); assert_eq!(image["text_capable"],false);
    }
    #[test] fn enrichment_fills_gaps_without_overwriting_provider_modalities() {
        let fallback = json!({"modalities":{"input":["text","image"],"output":["text","image"]},"limit":{"context":123}});
        let row = normalize("bare",&json!({}),&fallback);
        assert_eq!(row["image_capable"],true);assert_eq!(row["context"],123);
        let live = normalize("bare",&json!({"input_modalities":["text"],"output_modalities":["text"]}),&fallback);
        assert_eq!(live["image_capable"],false);assert_eq!(live["image_out_capable"],false);
        assert_eq!(normalize("gpt-image-1",&json!({}),&Value::Null)["image_out_capable"],true);
    }
}
