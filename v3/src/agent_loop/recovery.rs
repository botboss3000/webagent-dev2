//! Reopen unfinished runs after interruption; hold uncertain writes for review.
use super::*;
pub(crate) fn reconcile_late_tool_result(run: &mut Value, result: Result<Value>) -> Result<bool> {
    if run["uncertain"] != true { return Ok(false); }
    let value = match result {
        Ok(value) if !matches!(value["status"].as_str(),
            Some("cancelled" | "timeout" | "uncertain")) => value,
        _ => return Ok(false),
    };
    append_tool(run, &value)?;
    run["uncertain"] = json!(false);
    if status(run) == "recovery" {
        run["status"] = json!(if run["error"] == "Cancelled by user" {
            "cancelled"
        } else {
            "stopped"
        });
    }
    Ok(true)
}

pub(crate) async fn user_apps(app:&App) -> Result<Vec<App>> {
    let mut views=Vec::new();
    for user_id in app.store.all_user_ids().await? {
        let mut view=app.clone();
        view.store=app.store.for_user(&user_id).await?;
        views.push(view);
    }
    Ok(views)
}
// After a restart, put safe unfinished work back in the queue. An interrupted
// external write is held for review because its effect may already have happened.
pub(crate) async fn recover(app: &App, startup: bool) -> Result<()> {
    let _guard = app.gate.lock().await;
    for user_app in user_apps(app).await? {
    for view in user_app.store.active_runs().await? {
        let id = view["id"].as_str().unwrap();
        if status(&view) != "running" || (!startup && app.active.lock().await.contains_key(id)) {
            continue;
        }
        let mut run = user_app.run(id).await?;
        if (run["pending"]["state"] == "executing"
            && !tools::is_read(run["pending"]["name"].as_str().unwrap_or("")))
            || parallel_batch::executing_write(&run)
        {
            run["status"] = json!("recovery");
            run["uncertain"] = json!(true);
            parallel_batch::mark_uncertain_writes(&mut run);
            run["error"] = json!("Tool outcome is uncertain after interruption");
        } else {
            let agent_id=run["agent_id"].as_str().unwrap_or("shared_default");
            let model=run["provider_selection"]["model"].as_str().unwrap_or("");
            let allowed=user_app.store.live_access(user_app.store.current_user_id(),agent_id).await
                .is_ok_and(|access|access.admitted()
                    && (model.is_empty() || access.model_allowed(model)));
            run["status"] = json!(if app.shutoff().await? || !allowed {
                "stopped"
            } else {
                "queued"
            });
            if run["pending"]["state"] == "executing" {
                run["pending"]["state"] = json!("requested");
            }
            if let Some(items)=run["batch"].as_array_mut() {
                for item in items { if item["state"]=="executing" {item["state"]=json!("requested");item["pending"]["state"]=json!("requested");} }
            }
            run["error"] = json!("Recovered after interruption");
        }
        user_app.save_override(&mut run).await?;
    }
    }
    Ok(())
}
