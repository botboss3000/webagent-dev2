//! Dispatch one already authorized call. Policy and approval stay with the loop.
use crate::*;
pub(crate) async fn call(
    app:App,sid:String,run_id:String,generation:String,name:String,args:Value,pending_id:String,
    policy_version:u64,cancel:CancellationToken,
)->Result<Value> {
    if name=="promotion_propose" {
        propose_promotion(&app,&sid,&pending_id,&run_id,&args,policy_version,cancel).await
    } else if matches!(name.as_str(),"prompt_slot_read"|"prompt_slot_write") {
        execute_prompt_slot(&app,&name,&args).await
    } else if name=="state_inspect" {
        match serde_json::from_value::<InspectArgs>(args) {
            Ok(query) if validate_inspect_args(&query).is_ok() =>
                inspect_state(&app,&sid,&query).await
                    .map(|output|json!({"status":"success","output":output})),
            _=>Ok(json!({"status":"denied","output":"Invalid state inspection arguments"})),
        }
    } else if tools::core_mode::owns(&name) {
        tools::core_mode::execute(&app,&sid,&run_id,&generation,&pending_id,&name,&args,cancel).await
    } else if tools::administrator::owns(&name) {
        tools::administrator::execute(&app,&sid,&run_id,&name,&args,&pending_id,policy_version,cancel).await
    } else if tools::core_task::owns(&name) {
        tools::core_task::execute(&app,&sid,&run_id,&pending_id,&name,&args,cancel).await
    } else if tools::core_self_edit::owns(&name) {
        tools::core_self_edit::execute(&app,&sid,&run_id,&name,&args,&pending_id,cancel).await
    } else if tools::core_tool_search::owns(&name) {
        tools::core_tool_search::execute(&app,&sid,&run_id,&args,cancel).await
    } else if tools::core_search::owns(&name) {
        tools::core_search::execute(&app,&sid,&run_id,&name,&args,cancel).await
    } else if tools::core_compaction::owns(&name) {
        tools::core_compaction::execute(&app,&sid,&run_id,&name,&args,cancel).await
    } else if tools::core_recall::owns(&name) {
        tools::core_recall::execute(&app,&sid,&run_id,&name,&args,cancel).await
    } else if tools::core_prompt::owns(&name) {
        tools::core_prompt::execute(&app,&sid,&run_id,&name,&args,cancel).await
    } else if tools::core_skills::owns(&name) {
        // Skill reads recheck current caller, agent and bundle validity; no activation.
        tools::core_skills::execute(&app,&sid,&run_id,&name,&args,cancel).await
    } else if tools::core_session::owns(&name) {
        // The authenticated runtime supplies the session; arguments cannot choose one.
        tools::core_session::execute(&app,&sid,&name,&args,cancel).await
    } else if tools::core_utilities::owns(&name) {
        // Native utilities remain behind the same live Rust authorization as worker calls.
        tools::core_utilities::execute(&name,&args,cancel).await
    } else if tools::web_access::owns(&name) {
        tools::web_access::execute(&name,&args,cancel).await
    } else if tools::core_decision::owns(&name) {
        // The compiled adapter resolves this session's assigned Decision model and credential.
        tools::core_decision::execute(&app,&sid,&run_id,&args,cancel).await
    } else if tools::core_directory_decision::owns(&name) {
        // Rust checks file_list authority before the worker returns names to Jev.
        tools::core_directory_decision::execute(&app,&sid,&run_id,&args,cancel).await
    } else {
        tools::execute(&name,&args,&sid,&app.tool_socket,cancel).await
    }
}
