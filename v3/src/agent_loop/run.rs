use super::*;
// Recheck all context authority after credential retrieval, under the dispatch gate.
async fn dispatch_asset_key(app:&App,sid:&str,agent_id:&str,loaded:&Value)->Result<String> {
    let access=app.store.live_access(app.store.current_user_id(),agent_id).await?;
    let (policy,_,visibility,state)=ability_controls(app,sid).await?;
    let mode=session_mode(app,sid).await?;
    Ok(digest(&json!([app.abilities.live_version(),policy,visibility,state,loaded,
        mode,access.context_fingerprint()])))
}
pub(crate) async fn execute_run(
    app: App,
    id: String,
    generation: String,
    cancel: CancellationToken,
) -> Result<()> {
    let mut context_accounting = context_accounting::ContextAccounting::default();
    // CONTEXT-ASSET-CACHE: one entry per executing run, never shared across callers.
    // Fresh live access and all shaping controls below invalidate this derived data;
    // actual tool execution still performs its own live authorization checks.
    let mut context_assets: Option<(String, Vec<Value>, String)> = None;
    loop {
        let _guard = app.gate.lock().await;
        let mut run = app.run(&id).await?;
        if run["generation"] != generation || status(&run) != "running" || cancel.is_cancelled() {
            return Ok(());
        }
        // Apply a composer choice before validating the next dispatch. This never
        // cancels/replays the model response or tool effects from the previous call.
        if run["phase"]=="model" {
            match apply_chat_model_preference(&app,&mut run).await {
                Ok(true)=>{context_accounting.reset();app.save(&mut run).await?;}
                Ok(false)=>{},
                Err(_)=>{run["status"]=json!("paused");run["error"]=json!("Selected model unavailable; choose a permitted connected model and resume");app.save(&mut run).await?;return Ok(());}
            }
        }
        let agent_id=run["agent_id"].as_str().unwrap_or("shared_default").to_owned();
        let access=app.store.live_access(app.store.current_user_id(),&agent_id).await;
        if access.as_ref().map_or(true, |access| !access.admitted()) || !app.run_model_authorized(&run).await {
            run["status"]=json!("stopped");
            run["error"]=json!("Access changed; run stopped");
            app.save(&mut run).await?;
            return Ok(());
        }
        let access=access.unwrap();
        let mut app=app.clone();
        app.store=match app.store.for_agent(&agent_id).await {
            Ok(store)=>store,
            Err(_)=>{
                run["status"]=json!("paused");
                run["error"]=json!("Agent storage unavailable; review required");
                app.save(&mut run).await?;
                return Ok(());
            }
        };
        if app.shutoff().await? {
            run["status"] = json!("stopped");
            app.save(&mut run).await?;
            return Ok(());
        }
        let sid = run["session_id"].as_str().unwrap().to_owned();
        let workflow_policy = match current_workflow_policy(&app).await {
            Ok(policy) => policy,
            Err(_) => {
                run["status"] = json!("paused");
                run["error"] = json!("Invalid workflow policy; administrator review required");
                app.save(&mut run).await?;
                return Ok(());
            }
        };
        if run["ability_catalog_version"] != app.abilities.version() {
            run["loaded_abilities"] = json!([]);
            run["ability_catalog_version"] = json!(app.abilities.version());
            app.save(&mut run).await?;
        }
        if run["phase"] == "model" {
            // Older runs had no selection; bind once before any model dispatch.
            let selection = match run.get("provider_selection") {
                Some(value) => provider::ProviderSelection::parse(value, DEFAULT_AGENT_MODEL),
                None => app.selection().await,
            };
            let selection = match selection {
                Ok(selection) => selection,
                Err(_) => {
                    run["status"] = json!("paused");
                    run["error"] =
                        json!("Unsupported or invalid provider selection; review this run");
                    app.save(&mut run).await?;
                    return Ok(());
                }
            };
            if run.get("provider_selection").is_none() {
                run["provider_selection"] = selection.value();
                app.save(&mut run).await?;
            }
            if run.get("instructions").is_none() {
                run["instructions"] = json!(assembled_agent_instructions(&app.store,
                    run["agent_id"].as_str().unwrap_or("shared_default")).await?);
                app.save(&mut run).await?;
            }
            let instructions = run["instructions"]
                .as_str()
                .ok_or_else(|| anyhow!("Invalid pinned agent instructions"))?
                .to_owned();
            if run["turns"].as_u64().unwrap() >= workflow_policy.max_turns || run["attempt"].as_u64().unwrap() >= workflow_policy.max_attempts {
                run["status"] = json!("paused");
                run["error"] = json!("Run limit reached; explicit continuation required");
                app.save(&mut run).await?;
                return Ok(());
            }
            let (policy, _, visibility, ability_state) = ability_controls(&app, &sid).await?;
            let mode = session_mode(&app, &sid).await?;
            let read_only=mode["mode"]=="read_only";
            // Old skill results are historical evidence, never continuing authority.
            // Mask derived recovery context; original interaction journal stays intact.
            if tools::core_skills::mask_withdrawn_history(&app,&sid,&id,
                run["input"].as_array_mut().unwrap()).await {
                context_accounting.reset();
            }
            // CONTEXT-ASSET-KEY: include full freshly resolved access (including
            // grants), live bundle validity, policy, visibility, loaded state and session mode.
            // Hash only in memory; never publish private grant data in reports.
            let asset_key = digest(&json!([app.abilities.live_version(), policy, visibility,
                ability_state, run["loaded_abilities"], mode, access.context_fingerprint()]));
            if context_assets.as_ref().map(|entry| &entry.0) != Some(&asset_key) {
                let schemas = app.abilities.schemas_with(&policy, &run["loaded_abilities"], &visibility, &ability_state)
                    .into_iter().filter(|schema| {
                        let name=schema["name"].as_str().unwrap_or("");
                        if abilities::is_discovery(name) {
                            let installation=access::Permission::from_policy(policy_decision(&policy,name))
                                .unwrap_or(access::Permission::Deny);
                            access.tool_decision(name,installation,true,true,read_only,true)
                                .permission!=access::Permission::Deny
                        } else {
                            live_tool_decision(&app,&access,name,&policy,&visibility,
                                &ability_state,&run["loaded_abilities"],read_only)
                                .permission!=access::Permission::Deny
                        }
                    }).collect::<Vec<_>>();
                // Build the model menu from the same filtered catalog and schemas
                // used for this caller; the menu never grants execution.
                let mut entries=app.abilities.catalog_with(&policy,&visibility,&ability_state);
                entries.retain_mut(|entry|filter_ability_entry(&app,&access,entry,
                    &policy,&visibility,&ability_state,&run["loaded_abilities"],read_only));
                let index=abilities::Catalog::model_index(&entries,&schemas);
                let visible=app.abilities.visible_guidance_filtered(&policy,&visibility,
                    &ability_state,
                    |id|access.ability_permission(id)!=access::Permission::Deny,
                    |name|live_tool_decision(&app,&access,name,&policy,&visibility,
                        &ability_state,&run["loaded_abilities"],read_only)
                        .permission!=access::Permission::Deny);
                let guidance=format!("{index}{visible}");
                context_assets = Some((asset_key, schemas, guidance));
            }
            let (assembled_asset_key, base_schemas, guidance) = context_assets.as_ref().unwrap();
            let assembled_asset_key=assembled_asset_key.clone();
            let mut gate_schemas = base_schemas.clone();
            let mut mode_content = format!("Current session capability mode: {} (revision {}). Recheck authorization before every tool call.",
                mode["mode"].as_str().unwrap(), mode["revision"].as_u64().unwrap());
            if let Some(label)=mode["definition"]["label"].as_str() {
                mode_content.push_str(&format!("\nExecution mode: {label}\n{}",mode["definition"]["prompt"].as_str().unwrap_or("")));
            }
            let call_number = run["attempt"].as_u64().unwrap_or(0) + 1;
            let mut contributions = vec![context_input::Contribution {
                id: "session_mode".into(), source: "native_session_mode".into(),
                lifetime: context_input::Lifetime::Call, content: mode_content,
                turn: run["turns"].as_u64().unwrap_or(0), call: call_number,
            }];
            // GATE-INJECTION: Rust reads validated gate JSON and adds trusted
            // call-scoped guidance. The model sees private control schemas, but
            // their replies return to Rust for validation and durable storage.
            let before_model = gate_registry::selected(&workflow_policy,
                workflow::Checkpoint::ModelDispatch)?;
            let agent_visibility = gate_controls::agent_visibility(&app.store, &agent_id).await?;
            // Identity format applies to every model call, even when the working-message route is off.
            if !agent_visibility.format.trim().is_empty() {
                contributions.push(context_input::Contribution {
                    id:"agent_message_format".into(),source:"agent_identity".into(),
                    lifetime:context_input::Lifetime::Call,
                    content:format!("Apply this format to user-facing messages in this call:\n{}", &agent_visibility.format),
                    turn:run["turns"].as_u64().unwrap_or(0),call:call_number,
                });
            }
            if agent_visibility.enabled && before_model.iter().any(|item|item.id==gate_registry::MESSAGE_VISIBILITY) {
                let mut config=gate_registry::visibility_config()?;
                if !agent_visibility.guidance.is_empty() {
                    config.guidance.push_str("\nAdditional agent guidance:\n");
                    config.guidance.push_str(&agent_visibility.guidance);
                }
                gate_schemas.push(gate_controls::working_schema(&config));
                contributions.push(context_input::Contribution {
                    id:"gate_message_visibility".into(),source:"native_gate_config".into(),
                    lifetime:context_input::Lifetime::Call,content:config.guidance,
                    turn:run["turns"].as_u64().unwrap_or(0),call:call_number,
                });
            }
            let before_publication = gate_registry::selected(&workflow_policy,
                workflow::Checkpoint::Publication)?;
            if before_publication.iter().any(|item|item.id==gate_registry::CLOSING_STATUS) {
                let config=gate_registry::closing_config()?;
                gate_schemas.push(gate_controls::closing_schema(&config));
                // Closing notification guidance accompanies the fixed status protocol.
                let closing=gate_controls::agent_closing(&app.store,&agent_id).await?;
                contributions.push(context_input::Contribution {
                    id:"gate_closing_status".into(),source:"native_gate_config".into(),
                    lifetime:context_input::Lifetime::Call,content:gate_controls::closing_guidance(&config,&closing),
                    turn:run["turns"].as_u64().unwrap_or(0),call:call_number,
                });
            }
            // COMPACTION CONFIG: reread the single external JSON at each call boundary.
            // Keep this snapshot through planning, prompting and validation; edits apply
            // next boundary. See context_compaction::Config and deploy.sh for the path.
            let compaction_config = match context_compaction::Config::read(&app.compaction_config_path) {
                Ok(config) => config,
                Err(error) => {
                    run["status"] = json!("paused");
                    run["error"] = json!(format!("Invalid context-compaction.json: {error}"));
                    app.save(&mut run).await?;
                    return Ok(());
                }
            };
            // MODEL-CONTEXT-CEILING: runtime profiles bound the entire envelope;
            // compaction JSON still owns folding policy and summary reserves.
            let model_ceiling = loop_runtime::Config::read(&app.runtime_path)?
                .context.ceiling(&selection)?.min(workflow_policy.max_context_bytes);
            let assembled = match context_assembly::prepare_incremental(context_input::Request {
                history: run["input"].as_array().unwrap(), base: &instructions,
                guidance, schemas: &gate_schemas, contributions: &contributions,
                identity: context_input::CallIdentity {
                    turn: run["turns"].as_u64().unwrap_or(0),
                    call: run["attempt"].as_u64().unwrap_or(0) + 1,
                },
                selection: &selection, max_bytes: model_ceiling,
                compaction: &compaction_config,
            }, &mut context_accounting) {
                Ok(prepared) => prepared,
                Err(_) => {
                    run["status"] = json!("paused");
                    run["error"] = json!("Context assembly failed budget or safe compaction validation; review this run");
                    app.save(&mut run).await?;
                    return Ok(());
                }
            };
            // A manual request is only acted on after its tool result has been
            // saved, so no pending call is split across the summary boundary.
            let assembled=if run["manual_compaction_requested"]==true {
                match assembled {
                    context_assembly::Prepared::Ready(ref envelope) => {
                        match context_compaction::plan_manual(run["input"].as_array().unwrap(),
                            envelope.report.history_budget_bytes,&compaction_config) {
                            Ok(Some(plan)) => context_assembly::Prepared::Compact(plan),
                            _ => {
                                run["status"]=json!("paused");
                                run["error"]=json!("Manual compaction has no safe completed boundary; review this run");
                                app.save(&mut run).await?;
                                return Ok(());
                            }
                        }
                    }
                    prepared => prepared,
                }
            } else { assembled };
            // The assembler only plans; the loop owns summarization and persistence.
            if let context_assembly::Prepared::Compact(plan) = assembled {
                // Compaction dispatches only its admission-pinned System role and backup cursor.
                let system_run=match system_dispatch_run(&run) {
                    Ok(value)=>value,
                    Err(error)=>{
                        run["status"]=json!("paused");
                        run["error"]=json!(error.to_string());
                        app.save(&mut run).await?;
                        return Ok(());
                    }
                };
                let selection=provider::ProviderSelection::parse(&system_run["provider_selection"],DEFAULT_AGENT_MODEL)?;
                let prompt = match compaction_config.prompt(&plan) {
                    Ok(prompt) => prompt,
                    Err(_) => {
                        run["status"] = json!("paused");
                        run["error"] = json!("Context summary source is too large; review this run");
                        app.save(&mut run).await?;
                        return Ok(());
                    }
                };
                // Validate the exact summary envelope against System, including on a backup retry.
                let summary_input=match system_summary_input(
                    &loop_runtime::Config::read(&app.runtime_path)?.context,&selection,prompt,workflow_policy.max_context_bytes) {
                    Ok(input)=>input,
                    Err(error)=>{
                        run["status"]=json!("paused");
                        run["error"]=json!(error.to_string());
                        app.save(&mut run).await?;
                        return Ok(());
                    }
                };
                drop(_guard);
                let tokens = match app.tokens_for(&selection, &system_run).await {
                    Ok(value) => value,
                    Err(error) => {
                        let _guard = app.gate.lock().await;
                        let mut run = app.run(&id).await?;
                        if run["generation"] == generation && status(&run) == "running" {
                            system_model_failure(&mut run, &error)?;
                            app.save(&mut run).await?;
                        }
                        return Ok(());
                    }
                };
                let (sender, mut receiver) = tokio::sync::mpsc::channel::<String>(32);
                let drain = tokio::spawn(async move { while receiver.recv().await.is_some() {} });
                // Preserve the same shutoff/dispatch ordering as normal model calls.
                let dispatch_guard = app.gate.lock().await;
                let mut current = app.run(&id).await?;
                if current["generation"] != generation
                    || status(&current) != "running"
                    || app.shutoff().await?
                    || cancel.is_cancelled()
                {
                    drop(sender);
                    drop(dispatch_guard);
                    let _ = drain.await;
                    return Ok(());
                }
                if !app.run_model_authorized(&system_dispatch_run(&current)?).await {
                    current["status"]=json!("stopped");
                    current["error"]=json!("Access changed before model dispatch");
                    app.save(&mut current).await?;
                    drop(sender);
                    drop(dispatch_guard);
                    let _=drain.await;
                    return Ok(());
                }
                // A user choice made during credential retrieval is a new envelope,
            // not an access failure. Reassemble before sending anything upstream.
            let live_mode=session_mode(&app,&sid).await?;
            let live_choice=app.store.get(&format!("session_chat_controls:{sid}")).await?;
            if live_mode["revision"]!=mode["revision"]
                || live_choice.as_ref().and_then(|v|v["revision"].as_u64()).unwrap_or(0)!=current["chat_model_revision"].as_u64().unwrap_or(0) {
                drop(sender);drop(dispatch_guard);let _=drain.await;continue;
            }
            if !dispatch_asset_key(&app,&sid,&agent_id,&current["loaded_abilities"]).await
                    .is_ok_and(|key|key==assembled_asset_key) {
                    current["status"]=json!("stopped");
                    current["error"]=json!("Ability access or bundle changed before model dispatch; review and resume");
                    app.save(&mut current).await?;
                    drop(sender);
                    drop(dispatch_guard);
                    let _=drain.await;
                    return Ok(());
                }
                app.run_candidate(&system_dispatch_run(&current)?).await?;
                let mut completion = Box::pin(app.provider.complete_selected(
                    &selection,
                    &tokens,
                    summary_input,
                    Vec::new(),
                    &sid,
                    sender,
                    cancel.clone(),
                ));
                let mut immediate = None;
                futures_util::future::poll_fn(|cx| {
                    use std::future::Future;
                    if let std::task::Poll::Ready(value) = completion.as_mut().poll(cx) {
                        immediate = Some(value);
                    }
                    std::task::Poll::Ready(())
                })
                .await;
                drop(dispatch_guard);
                let response = if let Some(value) = immediate {
                    value
                } else {
                    tokio::select! {
                        biased;
                        _ = cancel.cancelled() => Err(anyhow!("Context summary cancelled")),
                        result = &mut completion => result
                    }
                };
                drop(completion);
                let _ = drain.await;
                let _guard = app.gate.lock().await;
                let mut run = app.run(&id).await?;
                if run["generation"] != generation || status(&run) != "running" {
                    return Ok(());
                }
                if !app.run_model_authorized(&system_dispatch_run(&run)?).await {
                    run["status"]=json!("stopped");
                    run["error"]=json!("Access changed during context summary");
                    app.save(&mut run).await?;
                    return Ok(());
                }
                if app.shutoff().await? || cancel.is_cancelled() {
                    run["status"] = json!("stopped");
                    run["error"] = json!("Context summary interrupted");
                    app.save(&mut run).await?;
                    return Ok(());
                }
                if let Err(error) = &response {
                    system_model_failure(&mut run, error)?;
                    app.save(&mut run).await?;
                    return Ok(());
                }
                if let Ok(value)=&response{usage::record(&mut run,value);}
                let summary = response.ok().and_then(|value| {
                    value["output"].as_array().map(|output| {
                        output
                            .iter()
                            .filter(|item| item["type"] == "message")
                            .flat_map(|item| item["content"].as_array().into_iter().flatten())
                            .filter_map(|item| item["text"].as_str())
                            .collect::<Vec<_>>()
                            .join("")
                    })
                });
                let compacted = summary.as_deref().and_then(|summary| {
                    context_compaction::apply_summary(run["input"].as_array().unwrap(), &plan, summary).ok()
                });
                let Some(compacted) = compacted else {
                    run["status"] = json!("paused");
                    run["error"] = json!("Context summary failed validation; resume to retry");
                    app.save(&mut run).await?;
                    return Ok(());
                };
                // COMPACTION PERSISTENCE: Store::save_run below atomically saves run,
                // event and session checkpoint under its existing ownership fences.
                // Original interactions stay intact; continue reassembles before dispatch.
                run["input"] = json!(compacted);
                context_accounting.reset();
                run["compactions"] = json!(run["compactions"].as_u64().unwrap_or(0) + 1);
                run["manual_compaction_requested"] = json!(false);
                run["updated_at"] = json!(now());
                run["error"] = Value::Null;
                let segment_id=format!("{}-{}",id,run["compactions"].as_u64().unwrap());
                app.store
                    .save_run(
                        &mut run,
                        "context_compacted",
                        &json!({"run_id":id,"source_digest":plan.source_digest,
                            "segment_id":segment_id,"source":plan.source,
                            "dropped_items":plan.drop_until,"summary_bytes":summary.unwrap().len()}),
                    )
                    .await?;
                continue;
            }
            let context_assembly::Prepared::Ready(envelope) = assembled else {
                unreachable!("compaction path continues before model dispatch")
            };
            let report = serde_json::to_value(&envelope.report)?;
            let instructions = envelope.instructions;
            // DISPATCH-HISTORY-TRANSFER: fingerprint the borrowed history before
            // metadata mutation, then transfer its allocation only after saves.
            // Durable input remains intact; this local run is reloaded after dispatch.
            let input_digest = digest(envelope.input);
            let schemas = envelope.schemas.to_vec();
            run["attempt"] = json!(run["attempt"].as_u64().unwrap() + 1);
            run["context_assembly_report"] = report;
            run["dispatch_mode"]=json!({"execution_mode":mode["execution_mode"],"mode":mode["mode"],"revision":mode["revision"]});
            run["context_controls"]=json!({"trigger_tokens":compaction_config.trigger_tokens,"tail_tokens":compaction_config.tail_tokens,"characters_per_token":compaction_config.characters_per_token});
            app.save(&mut run).await?;
            let snapshot = workflow::ModelSnapshot {
                input_digest,
                instructions_digest:digest(&instructions),
                schemas_digest:digest(&schemas),
            };
            workflow::record(&mut run, &workflow_policy,
                &workflow::Input::ContextAssembly(snapshot.clone()), workflow::Decision::Pass)?;
            app.save(&mut run).await?;
            drop(_guard);
            let tokens = match app.tokens_for(&selection, &run).await {
                Ok(v) => v,
                Err(error) => {
                    let _guard = app.gate.lock().await;
                    let mut run = app.run(&id).await?;
                    if run["generation"] == generation && status(&run) == "running" {
                        model_failure(&mut run, &error)?;
                        app.save(&mut run).await?;
                    }
                    drop(_guard);
                    tokio::time::sleep(Duration::from_secs(2)).await;
                    return Ok(());
                }
            };
            // DISPATCH-HISTORY-TRANSFER: no save may use this consumed local run.
            // Ownership/generation checks below load their own authoritative run.
            let input = match std::mem::take(&mut run["input"]) {
                Value::Array(items) => items,
                _ => return Err(anyhow!("Dispatch history must be an array")),
            };
            let (sender, mut receiver) = tokio::sync::mpsc::channel::<String>(32);
            // Provider fragments remain private; only a final chat row is streamed.
            let collector = tokio::spawn(async move { while receiver.recv().await.is_some() {} });

            // First poll initiates local dispatch while holding the same gate as shutoff.
            let dispatch_guard = app.gate.lock().await;
            let mut current = app.run(&id).await?;
            if current["generation"] != generation
                || status(&current) != "running"
                || app.shutoff().await?
                || cancel.is_cancelled()
            {
                drop(sender);
                drop(dispatch_guard);
                let _ = collector.await;
                return Ok(());
            }
            if !app.run_model_authorized(&current).await {
                current["status"]=json!("stopped");
                current["error"]=json!("Access changed before model dispatch");
                app.save(&mut current).await?;
                drop(sender);
                drop(dispatch_guard);
                let _=collector.await;
                return Ok(());
            }
            // Refresh choices which arrived during credential retrieval before dispatch.
            let live_mode=session_mode(&app,&sid).await?;
            let live_choice=app.store.get(&format!("session_chat_controls:{sid}")).await?;
            if live_mode["revision"]!=mode["revision"]
                || live_choice.as_ref().and_then(|v|v["revision"].as_u64()).unwrap_or(0)!=current["chat_model_revision"].as_u64().unwrap_or(0) {
                drop(sender);drop(dispatch_guard);let _=collector.await;continue;
            }
            if !dispatch_asset_key(&app,&sid,&agent_id,&current["loaded_abilities"]).await
                .is_ok_and(|key|key==assembled_asset_key) {
                current["status"]=json!("stopped");
                current["error"]=json!("Ability access or bundle changed before model dispatch; review and resume");
                app.save(&mut current).await?;
                drop(sender);
                drop(dispatch_guard);
                let _=collector.await;
                return Ok(());
            }
            app.run_candidate(&current).await?;
            if current_workflow_policy(&app).await?.revision()? != workflow_policy.revision()? {
                drop(sender);
                drop(dispatch_guard);
                let _ = collector.await;
                continue;
            }
            workflow::record(&mut current, &workflow_policy,
                &workflow::Input::ModelDispatch(snapshot), workflow::Decision::Pass)?;
            app.save(&mut current).await?;
            // DISPATCH-RELEASE-SNAPSHOTS: fencing is committed; do not retain its
            // reconstructed history or consumed local metadata during network waits.
            // Response handling below reloads authoritative state under the gate.
            drop(current);
            drop(run);
            let mut completion = Box::pin(app.provider.complete_selected_with_instructions(
                &selection,
                &tokens,
                input,
                schemas,
                &sid,
                &instructions,
                sender,
                cancel.clone(),
            ));
            let mut immediate = None;
            futures_util::future::poll_fn(|cx| {
                use std::future::Future;
                if let std::task::Poll::Ready(value) = completion.as_mut().poll(cx) {
                    immediate = Some(value);
                }
                std::task::Poll::Ready(())
            })
            .await;
            drop(dispatch_guard);
            let response = if let Some(value) = immediate {
                value
            } else {
                tokio::select! {
                    biased;
                    _=cancel.cancelled()=>Err(anyhow!("Execution cancelled")),
                    r=&mut completion=>r
                }
            };
            drop(completion);
            let _ = collector.await;
            let _guard = app.gate.lock().await;
            let mut run = app.run(&id).await?;
            if run["generation"] != generation || status(&run) != "running" {
                return Ok(());
            }
            if !app.run_model_authorized(&run).await {
                run["status"]=json!("stopped");
                run["error"]=json!("Access changed during model call");
                app.save(&mut run).await?;
                return Ok(());
            }
            if cancel.is_cancelled() || app.shutoff().await? {
                run["status"] = json!("stopped");
                run["error"] = json!("Provider response discarded after cancellation or shutoff");
                app.save(&mut run).await?;
                return Ok(());
            }
            match response {
                Err(error) => {
                    if cancel.is_cancelled() {
                        run["status"] = json!("stopped");
                        run["error"] = json!("Provider request interrupted");
                    } else {
                        model_failure(&mut run, &error)?;
                    }
                    let payload=json!({"run_id":id,
                        "failover_state":run["failover_state"],"error":run["error"]});
                    app.store.save_run(&mut run, "model_selection_failure", &payload).await?;
                    drop(_guard);
                    if !cancel.is_cancelled() {
                        tokio::time::sleep(Duration::from_secs(2)).await;
                    }
                    return Ok(());
                }
                Ok(response) => {
                    usage::record(&mut run,&response);
                    if run["failover_state"].is_object() {
                        run["failover_state"]["response_started"] = json!(true);
                    }
                    let output = response["output"]
                        .as_array()
                        .ok_or_else(|| anyhow!("Missing provider output"))?;
                    let mut calls = output
                        .iter()
                        .filter(|v| v["type"] == "function_call")
                        .cloned()
                        .collect::<Vec<_>>();
                    for item in output {
                        run["input"].as_array_mut().unwrap().push(item.clone());
                    }
                    let text = output
                        .iter()
                        .filter(|v| v["type"] == "message")
                        .flat_map(|v| v["content"].as_array().into_iter().flatten())
                        .filter_map(|v| v["text"].as_str())
                        .collect::<Vec<_>>()
                        .join("");
                    let publication_policy = current_workflow_policy(&app).await?;
                    workflow::record(&mut run, &publication_policy,
                        &workflow::Input::ModelOutput { output_digest:digest(&json!(output)) },
                        workflow::Decision::Pass)?;
                    run["turns"] = json!(run["turns"].as_u64().unwrap() + 1);
                    run["attempt"] = json!(0);
                    run["calls"] = json!(calls);
                    run["call_index"] = json!(0);
                    run["phase"] = json!("tools");
                    let closing_enabled=gate_registry::selected(&publication_policy,
                        workflow::Checkpoint::Publication)?.iter()
                        .any(|item|item.id==gate_registry::CLOSING_STATUS);
                    // CLOSING-SAME-RESPONSE: the private status tool may accompany
                    // the final prose. Validate it here, then let the existing
                    // terminal transaction publish that prose exactly once.
                    if closing_enabled && calls.len()==1
                        && calls[0]["name"]=="set_closer_status"
                        && !text.trim().is_empty() {
                        let config=gate_registry::closing_config()?;
                        let call_id=calls[0]["call_id"].as_str()
                            .filter(|value|!value.is_empty() && value.len()<=128
                                && !value.chars().any(char::is_control));
                        let arguments=calls[0]["arguments"].as_str()
                            .and_then(|raw|serde_json::from_str::<Value>(raw).ok());
                        let accepted=call_id.and_then(|_|arguments.as_ref())
                            .and_then(|args|gate_controls::closing_status(args,&config).ok());
                        run["closing"]=accepted.clone()
                            .unwrap_or_else(||gate_controls::fallback_status(&config));
                        if let Some(call_id)=call_id {
                            let result=json!({"status":if accepted.is_some() {"ok"} else {"denied"},
                                "output":if accepted.is_some() {"Closing status recorded"}
                                    else {"Closing status invalid; unverified fallback recorded"}});
                            run["input"].as_array_mut().unwrap().push(json!({
                                "type":"function_call_output","call_id":call_id,
                                "output":result.to_string()}));
                        }
                        calls.clear();
                        run["calls"]=json!([]);
                    }
                    if calls.is_empty() {
                        if text.trim().is_empty() {
                            let reasoning_only = !output.is_empty()
                                && output.iter().all(|item| item["type"] == "reasoning");
                            let continuations = run["reasoning_only_completions"].as_u64().unwrap_or(0);
                            if reasoning_only && continuations < 2 {
                                run["reasoning_only_completions"] = json!(continuations + 1);
                                run["phase"] = json!("model");
                                app.store.save_run(&mut run, "model_output", &json!({"output":output})).await?;
                                continue;
                            }
                            let kinds = output.iter().map(|item| item["type"].as_str().unwrap_or("unknown"))
                                .collect::<Vec<_>>();
                            tracing::warn!(run_id=%id, output_types=?kinds,
                                "Provider completed without an assistant message or tool call");
                            run["status"] = json!("failed");
                            run["error"] = json!("Provider completed without a final reply; verify the selected model");
                            app.store
                                .save_run(&mut run, "model_output", &json!({"output":output}))
                                .await?;
                            return Ok(());
                        }
                        if closing_enabled {
                            // CLOSING-FALLBACK: missing or malformed model status
                            // never blocks a valid final reply or starts a repair turn.
                            let config=gate_registry::closing_config()?;
                            let valid=run["closing"]["flag"].as_str()
                                .is_some_and(|flag|config.flags.iter().any(|item|item==flag))
                                && run["closing"]["notification"].as_str()
                                    .is_some_and(|note|!note.trim().is_empty()
                                        && note.chars().count()<=config.notification_max_chars
                                        && note.split_whitespace().count()<=config.notification_max_words);
                            if !valid {
                                run["closing"]=gate_controls::fallback_status(&config);
                            }
                        }
                        if text.len() > publication_policy.max_publication_bytes {
                            workflow::record(&mut run, &publication_policy,
                                &workflow::Input::Publication { text_digest:digest(&json!(text)) },
                                workflow::Decision::Reject { reason: "Publication exceeds configured limit".into() })?;
                            run["status"] = json!("paused");
                            run["error"] = json!("Publication exceeds configured limit; review required");
                            app.store.save_run(&mut run, "model_output", &json!({"output":output})).await?;
                            return Ok(());
                        }
                        let gate_input=gate_protocol::PublicationGateInput {
                            run_id:id.clone(),
                            generation:generation.clone(),
                            policy_revision:publication_policy.revision()?,
                            input_digest:workflow::digest_input(&workflow::Input::Publication {
                                text_digest:digest(&json!(text)) })?,
                            turn:run["turns"].as_u64().unwrap_or(0),
                            call_index:run["call_index"].as_u64().unwrap_or(0),
                            text_bytes:text.len(),
                        };
                        let gate_socket=PathBuf::from(std::env::var("V3_GATE_SOCKET")
                            .unwrap_or_else(|_|gate_protocol::SOCKET_PATH.into()));
                        match gate_protocol::evaluate_publication(
                            &gate_socket,&publication_policy,&gate_input).await? {
                            gate_protocol::GateOutcome::Pass=>{},
                            gate_protocol::GateOutcome::Reject{module_id,reason_code}=>{
                                workflow::record(&mut run,&publication_policy,
                                    &workflow::Input::Publication {
                                        text_digest:digest(&json!(text)) },
                                    workflow::Decision::Reject {
                                        reason:format!("Publication module {module_id} rejected: {reason_code}") })?;
                                run["status"]=json!("paused");
                                run["error"]=json!("Publication rejected by configured gate");
                                app.store.save_run(&mut run,"model_output",&json!({"output":output})).await?;
                                return Ok(());
                            },
                            gate_protocol::GateOutcome::Skipped{module_id,reason_code}=>{
                                run["workflow"]["module_skips"]["publication"]=
                                    json!({"module_id":module_id,"reason_code":reason_code});
                            },
                        }
                        publication::terminal(&app.store, &mut run, output, &text, &publication_policy).await?;
                        return Ok(());
                    }
                    app.store
                        .save_run(&mut run, "model_output", &json!({"output":output}))
                        .await?;
                }
            }
        } else {
            if run["calls"].as_array().is_some_and(|calls|calls.len()>1
                && !calls.iter().any(|call|matches!(call["name"].as_str(),
                    Some("publish_main_message" | "set_closer_status" | "compact_context")))) {
                // NATIVE-GATE-SERIAL: private controls keep provider order and
                // never enter a parallel batch or the Python tool worker.
                run["closing"]=Value::Null;
                parallel_batch::execute_parallel_batch(&app,&id,&generation,&cancel,&workflow_policy,
                    &access,run,_guard).await?;
                continue;
            }
            let idx = run["call_index"].as_u64().unwrap() as usize;
            let calls = run["calls"].as_array().unwrap();
            if idx >= calls.len() {
                run["phase"] = json!("model");
                app.save(&mut run).await?;
                continue;
            }
            let call = calls[idx].clone();
            let name = call["name"].as_str().unwrap_or("").to_owned();
            let args = match call["arguments"]
                .as_str()
                .map(serde_json::from_str::<Value>)
            {
                Some(Ok(v)) if v.is_object() => v,
                _ => json!({"invalid_arguments":true}),
            };
            if name != "set_closer_status" {
                // A later tool means the earlier closer no longer describes
                // the eventual final reply.
                run["closing"]=Value::Null;
            }
            if matches!(name.as_str(),"publish_main_message" | "set_closer_status") {
                // NATIVE-CONTROL-DISPATCH: reserved calls never request approval
                // or enter the Python worker. Rust checks activation and arguments.
                let call_id=call["call_id"].as_str().filter(|value|!value.is_empty()
                    && value.len()<=128 && !value.chars().any(char::is_control));
                let Some(call_id)=call_id else {
                    run["status"]=json!("paused");
                    run["error"]=json!("Invalid private control call ID");
                    app.save(&mut run).await?;
                    return Ok(());
                };
                let checkpoint=if name=="publish_main_message" {
                    workflow::Checkpoint::ModelDispatch
                } else {workflow::Checkpoint::Publication};
                let module_id=if name=="publish_main_message" {
                    gate_registry::MESSAGE_VISIBILITY
                } else {gate_registry::CLOSING_STATUS};
                let enabled=gate_registry::selected(&workflow_policy,checkpoint)?
                    .iter().any(|item|item.id==module_id)
                    && (name != "publish_main_message" || gate_controls::agent_visibility(&app.store, &agent_id).await?.enabled);
                workflow::record(&mut run,&workflow_policy,
                    &workflow::Input::PreTool(workflow::PreToolInput {
                        name:name.clone(),arguments_digest:digest(&args),
                        policy_version:0,mode_revision:0,visibility_revision:0,
                        ability_state_revision:0,
                    }),workflow::Decision::Pass)?;
                run["pending"]=json!({"call_id":call_id});
                let mut working_text=None;
                let result=if !enabled {
                    run["closing"]=Value::Null;
                    json!({"status":"denied","output":"Private control is not enabled"})
                } else if name=="publish_main_message" {
                    match gate_controls::working_text(&args) {
                        Ok(text)=>{
                            working_text=Some(text);
                            json!({"status":"ok","output":"Working message published"})
                        },
                        Err(_)=>json!({"status":"denied","output":"Invalid working message"}),
                    }
                } else {
                    let config=gate_registry::closing_config()?;
                    match gate_controls::closing_status(&args,&config) {
                        Ok(status)=>{
                            run["closing"]=status;
                            json!({"status":"ok","output":"Closing status recorded"})
                        },
                        Err(_)=>{
                            run["closing"]=gate_controls::fallback_status(&config);
                            json!({"status":"denied","output":"Invalid closing status; unverified fallback recorded"})
                        },
                    }
                };
                append_tool(&mut run,&result)?;
                if let Some(text)=working_text {
                    let message_id=format!("working:{id}:{}:{idx}",run["turns"]);
                    app.store.save_run(&mut run,"working_message",
                        &json!({"id":message_id,"text":text})).await?;
                } else {
                    app.store.save_run(&mut run,"tool_result",
                        &json!({"run_id":id,"name":name,"result":result})).await?;
                }
                continue;
            }
            let (policy, mode_revision, visibility, mut ability_state) =
                ability_controls(&app, &sid).await?;
            let read_only=session_mode(&app,&sid).await?["mode"]=="read_only";
            let mut effective=if abilities::is_discovery(&name) {
                let installation=access::Permission::from_policy(policy_decision(&policy,&name))
                    .unwrap_or(access::Permission::Deny);
                access.tool_decision(&name,installation,true,true,read_only,true)
            } else {
                live_tool_decision(&app,&access,&name,&policy,&visibility,
                    &ability_state,&run["loaded_abilities"],read_only)
            };
            workflow::record(&mut run, &workflow_policy,
                &workflow::Input::PreTool(workflow::PreToolInput {
                    name:name.clone(), arguments_digest:digest(&args),
                    policy_version:policy["version"].as_u64().unwrap_or(0),
                    mode_revision, visibility_revision:visibility["revision"].as_u64().unwrap_or(0),
                    ability_state_revision:ability_state["revision"].as_u64().unwrap_or(0),
                }), workflow::Decision::Pass)?;
            if run["pending"].is_null() {
                run["pending"] = json!({"id":uuid(),"name":name,"arguments":args,"call_id":call["call_id"],"digest":digest(&args),
                    "policy_version":policy["version"],"mode_revision":mode_revision,
                    "visibility_revision":visibility["revision"],
                    "ability_state_revision":ability_state["revision"],
                    "access_revision":access.revision.clone(),
                    "state":"requested","decision":null});
            }
            // A model may narrow its own session without approval. Restoring
            // write authority from read_only always crosses a human checkpoint,
            // even when an administrator otherwise grants automatic use.
            if name=="set_execution_mode" && read_only && {
                let target=args["mode"].as_str().unwrap_or("");
                target=="write" || crate::execution_modes::resolve(&crate::execution_modes::config(&app.store,&agent_id).await?,target)
                    .is_ok_and(|m|m["permission_policy"]=="write")
            }
                && effective.permission!=access::Permission::Deny {
                effective.permission=access::Permission::Ask;
            }
            if run["pending"]["policy_version"] != policy["version"]
                || run["pending"]["access_revision"] != json!(access.revision)
                || run["pending"]["mode_revision"].as_u64().unwrap_or(0) != mode_revision
                || run["pending"]["visibility_revision"].as_u64().unwrap_or(0)
                    != visibility["revision"].as_u64().unwrap()
                || run["pending"]["ability_state_revision"]
                    .as_u64()
                    .unwrap_or(0)
                    != ability_state["revision"].as_u64().unwrap()
            {
                run["pending"]["id"] = json!(uuid());
                run["pending"]["decision"] = Value::Null;
                run["pending"]["access_revision"] = json!(access.revision.clone());
                run["pending"]["policy_version"] = policy["version"].clone();
                run["pending"]["mode_revision"] = json!(mode_revision);
                run["pending"]["visibility_revision"] = visibility["revision"].clone();
                run["pending"]["ability_state_revision"] = ability_state["revision"].clone();
            }
            if abilities::is_discovery(&name) {
                if effective.permission == access::Permission::Deny {
                    let result=json!({"status":"denied","output":"Ability discovery unavailable"});
                    append_tool(&mut run,&result)?;
                    app.store.save_run(&mut run,"tool_result",
                        &json!({"run_id":id,"name":name,"result":result})).await?;
                    continue;
                }
                if effective.permission == access::Permission::Ask
                    && run["pending"]["decision"] != "approve" {
                    run["status"]=json!("waiting_approval");
                    run["pending"]["state"]=json!("waiting");
                    let mut payload=run["pending"].clone();
                    payload["run_id"]=json!(id);
                    app.store.save_run(&mut run,"approval_required",&payload).await?;
                    return Ok(());
                }
                let mut result = app.abilities.discover_with(
                    &name,
                    &args,
                    &mut run,
                    &policy,
                    &visibility,
                    &ability_state,
                );
                if name=="list_abilities" {
                    if let Some(entries)=result["abilities"].as_array_mut() {
                        entries.retain_mut(|entry|filter_ability_entry(&app,&access,entry,
                            &policy,&visibility,&ability_state,&run["loaded_abilities"],read_only));
                    }
                } else if result["status"]=="ok" {
                    let allowed=filter_ability_entry(&app,&access,&mut result["ability"],
                        &policy,&visibility,&ability_state,&run["loaded_abilities"],read_only);
                    if !allowed {
                        if let Some(loaded)=run["loaded_abilities"].as_array_mut() {
                            loaded.retain(|item|item!=&args["ability_id"]);
                        }
                        result=json!({"status":"denied","output":"Ability unavailable under current access"});
                    } else {
                        let names=result["ability"]["tools"].as_array().unwrap().iter()
                            .filter_map(|tool|tool["name"].as_str().map(str::to_owned))
                            .collect::<std::collections::BTreeSet<_>>();
                        let skills=result["tool_skills"].as_object().map(|map|map.iter()
                            .filter(|(name,_)|names.contains(*name))
                            .map(|(name,value)|(name.clone(),value.clone()))
                            .collect::<serde_json::Map<String,Value>>());
                        if let Some(skills)=skills {result["tool_skills"]=Value::Object(skills);}
                    }
                }
                if name == "load_ability" && result["status"] == "ok" {
                    let ability_id = args["ability_id"].as_str().unwrap();
                    if !ability_state["active"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|v| v.as_str() == Some(ability_id))
                    {
                        ability_state["active"]
                            .as_array_mut()
                            .unwrap()
                            .push(json!(ability_id));
                        ability_state["revision"] =
                            json!(ability_state["revision"].as_u64().unwrap() + 1);
                        app.store
                            .put(&key("session_ability_state", &sid), &ability_state)
                            .await?;
                    }
                }
                append_tool(&mut run, &result)?;
                app.store
                    .save_run(
                        &mut run,
                        "tool_result",
                        &json!({"run_id":id,"name":name,"result":result}),
                    )
                    .await?;
                continue;
            }
            if effective.permission == access::Permission::Deny
                || run["pending"]["decision"] == "deny"
                || args.get("invalid_arguments").is_some()
            {
                run["workflow"]["receipts"]["pre_tool"]["result"] =
                    json!(workflow::Decision::Reject {
                        reason: "Tool invocation denied by policy or invalid arguments".into()
                    });
                let result = json!({"status":"denied","output":"Tool invocation denied by policy or invalid arguments"});
                append_tool(&mut run, &result)?;
                app.store
                    .save_run(
                        &mut run,
                        "tool_result",
                        &json!({"run_id":id,"name":name,"result":result}),
                    )
                    .await?;
                continue;
            }
            if effective.permission == access::Permission::Ask && run["pending"]["decision"] != "approve" {
                run["status"] = json!("waiting_approval");
                run["pending"]["state"] = json!("waiting");
                let mut payload = run["pending"].clone();
                payload["run_id"] = json!(id);
                app.store
                    .save_run(&mut run, "approval_required", &payload)
                    .await?;
                return Ok(());
            }
            if run["pending"]["digest"] != digest(&args) {
                return Err(anyhow!("Tool argument digest mismatch"));
            }
            run["pending"]["state"] = json!("executing");
            run["pending"]["human_approved"] = json!(run["pending"]["decision"] == "approve");
            run["pending"]["decision"] = json!("consumed");
                let valid=app.store.live_access(app.store.current_user_id(),&agent_id).await
                .is_ok_and(|current|current.admitted()
                    && json!(current.revision)==run["pending"]["access_revision"])
                && app.run_model_authorized(&run).await;
            if !valid {
                run["pending"]["state"]=json!("requested");
                run["pending"]["decision"]=Value::Null;
                run["status"]=json!("stopped");
                run["error"]=json!("Access changed before tool dispatch");
                app.save(&mut run).await?;
                return Ok(());
            }
            app.save(&mut run).await?;
            drop(_guard);
            let result = tools::call(
                app.clone(), sid.clone(), id.clone(), generation.clone(), name.clone(), args.clone(),
                run["pending"]["id"].as_str().unwrap().to_owned(),
                run["pending"]["policy_version"].as_u64().unwrap_or(0),
                cancel.clone(),
            ).await;
            let _guard = app.gate.lock().await;
            let mut run = app.run(&id).await?;
            if run["generation"] != generation {
                return Ok(());
            }
            if status(&run) != "running" {
                // An already committed result can resolve uncertainty without restarting the run.
                if reconcile_late_tool_result(&mut run, result)? {
                    app.save(&mut run).await?;
                }
                return Ok(());
            }
            match result {
                Ok(value) => {
                    let uncertain = value["uncertain"]==true || matches!(
                        value["status"].as_str(),
                        Some("cancelled" | "timeout" | "uncertain")
                    );
                    if uncertain && !tools::is_read(&name) {
                        run["uncertain"] = json!(true);
                        run["status"] = json!("recovery");
                        run["error"] = json!("Tool interrupted; review its outcome");
                        app.save(&mut run).await?;
                        return Ok(());
                    }
                    append_tool(&mut run, &value)?;
                    if name=="compact_context" && value["status"]=="success"
                        && value["control"]=="manual_compaction_requested" {
                        run["manual_compaction_requested"]=json!(true);
                    }
                    app.store
                        .save_run(
                            &mut run,
                            "tool_result",
                            &json!({"run_id":id,"name":name,"result":value}),
                        )
                        .await?;
                }
                Err(_) => {
                    if tools::is_read(&name) {
                        run["pending"]["state"] = json!("requested");
                        run["status"] = json!("failed");
                        run["error"] = json!("Read tool interrupted");
                    } else {
                        run["uncertain"] = json!(true);
                        run["status"] = json!("recovery");
                        run["error"] = json!("Tool outcome unknown; review before continuing");
                    }
                    app.save(&mut run).await?;
                    return Ok(());
                }
            }
        }
    }
}
