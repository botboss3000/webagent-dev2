//! Installation-owned audio routes. Never accept a URL or credentials from a browser payload.
use super::*;
use base64::{engine::general_purpose::STANDARD, Engine};
impl Provider {
    async fn media_json(
        &self,
        request: reqwest::RequestBuilder,
        cancel: CancellationToken,
    ) -> Result<Value> {
        let response = tokio::select! {biased;_=cancel.cancelled()=>bail!("Media request cancelled"),
        result=tokio::time::timeout(Duration::from_secs(90),request.send())=>result.map_err(|_|anyhow!("Media timeout"))?.map_err(|_|anyhow!("Media connection failed"))?};
        if !response.status().is_success() {
            return Err(response_failure(response, &cancel).await);
        }
        let mut stream = response.bytes_stream();
        let mut bytes = Vec::new();
        loop {
            let chunk = tokio::select! {biased;_=cancel.cancelled()=>bail!("Media request cancelled"),
            result=tokio::time::timeout(Duration::from_secs(30),stream.next())=>result.map_err(|_|anyhow!("Media timeout"))?};
            let Some(chunk) = chunk else { break };
            let chunk = chunk.map_err(|_| anyhow!("Media response interrupted"))?;
            if bytes.len() + chunk.len() > 128 * 1024 {
                bail!("Media response exceeded limit");
            }
            bytes.extend_from_slice(&chunk);
        }
        serde_json::from_slice(&bytes).map_err(|_| anyhow!("Invalid media response"))
    }
    pub async fn speak(
        &self,
        key: &str,
        model: &str,
        text: &str,
        cancel: CancellationToken,
    ) -> Result<Vec<u8>> {
        if !matches!(model, "tts-1" | "tts-1-hd" | "gpt-4o-mini-tts")
            || text.is_empty()
            || text.len() > 8192
        {
            bail!("Invalid speech configuration");
        }
        let fixed = "https://api.openai.com/v1/audio/speech";
        #[cfg(not(test))]
        let endpoint = fixed;
        #[cfg(test)]
        let endpoint = self.responses_endpoint.as_deref().unwrap_or(fixed);
        let request = self
            .client
            .post(endpoint)
            .bearer_auth(key)
            .json(&json!({"model":model,"input":text,"voice":"alloy","response_format":"mp3"}));
        let response = tokio::select! {biased;_=cancel.cancelled()=>bail!("Speech cancelled"),result=tokio::time::timeout(Duration::from_secs(75),request.send())=>result.map_err(|_|anyhow!("Speech timeout"))?.map_err(|_|anyhow!("Speech connection failed"))?};
        if !response.status().is_success() {
            return Err(response_failure(response, &cancel).await);
        }
        let mut stream = response.bytes_stream();
        let mut bytes = Vec::new();
        loop {
            let chunk = tokio::select! {biased;_=cancel.cancelled()=>bail!("Speech cancelled"),result=tokio::time::timeout(Duration::from_secs(20),stream.next())=>result.map_err(|_|anyhow!("Speech timeout"))?};
            let Some(chunk) = chunk else { break };
            let chunk = chunk.map_err(|_| anyhow!("Speech interrupted"))?;
            if bytes.len() + chunk.len() > 4 * 1024 * 1024 {
                bail!("Speech response exceeded limit");
            }
            bytes.extend_from_slice(&chunk);
        }
        if bytes.is_empty() {
            bail!("Speech response empty");
        }
        Ok(bytes)
    }
    pub async fn transcribe(
        &self,
        selection: &ProviderSelection,
        tokens: &Value,
        bytes: &[u8],
        mime: &str,
        cancel: CancellationToken,
    ) -> Result<String> {
        let format = match mime {
            "audio/webm" => "webm",
            "audio/ogg" => "ogg",
            "audio/mp4" => "mp4",
            "audio/mpeg" | "audio/mp3" => "mp3",
            "audio/wav" | "audio/x-wav" => "wav",
            "audio/flac" => "flac",
            _ => bail!("Unsupported audio recording"),
        };
        if bytes.is_empty() || bytes.len() > 10 * 1024 * 1024 {
            bail!("Recording exceeds limit");
        }
        let base = selection
            .provider
            .chat_endpoint()
            .and_then(|p| p.strip_suffix("chat/completions"))
            .ok_or_else(|| anyhow!("Voice requires an audio-capable API connection"))?;
        let transcription_model = selection.model.to_ascii_lowercase().contains("whisper")
            || selection.model.to_ascii_lowercase().contains("transcrib");
        let (fixed, body) = if selection.provider == ProviderId::Openrouter {
            let audio = json!({"data":STANDARD.encode(bytes),"format":format});
            if transcription_model {
                (
                    format!("{base}audio/transcriptions"),
                    json!({"model":selection.model,"input_audio":audio}),
                )
            } else {
                (
                    format!("{base}chat/completions"),
                    json!({"model":selection.model,"stream":false,"max_tokens":2048,"messages":[{"role":"user","content":[{"type":"text","text":"Transcribe the attached recording verbatim. Return only the transcript, with no commentary."},{"type":"input_audio","input_audio":audio}]}]}),
                )
            }
        } else {
            (format!("{base}audio/transcriptions"), Value::Null)
        };
        #[cfg(not(test))]
        let endpoint = fixed.as_str();
        #[cfg(test)]
        let endpoint = self.responses_endpoint.as_deref().unwrap_or(&fixed);
        let request = self
            .client
            .post(endpoint)
            .bearer_auth(required(tokens, "api_key")?);
        let request = if selection.provider == ProviderId::Openrouter {
            request.json(&body)
        } else {
            let boundary = format!("WebAgent{}", uuid::Uuid::new_v4().simple());
            let mut data=format!("--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{}\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.{format}\"\r\nContent-Type: {mime}\r\n\r\n",selection.model).into_bytes();
            data.extend_from_slice(bytes);
            data.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
            request
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .body(data)
        };
        let response = self.media_json(request, cancel).await?;
        let text = if let Some(text) = response["text"].as_str() {
            text.to_owned()
        } else {
            let content = &response["choices"][0]["message"]["content"];
            content.as_str().map(str::to_owned).unwrap_or_else(|| {
                content
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|p| p["text"].as_str())
                    .collect::<Vec<_>>()
                    .join("")
            })
        };
        if text.len() > 8192 {
            bail!("Transcript exceeded limit");
        }
        Ok(text.trim().to_owned())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use axum::{routing::post, Router};
    #[tokio::test]
    async fn transcription_is_fixed_bounded_and_ephemeral() {
        let server = Router::new().route(
            "/audio",
            post(
                |headers: axum::http::HeaderMap, body: axum::body::Bytes| async move {
                    assert_eq!(headers["authorization"], "Bearer audio-key");
                    assert!(headers["content-type"]
                        .to_str()
                        .unwrap()
                        .starts_with("multipart/form-data"));
                    assert!(String::from_utf8_lossy(&body).contains("whisper-1"));
                    axum::Json(json!({"text":"What is the run status?"}))
                },
            ),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let task = tokio::spawn(async move { axum::serve(listener, server).await.unwrap() });
        let provider = Provider::for_test(format!("http://{address}/audio"));
        let selection = ProviderSelection {
            provider: ProviderId::OpenaiApi,
            model: "whisper-1".into(),
        };
        assert_eq!(
            provider
                .transcribe(
                    &selection,
                    &json!({"api_key":"audio-key"}),
                    b"wave",
                    "audio/wav",
                    CancellationToken::new()
                )
                .await
                .unwrap(),
            "What is the run status?"
        );
        assert!(provider
            .transcribe(
                &selection,
                &json!({"api_key":"audio-key"}),
                b"wave",
                "text/html",
                CancellationToken::new()
            )
            .await
            .is_err());
        let cancel = CancellationToken::new();
        cancel.cancel();
        assert!(provider
            .transcribe(
                &selection,
                &json!({"api_key":"audio-key"}),
                b"wave",
                "audio/wav",
                cancel
            )
            .await
            .is_err());
        task.abort();
    }
}
