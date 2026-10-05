//! Claim queued runs and renew their leases while the loop is alive.
use super::*;
// Retire compacted reasoning outside the run loop. Each user-store job commits
// atomically and remains queued after a failed attempt or process restart.
async fn reasoning_cleanup_worker(app: App) {
    loop {
        if app.stop.is_cancelled() { break; }
        match user_apps(&app).await {
            Ok(users) => for user in users {
                if app.stop.is_cancelled() { break; }
                if let Err(error)=user.store.cleanup_one_reasoning().await {
                    tracing::warn!("Reasoning cleanup deferred: {error}");
                }
            },
            Err(error) => tracing::warn!("Reasoning cleanup user scan deferred: {error}"),
        }
        tokio::time::sleep(Duration::from_secs(5)).await;
    }
}
// This is the loop container's dispatcher: announce that it is alive, notice
// queued work, claim a lease, and start at most the configured number of runs.
pub(crate) async fn scheduler(app: App) {
    let cleanup=tokio::spawn(reasoning_cleanup_worker(app.clone()));
    let owner = uuid();
    let mut last_heartbeat = 0;
    let mut last_recovery = 0;
    loop {
        if app.stop.is_cancelled() || !app.ready.load(Ordering::Acquire) {
            break;
        }
        if now().saturating_sub(last_heartbeat)>=2 {
            if let Err(error)=app.store.write_loop_heartbeat(&owner).await {
                tracing::error!("Loop heartbeat failed: {error}");
            }
            last_heartbeat=now();
        }
        if now() - last_recovery >= app.runtime.read().await.scheduler.recovery_interval_ms.div_ceil(1000) {
            if recover(&app, false).await.is_err() {
                tracing::error!("Recovery scan failed");
            }
            last_recovery = now();
        }
        let _guard = app.gate.lock().await;
        if let Ok(false) = app.shutoff().await {
            let users=match user_apps(&app).await {
                Ok(users)=>users,
                Err(error)=>{tracing::error!("User queue scan failed: {}",error);Vec::new()}
            };
            for user_app in users {
            if let Err(error)=user_app.store.acknowledge_loop_commands().await {
                tracing::error!("Loop command validation failed: {error}");
                continue;
            }
            if let Ok(runs) = user_app.store.active_runs().await {
                for view in runs {
                    if status(&view) != "queued" {
                        continue;
                    }
                    let id = view["id"].as_str().unwrap().to_owned();
                    let mut run = match user_app.run(&id).await {
                        Ok(r) => r,
                        Err(_) => continue,
                    };
                    let mut active = app.active.lock().await;
                    if active.contains_key(&id) {
                        continue;
                    }
                    let permit = match app.slots.clone().try_acquire_owned() {
                        Ok(p) => p,
                        Err(_) => break,
                    };
                    let cancel = app.stop.child_token();
                    active.insert(id.clone(), cancel.clone());
                    drop(active);
                    if run["context_hydration_pending"] == true {
                        match user_app.store.context(run["session_id"].as_str().unwrap()).await {
                            Ok(mut input) => {
                                // Keep saved notes beside the latest user turn as source data.
                                memory::insert(&mut input,run["memory_context"].as_str());
                                run["input"] = json!(input);
                                run["context_hydration_pending"] = json!(false);
                            },
                            Err(error) => {tracing::warn!(run_id=%id,"Context hydration failed: {}",error);app.active.lock().await.remove(&id);continue;}
                        }
                    }
                    let generation=uuid();
                    if !matches!(user_app.store.claim_run(&mut run,&owner,&generation).await,Ok(true)) {
                        app.active.lock().await.remove(&id);
                        continue;
                    }
                    let cloned = user_app.clone();
                    let lease_owner=owner.clone();
                    tokio::spawn(async move {
                        let epoch=run["lease_epoch"].as_i64().unwrap_or(0);
                        let watcher_store=cloned.store.clone();
                        let watcher_id=id.clone();
                        let watcher_generation=generation.clone();
                        let watcher_cancel=cancel.clone();
                        let watcher=tokio::spawn(async move {
                            loop {
                                tokio::time::sleep(Duration::from_secs(5)).await;
                                if watcher_cancel.is_cancelled() {break;}
                                let renewed=watcher_store.renew_run_lease(
                                    &watcher_id,&watcher_generation,&lease_owner,epoch).await
                                    .unwrap_or(false);
                                if !renewed {watcher_cancel.cancel();break;}
                            }
                        });
                        if let Err(error) =
                            execute_run(cloned.clone(), id.clone(), generation, cancel.clone()).await
                        {
                            tracing::warn!(run_id=%id,"Run interrupted: {}",error);
                        }
                        cancel.cancel();
                        watcher.abort();
                        cloned.active.lock().await.remove(&id);
                        drop(permit);
                    });
                }
            }
            }
        }
        drop(_guard);
        tokio::time::sleep(Duration::from_millis(app.runtime.read().await.scheduler.poll_interval_ms)).await;
    }
    cleanup.abort();
}
