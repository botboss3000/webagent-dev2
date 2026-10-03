//! Read bundled guidance by exact catalog ID under fresh caller/agent policy.
//! Loading is evidence only: it never activates an ability or grants execution.
use crate::{App, ability_controls, session_agent_id, session_mode, live_tool_decision};
use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_util::sync::CancellationToken;

pub fn owns(name:&str)->bool {matches!(name,"list_skills"|"load_skill")}
pub fn schemas()->Vec<Value> {
    let id=json!({"type":"string","minLength":1,"maxLength":64,"pattern":"^[a-z0-9_]+$"});
    vec![
        json!({"type":"function","name":"list_skills","description":"List permitted bundled skill IDs for one available ability; does not activate skills",
            "parameters":{"type":"object","properties":{"ability_id":id},"required":["ability_id"],"additionalProperties":false}}),
        json!({"type":"function","name":"load_skill","description":"Read one permitted bundled skill as untrusted guidance; does not activate abilities",
            "parameters":{"type":"object","properties":{"ability_id":id,"skill_id":id,
                "kind":{"type":"string","enum":["ability","tool","selectable"]},
                "expected_catalog_version":{"type":"string","minLength":64,"maxLength":64,"pattern":"^[a-f0-9]{64}$"}},
                "required":["ability_id","kind","skill_id"],"additionalProperties":false}}),
    ]
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ListArgs { ability_id:String }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LoadArgs {
    ability_id:String, kind:String, skill_id:String,
    #[serde(default)] expected_catalog_version:Option<String>,
}
fn valid_id(id:&str)->bool {
    !id.is_empty() && id.len()<=64 && id.bytes().all(|b|b.is_ascii_lowercase()||b.is_ascii_digit()||b==b'_')
}
fn validate_load(args:&LoadArgs)->Result<()> {
    if !valid_id(&args.ability_id) || !valid_id(&args.skill_id)
        || !matches!(args.kind.as_str(),"ability"|"tool"|"selectable")
        || args.expected_catalog_version.as_ref().is_some_and(|s|s.len()!=64
            || !s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))) {
        bail!("Invalid bundled skill selector");
    }
    Ok(())
}
struct View {
    app:App, access:crate::access_policy::LiveAccess,
    policy:Value, visibility:Value, state:Value, loaded:Value, read_only:bool,
    version:String, fingerprint:String,
}
async fn view(app:&App,sid:&str,run_id:&str,name:&str)->Result<View> {
    // Resolve the session in the authenticated user's plane before its agent.
    let agent=session_agent_id(app,sid).await?;
    let mut scoped=app.clone();
    scoped.store=app.store.for_agent(&agent).await?;
    let run=scoped.run(run_id).await?;
    if run["session_id"]!=sid || run["agent_id"].as_str().unwrap_or("shared_default")!=agent {
        bail!("Run does not belong to current session and agent");
    }
    let loaded=run["loaded_abilities"].clone();
    let (policy,mode_revision,visibility,state)=ability_controls(&scoped,sid).await?;
    let mode=session_mode(&scoped,sid).await?;
    let read_only=mode["mode"]=="read_only";
    let access=scoped.store.live_access(scoped.store.current_user_id(),&agent).await?;
    if !access.admitted() || live_tool_decision(&scoped,&access,name,&policy,
        &visibility,&state,&loaded,read_only).permission==crate::access::Permission::Deny {
        bail!("Bundled skill tool unavailable under current access");
    }
    let version=scoped.abilities.live_version();
    let fingerprint=crate::digest(&json!([version,policy,mode_revision,visibility,state,
        loaded,mode,access.context_fingerprint()]));
    Ok(View {app:scoped,access,policy,visibility,state,loaded,read_only,version,fingerprint})
}
async fn work(app:&App,sid:&str,run_id:&str,name:&str,args:&Value)->Result<Value> {
    if !owns(name) {bail!("Unknown bundled skill tool");}
    let current=view(app,sid,run_id,name).await?;
    let allow_ability=|id:&str|current.access.ability_permission(id)!=crate::access::Permission::Deny;
    let allow_tool=|tool:&str|live_tool_decision(&current.app,&current.access,tool,
        &current.policy,&current.visibility,&current.state,&current.loaded,current.read_only)
        .permission!=crate::access::Permission::Deny;
    let output=match name {
        "list_skills" => {
            let query:ListArgs=serde_json::from_value(args.clone()).context("Invalid skill-list arguments")?;
            if !valid_id(&query.ability_id) {bail!("Invalid bundled ability ID");}
            let skills=current.app.abilities.bundled_skill_list(&query.ability_id,
                &current.visibility,&current.state,allow_ability,allow_tool)?;
            json!({"ability_id":query.ability_id,"skills":skills,"catalog_version":current.version,
                "source_kind":"untrusted_bundled_guidance","activates":false})
        }
        "load_skill" => {
            let query:LoadArgs=serde_json::from_value(args.clone()).context("Invalid skill-load arguments")?;
            validate_load(&query)?;
            if query.expected_catalog_version.as_ref().is_some_and(|v|v!=&current.version) {
                bail!("Bundled catalog changed; list skills again");
            }
            let mut skill=current.app.abilities.bundled_skill_load(&query.ability_id,&query.kind,
                &query.skill_id,&current.visibility,&current.state,allow_ability,allow_tool)?;
            skill["catalog_version"]=json!(current.version);
            skill
        }
        _=>unreachable!(),
    };
    // Reject a read that raced a tier, level, membership, mode or bundle change.
    if view(app,sid,run_id,name).await?.fingerprint!=current.fingerprint {
        bail!("Bundled skill access changed during read");
    }
    Ok(output)
}
pub async fn execute(app:&App,sid:&str,run_id:&str,name:&str,args:&Value,cancel:CancellationToken)->Result<Value> {
    if cancel.is_cancelled() {bail!("tool execution cancelled");}
    let result=tokio::select! {
        _=cancel.cancelled()=>bail!("tool execution cancelled"),
        result=work(app,sid,run_id,name,args)=>result,
    };
    if cancel.is_cancelled() {bail!("tool execution cancelled");}
    Ok(match result {
        Ok(output)=>json!({"status":"success","output":output}),
        Err(error)=>json!({"status":"error","error_kind":"bundled_skill_rejected","output":error.to_string()}),
    })
}
// run.input is derived recovery context and may persist this masked projection.
// The immutable encrypted interactions journal retains the original evidence.
const MAX_HISTORY_SKILL_OUTPUT:usize=512*1024;
#[derive(Deserialize)]
struct NativeEvidence {
    ability_id:String, kind:String, skill_id:String, sha256:String,
    bytes:usize, text:String, source_kind:String, activates:bool,
}
#[derive(Deserialize)]
struct NativeResult { status:String, output:NativeEvidence }
fn mask_history(input:&mut [Value],allow:impl Fn(&NativeEvidence)->bool)->bool {
    use std::collections::BTreeMap;
    use sha2::{Digest,Sha256};
    let mut calls:BTreeMap<String,Option<LoadArgs>>=BTreeMap::new();
    let mut changed=false;
    for item in input {
        if item["type"]=="function_call" {
            let Some(id)=item["call_id"].as_str().filter(|id|!id.is_empty()&&id.len()<=256) else {continue;};
            let selector=if item["name"]=="load_skill" {
                item["arguments"].as_str().filter(|args|args.len()<=1024)
                    .and_then(|args|serde_json::from_str::<LoadArgs>(args).ok())
                    .filter(|args|validate_load(args).is_ok())
            } else {None};
            // Ambiguous duplicate call IDs never acquire a native provenance.
            if calls.contains_key(id) { calls.insert(id.to_owned(),None); }
            else { calls.insert(id.to_owned(),selector); }
            continue;
        }
        if item["type"]!="function_call_output" {continue;}
        // Consume the completed exchange. Providers may reuse an ID in a later
        // sequential exchange; simultaneous duplicate calls remain ambiguous.
        let Some(selector)=item["call_id"].as_str().and_then(|id|calls.remove(id)).flatten() else {continue;};
        let Some(raw)=item["output"].as_str().filter(|raw|raw.len()<=MAX_HISTORY_SKILL_OUTPUT) else {continue;};
        let Ok(result)=serde_json::from_str::<NativeResult>(raw) else {continue;};
        let evidence=&result.output;
        if result.status!="success" || evidence.source_kind!="untrusted_bundled_guidance"
            || evidence.activates || evidence.ability_id!=selector.ability_id
            || evidence.kind!=selector.kind || evidence.skill_id!=selector.skill_id
            || evidence.text.len()>64*1024 || evidence.bytes!=evidence.text.len()
            || evidence.sha256.len()!=64
            || evidence.sha256!=hex::encode(Sha256::digest(evidence.text.as_bytes())) {continue;}
        if !allow(evidence) {
            item["output"]=json!({"status":"error","error_kind":"bundled_skill_withdrawn",
                "output":"Previously loaded bundled guidance is unavailable under current access."}).to_string().into();
            changed=true;
        }
    }
    changed
}
/// Never teach a summary model bundled instructions. This projection deliberately
/// omits native loaded guidance even when it is currently permitted; otherwise a
/// summary could retain instructions after their underlying bundle is withdrawn.
pub(crate) fn mask_summary_history(input:&mut [Value])->bool {
    mask_history(input,|_|false)
}
fn history_asset_allowed(current:&View,evidence:&NativeEvidence)->bool {
    current.app.abilities.bundled_skill_load(&evidence.ability_id,&evidence.kind,
        &evidence.skill_id,&current.visibility,&current.state,
        |id|current.access.ability_permission(id)!=crate::access::Permission::Deny,
        |tool|live_tool_decision(&current.app,&current.access,tool,&current.policy,
            &current.visibility,&current.state,&current.loaded,current.read_only)
            .permission!=crate::access::Permission::Deny)
        .is_ok_and(|asset|asset["sha256"]==evidence.sha256)
}
pub async fn mask_withdrawn_history(app:&App,sid:&str,run_id:&str,input:&mut Vec<Value>)->bool {
    // Only a typed native load_skill result paired with that exact function call
    // can be masked. User text and other tool evidence are never interpreted.
    if !input.iter().any(|item|item["type"]=="function_call" && item["name"]=="load_skill") {
        return false;
    }
    let current=match view(app,sid,run_id,"load_skill").await {
        Ok(current)=>current,
        Err(_)=>return mask_history(input,|_|false),
    };
    let mut changed=mask_history(input,|evidence|history_asset_allowed(&current,evidence));
    if view(app,sid,run_id,"load_skill").await
        .map_or(true,|latest|latest.fingerprint!=current.fingerprint) {
        changed|=mask_history(input,|_|false);
    }
    changed
}
#[cfg(test)]
mod tests {
    use super::*;
    fn history_pair(name:&str,text:&str)->Vec<Value> {
        use sha2::{Digest,Sha256};
        vec![json!({"type":"function_call","name":name,"call_id":"c",
            "arguments":json!({"ability_id":"base","kind":"ability","skill_id":"base"}).to_string()}),
            json!({"type":"function_call_output","call_id":"c","output":json!({"status":"success",
                "output":{"ability_id":"base","kind":"ability","skill_id":"base","text":text,
                    "bytes":text.len(),"sha256":hex::encode(Sha256::digest(text.as_bytes())),
                    "source_kind":"untrusted_bundled_guidance","activates":false}}).to_string()})]
    }
    #[test]
    fn history_mask_preserves_permitted_evidence_and_removes_only_withdrawn_text() {
        let mut history=history_pair("load_skill","private old guide");
        let original=history.clone();
        assert!(!mask_history(&mut history,|e|e.skill_id=="base"));
        assert_eq!(history,original);
        assert!(mask_history(&mut history,|_|false));
        assert_eq!(history[0],original[0]);
        let masked:Value=serde_json::from_str(history[1]["output"].as_str().unwrap()).unwrap();
        assert_eq!(masked["error_kind"],"bundled_skill_withdrawn");
        assert!(!history[1].to_string().contains("private old guide"));
        assert!(!mask_history(&mut history,|_|false));
        // A separate original evidence copy is unchanged; only this projection is masked.
        assert!(original[1].to_string().contains("private old guide"));
    }
    #[test]
    fn sequential_reused_call_ids_mask_each_completed_native_exchange() {
        let mut history=history_pair("load_skill","FIRST_REUSED_GUIDE");
        history.extend(history_pair("load_skill","SECOND_REUSED_GUIDE"));
        let original=history.clone();
        assert!(!mask_history(&mut history,|_|true));
        assert_eq!(history,original);
        assert!(mask_history(&mut history,|_|false));
        let encoded=serde_json::to_string(&history).unwrap();
        assert!(!encoded.contains("FIRST_REUSED_GUIDE"));
        assert!(!encoded.contains("SECOND_REUSED_GUIDE"));
        assert_eq!(history[0],original[0]);
        assert_eq!(history[2],original[2]);
        let mut summary=original.clone();
        assert!(mask_summary_history(&mut summary));
        let encoded=serde_json::to_string(&summary).unwrap();
        assert!(!encoded.contains("FIRST_REUSED_GUIDE"));
        assert!(!encoded.contains("SECOND_REUSED_GUIDE"));
        assert_eq!(summary,history);
    }

