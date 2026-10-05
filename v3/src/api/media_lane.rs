//! Bounded ephemeral specialized-model calls. No audio or quick answers enter durable chat.
use super::*;
use tokio::sync::mpsc;
pub(crate) async fn lane_run(
    app: &App,
    sid: &str,
    role: model_selection::ModelRole,
) -> Result<Value, ApiError> {
    if app.shutoff().await? {
        return Err(conflict("Global shutoff is active"));
    }
    let session = app
        .store
        .get(&key("session", sid))
        .await?
        .ok_or_else(|| bad("Session not found"))?;
    if session["status"] == "recycled" {
        return Err(conflict("Restore this conversation first"));
    }
    let agent = session_agent_id(app, sid).await?;
    let mut plan = app
        .store
        .model_catalog()
        .await?
        .resolve_role(&agent, role)
        .map_err(|_| conflict("Configure and connect the required model role for this agent"))?;
    app.pin_model_plan(&mut plan)
        .await
        .map_err(|_| conflict("Required model is unavailable"))?;
    Ok(
        json!({"agent_id":agent,"session_id":sid,"selection_plan":plan,"failover_state":model_selection::FailoverState::default()}),
    )
}
pub(crate) async fn lane_selection(
    app: &App,
    run: &Value,
) -> Result<(provider::ProviderSelection, Value), ApiError> {
    let candidate = app
        .run_candidate(run)
        .await
        .map_err(|_| conflict("Model access changed"))?
        .ok_or_else(|| conflict("Model unavailable"))?;
    let selection = provider::ProviderSelection {
        provider: candidate.transport.provider_id(),
        model: candidate.upstream_model,
    };
    let tokens = app
        .tokens_for(&selection, run)
        .await
        .map_err(|_| conflict("Connect the required model provider"))?;
    Ok((selection, tokens))
}
pub(crate) async fn lane_answer(
    app: &App,
    run: &Value,
    input: Vec<Value>,
    instructions: &str,
) -> Result<String, ApiError> {
    let (selection, tokens) = lane_selection(app, run).await?;
    let (tx, mut rx) = mpsc::channel(16);
    let drain = tokio::spawn(async move { while rx.recv().await.is_some() {} });
    let result = tokio::time::timeout(
        Duration::from_secs(90),
        app.provider.complete_selected_with_instructions(
            &selection,
            &tokens,
            input,
            vec![],
            run["session_id"].as_str().unwrap(),
            instructions,
            tx,
            app.stop.child_token(),
        ),
    )
    .await;
    drain.abort();
    let result = result
        .map_err(|_| {
            ApiError(
                StatusCode::GATEWAY_TIMEOUT,
                "Voice or image model timed out".into(),
            )
        })?
        .map_err(|_| {
            ApiError(
                StatusCode::BAD_GATEWAY,
                "Voice or image model did not complete; check its assignment and connection".into(),
            )
        })?;
    lane_selection(app, run).await?;
    let answer = result["output"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|item| item["type"] == "message")
        .flat_map(|item| item["content"].as_array().into_iter().flatten())
        .filter_map(|part| part["text"].as_str())
        .collect::<Vec<_>>()
        .join("\n");
    if answer.trim().is_empty() || answer.len() > 32768 {
        return Err(ApiError(
            StatusCode::BAD_GATEWAY,
            "Model returned no bounded answer".into(),
        ));
    }
    Ok(answer)
}

pub(crate) async fn limit(request: Request, next: Next) -> Response {
    static SLOTS: std::sync::OnceLock<Semaphore> = std::sync::OnceLock::new();
    let Ok(_permit) = SLOTS.get_or_init(|| Semaphore::new(4)).try_acquire() else {
        return ApiError(
            StatusCode::TOO_MANY_REQUESTS,
            "Media is busy; try again shortly".into(),
        )
        .into_response();
    };
    next.run(request).await
}
