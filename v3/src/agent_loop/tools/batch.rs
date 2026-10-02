//! A model turn may request several independent tools. Rust authorizes each
//! one; the Python worker only receives calls that passed its own gate.
use super::*;
use futures_util::{stream::FuturesUnordered, StreamExt};

fn denied(message:&str)->Value { json!({"status":"denied","output":message}) }

pub(super) fn executing_write(run:&Value)->bool {
    run["batch"].as_array().is_some_and(|items|items.iter().any(|item|
        item["state"]=="executing"
            && !tools::is_read(item["pending"]["name"].as_str().unwrap_or(""))))
}
pub(super) fn uncertain_count(run:&Value)->usize {
    run["batch"].as_array().map(|items|items.iter()
        .filter(|item|item["state"]=="uncertain").count()).unwrap_or(0)
}
pub(super) fn mark_uncertain_writes(run:&mut Value) {
    if let Some(items)=run["batch"].as_array_mut() {
        for item in items {if item["state"]=="executing"
            && !tools::is_read(item["pending"]["name"].as_str().unwrap_or("")) {
            item["state"]=json!("uncertain");
        }}
    }
}

// BATCH-DISPATCH: never serialize overflow calls. The config explicitly marks
// each name concurrent or reject; omitted names reject only in a multi-call turn.
pub(super) async fn execute_parallel_batch(
    app:&App,id:&str,generation:&str,cancel:&CancellationToken,
    workflow_policy:&workflow::Policy,access:&access_policy::LiveAccess,
    mut run:Value,guard:tokio::sync::MutexGuard<'_,()>,
)->Result<()> {
    let calls=run["calls"].as_array().ok_or_else(||anyhow!("Missing tool calls"))?.clone();
    let mut ids=std::collections::BTreeSet::new();
    if calls.iter().any(|call|call["call_id"].as_str().is_none_or(|id|id.is_empty() || !ids.insert(id.to_owned()))) {
        run["status"]=json!("failed");
        run["error"]=json!("Provider returned missing or duplicate tool call identifiers");
        app.save(&mut run).await?;
        return Ok(());
    }
    if run["batch"].is_null() {
        run["batch"]=json!(calls.iter().map(|_|json!({"state":"new","pending":null,
            "result":null,"pre_tool_receipt":null})).collect::<Vec<_>>());
    }
    if run["batch"].as_array().map(Vec::len)!=Some(calls.len()) {
        return Err(anyhow!("Batch checkpoint length mismatch"));
    }
    let config=loop_runtime::Config::read(&app.runtime_path)?;
    let sid=run["session_id"].as_str().ok_or_else(||anyhow!("Missing session"))?.to_owned();
    let agent_id=run["agent_id"].as_str().unwrap_or("shared_default").to_owned();
    let (policy,mode_revision,visibility,ability_state)=ability_controls(app,&sid).await?;
    let read_only=session_mode(app,&sid).await?["mode"]=="read_only";
    let saved_index=run["call_index"].as_u64().unwrap_or(0);
    let mut accepted=0usize;
    for (idx,model_call) in calls.iter().enumerate() {
        if run["batch"][idx]["state"]!="new" {continue;}
        let name=model_call["name"].as_str().unwrap_or("").to_owned();
        let args=match model_call["arguments"].as_str().map(serde_json::from_str::<Value>) {
            Some(Ok(v)) if v.is_object()=>v,
            _=>json!({"invalid_arguments":true}),
        };
        run["call_index"]=json!(idx);
        workflow::record(&mut run,workflow_policy,&workflow::Input::PreTool(workflow::PreToolInput {
            name:name.clone(),arguments_digest:digest(&args),
            policy_version:policy["version"].as_u64().unwrap_or(0),mode_revision,
            visibility_revision:visibility["revision"].as_u64().unwrap_or(0),
            ability_state_revision:ability_state["revision"].as_u64().unwrap_or(0),
        }),workflow::Decision::Pass)?;
        let receipt=run["workflow"]["receipts"]["pre_tool"].clone();
        let pending=json!({"id":uuid(),"name":name,"arguments":args,"call_id":model_call["call_id"],
            "digest":digest(&args),"policy_version":policy["version"],"mode_revision":mode_revision,
            "visibility_revision":visibility["revision"],"ability_state_revision":ability_state["revision"],
            "access_revision":access.revision.clone(),"state":"requested","decision":null});
        let mode=config.tools.parallel_modes.get(&name).map(String::as_str).unwrap_or("reject");
        let effective=live_tool_decision(app,access,&name,&policy,&visibility,
            &ability_state,&run["loaded_abilities"],read_only);
        let result=if mode!="concurrent" {
            Some(denied("Tool rejects parallel execution; request it in a separate turn"))
        } else if accepted>=config.tools.max_parallel_calls {
            Some(denied("Parallel tool-call limit exceeded; request this tool later"))
        } else if args.get("invalid_arguments").is_some()
            || effective.permission==access::Permission::Deny {
            Some(denied("Tool invocation denied by policy or invalid arguments"))
        } else {accepted+=1;None};
        let state=if result.is_some() {"done"}
            else if effective.permission==access::Permission::Ask {"waiting"}
            else {"requested"};
        run["batch"][idx]=json!({"state":state,"pending":pending,"result":result,
            "pre_tool_receipt":receipt});
    }
    run["call_index"]=json!(saved_index);

    // APPROVAL-IDENTITY: only the matching call consumes the administrator's
    // decision. Other calls retain distinct IDs, revisions, and arguments.
    if !run["pending"].is_null() {
        let approval_id=run["pending"]["id"].clone();
        for idx in 0..calls.len() {
            if run["batch"][idx]["pending"]["id"]!=approval_id {continue;}
            match run["pending"]["decision"].as_str() {
                Some("approve")=>{
                    run["batch"][idx]["pending"]["decision"]=json!("approve");
                    run["batch"][idx]["state"]=json!("requested");
                    run["pending"]=Value::Null;
                },
                Some("deny")=>{
                    run["batch"][idx]["result"]=denied("Tool approval denied");
                    run["batch"][idx]["state"]=json!("done");
                    run["pending"]=Value::Null;
                },
                _=>{},
            }
            break;
        }
    }

    // Approval requests are serialized for the existing API, but execution is
    // not. Wait for every independently approved call, then launch the batch.
    if let Some(item)=run["batch"].as_array().unwrap().iter().find(|item|item["state"]=="waiting") {
        run["pending"]=item["pending"].clone();
        run["pending"]["state"]=json!("waiting");
        run["status"]=json!("waiting_approval");
        let mut payload=run["pending"].clone();payload["run_id"]=json!(id);
        app.store.save_run(&mut run,"approval_required",&payload).await?;
        return Ok(());
    }
    let mut dispatch=Vec::new();
    let fresh_access=app.store.live_access(app.store.current_user_id(),&agent_id).await?;
    for idx in 0..calls.len() {
        if run["batch"][idx]["state"]!="requested" {continue;}
        let pending=run["batch"][idx]["pending"].clone();
        let name=pending["name"].as_str().unwrap_or("").to_owned();
        let args=pending["arguments"].clone();
        let effective=live_tool_decision(app,&fresh_access,&name,&policy,&visibility,
            &ability_state,&run["loaded_abilities"],read_only);
        let fresh=pending["policy_version"]==policy["version"]
            && pending["access_revision"]==json!(fresh_access.revision.clone())
            && pending["mode_revision"]==json!(mode_revision)
            && pending["visibility_revision"]==visibility["revision"]
            && pending["ability_state_revision"]==ability_state["revision"];
        if !fresh || config.tools.parallel_modes.get(&name).map(String::as_str)!=Some("concurrent")
            || effective.permission==access::Permission::Deny
            || (effective.permission==access::Permission::Ask && pending["decision"]!="approve") {
            run["batch"][idx]["result"]=denied("Tool authorization changed before parallel dispatch");
            run["batch"][idx]["state"]=json!("done");
            continue;
        }
        // PRE-TOOL-LEASE: an approved call may resume under a new generation.
        // Bind its receipt to the exact dispatch that is about to occur.
        run["call_index"]=json!(idx);
        workflow::record(&mut run,workflow_policy,&workflow::Input::PreTool(workflow::PreToolInput {
            name:name.clone(),arguments_digest:digest(&args),
            policy_version:policy["version"].as_u64().unwrap_or(0),mode_revision,
            visibility_revision:visibility["revision"].as_u64().unwrap_or(0),
            ability_state_revision:ability_state["revision"].as_u64().unwrap_or(0),
        }),workflow::Decision::Pass)?;
        run["batch"][idx]["pre_tool_receipt"]=run["workflow"]["receipts"]["pre_tool"].clone();
        run["call_index"]=json!(saved_index);
        run["batch"][idx]["state"]=json!("executing");
        run["batch"][idx]["pending"]["state"]=json!("executing");
        run["batch"][idx]["pending"]["decision"]=json!("consumed");
        dispatch.push((idx,name,args,pending["id"].as_str().unwrap_or("").to_owned(),
            pending["policy_version"].as_u64().unwrap_or(0)));
    }
    app.save(&mut run).await?;
    if !dispatch.is_empty() {
        drop(guard);
        let mut futures=FuturesUnordered::new();
        for (idx,name,args,pending_id,policy_version) in dispatch {
            let task_app=app.clone();let task_sid=sid.clone();let task_run=id.to_owned();
            let task_cancel=cancel.clone();
            futures.push(async move {
                let result=tools::call(task_app,task_sid,task_run,name.clone(),args,
                    pending_id.clone(),policy_version,task_cancel).await;
                (idx,name,pending_id,result)
            });
        }
        while let Some((idx,name,pending_id,result))=futures.next().await {
            let _result_guard=app.gate.lock().await;
            let mut current=app.run(id).await?;
            if current["generation"]!=generation
                || current["batch"][idx]["pending"]["id"]!=pending_id {continue;}
            let interrupted=match &result {
                Ok(value)=>matches!(value["status"].as_str(),Some("cancelled"|"timeout"|"uncertain")),
                Err(_)=>true,
            };
            if interrupted && !tools::is_read(&name) {
                current["batch"][idx]["state"]=json!("uncertain");
                current["uncertain"]=json!(true);
                current["status"]=json!("recovery");
                current["error"]=json!("Parallel write outcome unknown; review each call");
            } else {
                current["batch"][idx]["result"]=match result {
                    Ok(value)=>value,
                    Err(_)=>json!({"status":"error","output":"Read tool interrupted"}),
                };
                current["batch"][idx]["state"]=json!("done");
                if current["status"]=="recovery" && uncertain_count(&current)==0 {
                    current["uncertain"]=json!(false);
                    current["status"]=json!("stopped");
                }
            }
            // STORAGE-RESULT-COMMIT: a completed write is durable before the
            // next future is awaited, so recovery only reviews unresolved calls.
            app.save(&mut current).await?;
        }
        return Ok(());
    }
    if run["batch"].as_array().unwrap().iter().all(|item|item["state"]=="done") {
        // STORAGE-ORDER: save one output at a time. The store indexes the last
        // appended output of each save; call_index makes restart idempotent.
        for idx in saved_index as usize..calls.len() {
            let item=run["batch"][idx].clone();
            run["pending"]=item["pending"].clone();
            run["workflow"]["receipts"]["pre_tool"]=item["pre_tool_receipt"].clone();
            append_tool(&mut run,&item["result"])?;
            let call_id=item["pending"]["call_id"].as_str().unwrap();
            let result_receipt=run["workflow"]["receipts"]["tool_result"].clone();
            run["workflow"]["batch_receipts"][call_id]=json!({
                "pre_tool":item["pre_tool_receipt"],"tool_result":result_receipt,
            });
            app.store.save_run(&mut run,"tool_result",
                &json!({"run_id":id,"name":item["pending"]["name"],"result":item["result"]})).await?;
        }
        run["batch"]=Value::Null;
        run["phase"]=json!("model");
        app.save(&mut run).await?;
        return Ok(());
    }
    Ok(())
}