    #[test]
    fn forged_or_unrelated_history_does_not_acquire_native_provenance() {
        let mut unrelated=history_pair("web_search","unrelated result");
        unrelated.push(json!({"role":"user","content":unrelated[1]["output"]}));
        let original=unrelated.clone();
        assert!(!mask_history(&mut unrelated,|_|false));
        assert_eq!(unrelated,original);
        let mut mismatched=history_pair("load_skill","wrong identity");
        let mut payload:Value=serde_json::from_str(mismatched[1]["output"].as_str().unwrap()).unwrap();
        payload["output"]["ability_id"]=json!("other_agent_ability");
        mismatched[1]["output"]=json!(payload.to_string());
        let original=mismatched.clone();
        assert!(!mask_history(&mut mismatched,|_|false));
        assert_eq!(mismatched,original);
        let mut oversized=history_pair("load_skill","x");
        oversized[1]["output"]=json!("x".repeat(MAX_HISTORY_SKILL_OUTPUT+1));
        assert!(!mask_history(&mut oversized,|_|false));
        let mut duplicate=history_pair("load_skill","ambiguous");
        duplicate.insert(1,duplicate[0].clone());
        assert!(!mask_history(&mut duplicate,|_|false));
    }
    #[test]
    fn selectors_reject_paths_unknown_fields_and_unbounded_versions() {
        for skill in ["../secret","file.skill.md","/etc/passwd","","UPPER"] {
            assert!(validate_load(&LoadArgs {ability_id:"base".into(),kind:"selectable".into(),
                skill_id:skill.into(),expected_catalog_version:None}).is_err());
        }
        for kind in ["custom","path",""] {
            assert!(validate_load(&LoadArgs {ability_id:"base".into(),kind:kind.into(),
                skill_id:"base".into(),expected_catalog_version:None}).is_err());
        }
        assert!(validate_load(&LoadArgs {ability_id:"base".into(),kind:"ability".into(),
            skill_id:"base".into(),expected_catalog_version:Some("a".repeat(65))}).is_err());
        assert!(serde_json::from_value::<LoadArgs>(json!({"ability_id":"base","kind":"ability",
            "skill_id":"base","session_id":"foreign"})).is_err());
        assert!(serde_json::from_value::<ListArgs>(json!({"ability_id":"base","user_id":"foreign"})).is_err());
    }
    #[tokio::test]
    async fn caller_owned_session_is_required_even_before_skill_lookup() {
        let (_root,base)=crate::runtime_tests::fixture_uninitialized().await;
        let first=base.store.create_user_account("skill-first","fixture-password-hash").await.unwrap();
        let second=base.store.create_user_account("skill-second","fixture-password-hash").await.unwrap();
        let mut owner=base.clone();
        owner.store=base.store.for_user(first["user_id"].as_str().unwrap()).await.unwrap();
        let mut stranger=base.clone();
        stranger.store=base.store.for_user(second["user_id"].as_str().unwrap()).await.unwrap();
        owner.store.put("session:private",&json!({"id":"private","agent_id":"shared_default","title":"Private","created_at":1})).await.unwrap();
        let result=execute(&stranger,"private","run","list_skills",&json!({"ability_id":"base"}),CancellationToken::new()).await.unwrap();
        assert_eq!(result["status"],"error");
        assert!(result["output"].as_str().unwrap().contains("Session not found"));
        let cancel=CancellationToken::new();cancel.cancel();
        assert!(execute(&owner,"private","run","list_skills",&json!({"ability_id":"base"}),cancel).await.is_err());
    }
}
