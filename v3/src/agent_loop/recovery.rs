//! Reopen unfinished runs after interruption; hold uncertain writes for review.
use super::*;
use anyhow::bail;
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
fn pending_build_finish(run:&Value)->bool {
    run["pending"]["name"]=="v3_build_work"
        && run["pending"]["arguments"]["action"]=="finish"
        && run["pending"]["state"]=="executing"
}
pub(crate) fn attach_external_build_result(run:&mut Value,result:&Value)->Result<()> {
    if status(run)!="waiting_external" || !pending_build_finish(run)
        || run["pending"]["external_job"]["kind"]!="v3_build" {
        bail!("No pending external build result");
    }
    append_tool(run,result)?;
    run["status"]=json!("queued");
    run["error"]=Value::Null;
    Ok(())
}
async fn build_result(app:&App,run:&Value)->Result<Option<Value>> {
    let sid=run["session_id"].as_str().ok_or_else(||anyhow!("Missing session"))?;
    let rid=run["id"].as_str().ok_or_else(||anyhow!("Missing run"))?;
    let task=run["pending"]["arguments"]["task_id"].as_str()
        .ok_or_else(||anyhow!("Missing build task"))?;
    let fingerprint=tools::administrator::host_fingerprint(app,sid,rid,"v3_build_work_finish").await?;
    let trusted=json!({"user_id":app.store.current_user_id(),"session_id":sid,
        "lease_until":now()+30,"access_fingerprint":fingerprint});
    let socket=std::path::Path::new("/run/v3/build/build.sock");
    let mut job_id=run["pending"]["external_job"]["job_id"].as_str().map(str::to_owned);
    if job_id.is_none() {
        // The broker may have admitted finish just before the loop stopped.
        // Resolve its task receipt; never submit another finish.
        let status=tools::execute_trusted("v3_build_work",&json!({"action":"status","task_id":task}),sid,
            socket,Some(trusted.clone()),CancellationToken::new()).await?;
        if status["status"]!="success" {bail!("Build task receipt unavailable");}
        let item=status["output"]["own"].as_array().and_then(|items|items.iter()
            .find(|item|item["task_id"]==task));
        let Some(item)=item else {
            return Ok(Some(json!({"status":"error","output":{
                "task_id":task,"status":"not_submitted","reason":"Task receipt unavailable; no build was submitted"}})));
        };
        if item["inputs_sha256"]!=run["pending"]["arguments"]["expected_inputs_sha256"]
            && item["status"]=="closed" {bail!("Build task digest changed");}
        job_id=item["job_id"].as_str().map(str::to_owned);
        if job_id.is_none() && item["status"]=="open" {
            return Ok(Some(json!({"status":"error","output":{
                "task_id":task,"status":"not_submitted","reason":"Task remains open; no build was submitted"}})));
        }
        if job_id.is_none() {return Ok(None);}
    }
    let job_id=job_id.unwrap();
    let response=tools::execute_trusted("v3_worker_job",&json!({"job_id":job_id}),sid,
        socket,Some(trusted.clone()),CancellationToken::new()).await?;
    if response["status"]!="success" {bail!("Build job receipt unavailable");}
    let job=&response["output"];
    let state=job["status"].as_str().unwrap_or("");
    if !matches!(state,"completed"|"failed"|"stale"|"cancelled"|"uncertain"|"interrupted") {
        return Ok(None);
    }
    let mut output=json!({"task_id":task,"job_id":job_id,"status":state,
        "phase":job["phase"],"reason":job["reason"],"log":job["log"]});
    if state=="completed" {
        let health=tools::execute_trusted("v3_build_status",&json!({}),sid,
            socket,Some(trusted),CancellationToken::new()).await?;
        if health["status"]!="success" {return Ok(None);}
        let containers=&health["output"]["containers"];
        output["containers"]=containers.clone();
        output["verified_healthy"]=json!(["api","loop"].iter().all(|role|
            containers[*role]["state"]=="running" && containers[*role]["health"]=="healthy"));
    }
    Ok(Some(json!({"status":"success","output":output})))
}
// After a restart, put safe unfinished work back in the queue. An interrupted
// external write is held for review because its effect may already have happened.
pub(crate) async fn recover(app: &App, startup: bool) -> Result<()> {
    let _guard = app.gate.lock().await;
    // Only boot recovery obeys the optional resume control. Uncertain effects
    // are always classified, and ordinary lease recovery retains its behavior.
    let resume = if !startup {true} else {
        match crate::services::startup_resume_enabled(&app.store).await {
            Ok(enabled) => enabled,
            Err(_) => {
                tracing::error!("Startup resume configuration unavailable; safe interrupted runs remain stopped");
                false
            }
        }
    };
    for user_app in user_apps(app).await? {
    for view in user_app.store.active_runs().await? {
        let id = view["id"].as_str().unwrap();
        if !matches!(status(&view),"running"|"waiting_external")
            || (!startup && app.active.lock().await.contains_key(id)) {
            continue;
        }
        let mut run = user_app.run(id).await?;
        if status(&run)=="waiting_external" || (status(&run)=="running" && pending_build_finish(&run)) {
            if status(&run)=="running" {
                run["pending"]["external_job"]=json!({"kind":"v3_build",
                    "task_id":run["pending"]["arguments"]["task_id"],
                    "expected_inputs_sha256":run["pending"]["arguments"]["expected_inputs_sha256"],
                    "submitted_at":now()});
                run["status"]=json!("waiting_external");
                run["error"]=Value::Null;
                user_app.save_override(&mut run).await?;
            }
            if !resume || app.shutoff().await? || !user_app.run_model_authorized(&run).await {
                run["status"]=json!("stopped");
                run["error"]=json!("Build result awaits authorized resume");
                user_app.save_override(&mut run).await?;
                continue;
            }
            match tokio::time::timeout(Duration::from_secs(3),build_result(&user_app,&run)).await {
                Ok(Ok(Some(result)))=>{
                    attach_external_build_result(&mut run,&result)?;
                    user_app.store.save_run_override(&mut run,"tool_result",
                        &json!({"run_id":id,"name":"v3_build_work","result":result})).await?;
                },
                Ok(Err(error))=>{
                    tracing::warn!(run_id=%id,"Build receipt reconciliation deferred: {error}");
                },
                _=>{},
            }
            continue;
        }
        if status(&run)!="running" {continue;}
        if (run["pending"]["state"] == "executing"
            && !tools::is_read(run["pending"]["name"].as_str().unwrap_or("")))
            || parallel_batch::executing_write(&run)
        {
            run["status"] = json!("recovery");
            run["uncertain"] = json!(true);
            parallel_batch::mark_uncertain_writes(&mut run);
            run["error"] = json!("Tool outcome is uncertain after interruption");
        } else {
                let allowed=user_app.run_model_authorized(&run).await;
            run["status"] = json!(if !resume || app.shutoff().await? || !allowed {
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
            run["error"] = json!(if !resume {"Automatic startup resume is disabled; explicit resume required"} else {"Recovered after interruption"});
        }
        user_app.save_override(&mut run).await?;
    }
    }
    Ok(())
}
