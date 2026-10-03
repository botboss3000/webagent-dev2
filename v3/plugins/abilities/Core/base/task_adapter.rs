//! Core task bookkeeping and safe declarative presentation.
//! The model never selects a user, session or run: Rust supplies those identities.
use crate::{App,ability_controls,session_agent_id,session_mode,live_tool_decision};
use anyhow::{anyhow, bail, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_util::sync::CancellationToken;

pub fn owns(name: &str) -> bool {
    matches!(name, "get_task_checkpoint" | "update_task_checkpoint" | "present_plan" | "present_chat_component")
}
pub fn schemas() -> Vec<Value> {
    [
        ("get_task_checkpoint", "Read the current session's durable task checkpoint and revision", json!({}), vec![]),
        ("update_task_checkpoint", "Save bounded task progress using a compare-and-swap revision; summaries are claims, not verified evidence",
            json!({"expected_revision":{"type":"integer","minimum":0},"update":{"type":"object",
                "properties":{"summary":{"type":"string","minLength":20,"maxLength":1200},
                "steps":{"type":"array","maxItems":32,"items":{"type":"object"}},
                "active_step":{"type":"string","maxLength":120},
                "next_action":{"type":"string","maxLength":1200},
                "blockers":{"type":"array","maxItems":8,"items":{"type":"string"}}},
                "required":["summary"],"additionalProperties":false}}), vec!["expected_revision","update"]),
        ("present_plan", "Persist a declarative plan overview and checklist in this session; expected_revision is 0 for the first plan",
            json!({"expected_revision":{"type":"integer","minimum":0},
                "goal":{"type":"string","maxLength":1200},"criteria":{"type":"string","maxLength":1200},
                "notes":{"type":"string","maxLength":1200},"title":{"type":"string","maxLength":80},
                "checklist":{"type":"array","minItems":1,"maxItems":32,"items":{"type":"object"}}}), vec!["expected_revision","checklist"]),
        ("present_chat_component", "Persist a bounded declarative status, checklist, choice, approval display or form; this does not authorize actions",
            json!({"type":{"type":"string","enum":["todo_list","status","choice","approval","form"]},
                "title":{"type":"string","maxLength":80},"placement":{"type":"string","enum":["inline","hover","sticky"]},
                "data":{"type":"object"},"component_id":{"type":"string","maxLength":64},
                "expected_revision":{"type":"integer","minimum":0}}), vec!["type","expected_revision"]),
    ].into_iter().map(|(name,description,properties,required)|json!({
        "type":"function","name":name,"description":description,
        "parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":false}
    })).collect()
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CheckpointArgs {}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UpdateArgs { expected_revision:u64, update:Update }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Update {
    summary:String,
    #[serde(default)] steps:Option<Vec<Step>>,
    #[serde(default)] active_step:Option<String>,
    #[serde(default)] next_action:Option<String>,
    #[serde(default)] blockers:Option<Vec<String>>,
}
#[derive(Deserialize,serde::Serialize)]
#[serde(deny_unknown_fields)]
struct Step {
    label:String,
    #[serde(default)] status:Option<String>,
    #[serde(default)] verification:Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ComponentArgs {
    #[serde(rename="type")] kind:String,
    #[serde(default)] title:String,
    #[serde(default)] placement:Option<String>,
    #[serde(default="empty_object")] data:Value,
    #[serde(default)] component_id:Option<String>,
    expected_revision:u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PlanArgs {
    expected_revision:u64,
    #[serde(default)] goal:String,
    #[serde(default)] criteria:String,
    #[serde(default)] notes:String,
    #[serde(default)] title:String,
    checklist:Vec<Step>,
}
fn empty_object()->Value {json!({})}
fn text(value:&str,limit:usize)->Result<String> {
    let value=value.trim();
    if value.len()>limit || value.chars().any(|ch| ch.is_control() && ch!='\n' && ch!='\t') {
        bail!("Text exceeds bounds or contains control characters");
    }
    Ok(value.to_owned())
}
fn short_title(value:&str,fallback:&str)->Result<String> {
    let value=text(value,80)?;
    if value.is_empty() || value.split_whitespace().count()>3 { Ok(fallback.to_owned()) }
    else { Ok(value) }
}
fn validate_steps(steps:&[Step])->Result<Value> {
    if steps.is_empty() || steps.len()>32 { bail!("Expected 1..32 task steps"); }
    let mut output=Vec::with_capacity(steps.len());
    for step in steps {
        let label=text(&step.label,240)?;
        if label.len()<3 { bail!("Task step label is too short"); }
        let status=step.status.as_deref().unwrap_or("pending");
        if !matches!(status,"pending"|"in_progress"|"done"|"blocked") {
            bail!("Invalid task step status");
        }
        output.push(json!({"label":label,"status":status,
            "verification":text(step.verification.as_deref().unwrap_or(""),240)?}));
    }
    Ok(json!(output))
}
fn checkpoint(update:Update)->Result<Value> {
    let summary=text(&update.summary,1200)?;
    if summary.len()<20 { bail!("Task summary needs 20..1200 bytes"); }
    let steps=update.steps.as_ref().map(|steps|validate_steps(steps)).transpose()?;
    let active_step=update.active_step.as_deref().map(|s|text(s,120)).transpose()?;
    let next_action=update.next_action.as_deref().map(|s|text(s,1200)).transpose()?;
    let blockers=update.blockers.map(|items| {
        if items.len()>8 { bail!("Too many task blockers"); }
        items.into_iter().map(|item|text(&item,240)).collect::<Result<Vec<_>>>()
    }).transpose()?;
    Ok(json!({"summary":summary,"steps":steps,"active_step":active_step,
        "next_action":next_action,"blockers":blockers,"provenance":"agent_claim"}))
}
fn component(args:ComponentArgs, fallback_id:&str)->Result<(String,Value,u64)> {
    let kind=args.kind.as_str();
    if !matches!(kind,"todo_list"|"status"|"choice"|"approval"|"form") {
        bail!("Unsupported chat component type");
    }
    let id=args.component_id.unwrap_or_else(||fallback_id.to_owned());
    if id.is_empty() || id.len()>64 || !id.bytes().all(|b|b.is_ascii_alphanumeric() || b==b'-' || b==b'_') {
        bail!("Invalid component ID");
    }
    let placement=args.placement.unwrap_or_else(||"inline".to_owned());
    if !matches!(placement.as_str(),"inline"|"hover"|"sticky") {bail!("Invalid component placement");}
    let fallback=match kind {"todo_list"=>"Checklist","status"=>"Overview","choice"=>"Choice",
        "approval"=>"Approval",_=>"Form"};
    let title=short_title(&args.title,fallback)?;
    let data=args.data.as_object().ok_or_else(||anyhow!("Component data must be an object"))?;
    let cleaned=match kind {
        "todo_list"=>{
            let items=data.get("items").and_then(Value::as_array).ok_or_else(||anyhow!("Checklist items required"))?;
            if items.is_empty() || items.len()>100 {bail!("Expected 1..100 checklist items");}
            let mut clean=Vec::new();
            for (n,item) in items.iter().enumerate() {
                clean.push(json!({"id":format!("item-{}",n+1),
                    "label":text(item["label"].as_str().ok_or_else(||anyhow!("Checklist label required"))?,240)?,
                    "done":item["done"].as_bool().unwrap_or(false)}));
            }
            json!({"items":clean})
        }
        "status"=>{
            let sections=data.get("sections").and_then(Value::as_array).ok_or_else(||anyhow!("Status sections required"))?;
            if sections.is_empty() || sections.len()>20 {bail!("Expected 1..20 status sections");}
            let mut clean=Vec::new();
            for section in sections {
                clean.push(json!({"label":text(section["label"].as_str().ok_or_else(||anyhow!("Status label required"))?,80)?,
                    "value":text(section["value"].as_str().unwrap_or(""),1200)?,
                    "status":section["status"].as_str().filter(|s|matches!(*s,"good"|"warning"|"danger"))}));
            }
            json!({"sections":clean})
        }
        "choice"=>{
            let options=data.get("options").and_then(Value::as_array).ok_or_else(||anyhow!("Choice options required"))?;
            if options.is_empty() || options.len()>20 {bail!("Expected 1..20 choice options");}
            let mut clean=Vec::new();
            for (n,option) in options.iter().enumerate() {
                let label=text(option["label"].as_str().ok_or_else(||anyhow!("Choice label required"))?,240)?;
                if label.is_empty() {bail!("Choice label required");}
                clean.push(json!({"id":format!("option-{}",n+1),"label":label}));
            }
            json!({"options":clean})
        }
        "form"=>{
            let fields=data.get("fields").and_then(Value::as_array).ok_or_else(||anyhow!("Form fields required"))?;
            if fields.is_empty() || fields.len()>20 {bail!("Expected 1..20 form fields");}
            let mut clean=Vec::new();
            for field in fields {
                let name=field["name"].as_str().ok_or_else(||anyhow!("Field name required"))?;
                if name.is_empty() || name.len()>40 || !name.bytes().all(|b|b.is_ascii_alphanumeric() || b==b'_') {bail!("Invalid field name");}
                let field_type=field["type"].as_str().unwrap_or("text");
                if !matches!(field_type,"text"|"email"|"number"|"select") {bail!("Unsupported field type");}
                clean.push(json!({"name":name,"label":text(field["label"].as_str().unwrap_or(name),120)?,
                    "type":field_type,"required":field["required"].as_bool().unwrap_or(false)}));
            }
            json!({"fields":clean})
        }
        "approval"=>json!({"message":text(data.get("message").and_then(Value::as_str).unwrap_or(""),1200)?,
            "display_only":true}),
        _=>unreachable!(),
    };
    let spec=json!({"id":id,"type":kind,"title":title,"placement":placement,
        "data":cleaned,"lifecycle":{},"authority":"display_only"});
    if serde_json::to_vec(&spec)?.len()>24*1024 {bail!("Component exceeds 24 KiB");}
    Ok((id,spec,args.expected_revision))
}
fn plan(args:PlanArgs)->Result<(String,Value,u64)> {
    let steps=validate_steps(&args.checklist)?;
    let mut sections=Vec::new();
    for (label,value) in [("Goal",&args.goal),("Criteria",&args.criteria),("Notes",&args.notes)] {
        let value=text(value,1200)?;
        if !value.is_empty() {sections.push(json!({"label":label,"value":value,"status":null}));}
    }
    let list=steps.as_array().unwrap().iter().enumerate().map(|(i,s)|
        format!("{}. [{}] {}",i+1,if s["status"]=="done" {"x"} else {" "},s["label"].as_str().unwrap_or("")))
        .collect::<Vec<_>>().join("\n");
    sections.push(json!({"label":"Checklist","value":list,"status":null}));
    let title=short_title(&args.title,"Plan")?;
    let spec=json!({"id":"plan-overview","type":"status","title":title,"placement":"sticky",
        "data":{"sections":sections},"lifecycle":{},"authority":"display_only"});
    if serde_json::to_vec(&spec)?.len()>24*1024 {bail!("Plan exceeds 24 KiB");}
    Ok(("plan-overview".to_owned(),spec,args.expected_revision))
}
async fn authorize(app:&App,sid:&str,rid:&str,pending_id:&str,name:&str)->Result<String> {
    let agent=session_agent_id(app,sid).await?;
    let mut scoped=app.clone();
    scoped.store=app.store.for_agent(&agent).await?;
    let run=scoped.run(rid).await?;
    if run["session_id"]!=sid || run["agent_id"].as_str().unwrap_or("shared_default")!=agent
        || run["status"]!="running" {bail!("Task run unavailable for current session");}
    let (policy,mode_revision,visibility,state)=ability_controls(&scoped,sid).await?;
    let mode=session_mode(&scoped,sid).await?;
    let access=scoped.store.live_access(scoped.store.current_user_id(),&agent).await?;
    let pending=if run["pending"]["id"]==pending_id && run["pending"]["state"]=="executing" {
        Some(&run["pending"])
    } else {
        run["batch"].as_array().and_then(|batch|batch.iter().find(|item|
            item["pending"]["id"]==pending_id && item["state"]=="executing"))
            .map(|item|&item["pending"])
    }.ok_or_else(||anyhow!("Task call is no longer executing"))?;
    if pending["policy_version"]!=policy["version"]
        || pending["mode_revision"]!=json!(mode_revision)
        || pending["visibility_revision"]!=visibility["revision"]
        || pending["ability_state_revision"]!=state["revision"]
        || pending["access_revision"]!=json!(access.revision.clone()) {
        bail!("Task authorization changed during dispatch");
    }
    if !access.admitted() || live_tool_decision(&scoped,&access,name,&policy,&visibility,
        &state,&run["loaded_abilities"],mode["mode"]=="read_only")
        .permission==crate::access::Permission::Deny {
        bail!("Base task tool unavailable under current access");
    }
    Ok(run["generation"].as_str().ok_or_else(||anyhow!("Task run generation unavailable"))?.to_owned())
}
async fn work(app:&App,sid:&str,rid:&str,pending_id:&str,name:&str,args:&Value)->Result<Value> {
    let generation=authorize(app,sid,rid,pending_id,name).await?;
    match name {
        "get_task_checkpoint"=>{
            let _:CheckpointArgs=serde_json::from_value(args.clone())?;
            let state=app.store.base_task_checkpoint(sid,rid).await?;
            if authorize(app,sid,rid,pending_id,name).await? != generation {bail!("Task run generation changed");}
            Ok(json!({"status":"success","output":state}))
        }
        "update_task_checkpoint"=>{
            let request:UpdateArgs=serde_json::from_value(args.clone())?;
            let artifact=checkpoint(request.update)?;
            if authorize(app,sid,rid,pending_id,name).await? != generation {bail!("Task run generation changed");}
            let saved=app.store.base_write_artifact(sid,rid,pending_id,&generation,"checkpoint","task",
                request.expected_revision,&artifact).await?;
            Ok(json!({"status":"success","output":saved}))
        }
        "present_chat_component"=>{
            let request:ComponentArgs=serde_json::from_value(args.clone())?;
            let fallback=format!("component-{}",&crate::digest(&json!(pending_id))[..24]);
            let (id,spec,revision)=component(request,&fallback)?;
            if authorize(app,sid,rid,pending_id,name).await? != generation {bail!("Task run generation changed");}
            let saved=app.store.base_write_artifact(sid,rid,pending_id,&generation,"component",&id,revision,&spec).await?;
            Ok(json!({"status":"success","output":saved}))
        }
        "present_plan"=>{
            let request:PlanArgs=serde_json::from_value(args.clone())?;
            let (id,spec,revision)=plan(request)?;
            if authorize(app,sid,rid,pending_id,name).await? != generation {bail!("Task run generation changed");}
            let saved=app.store.base_write_artifact(sid,rid,pending_id,&generation,"component",&id,revision,&spec).await?;
            Ok(json!({"status":"success","output":saved}))
        }
        _=>bail!("Unknown Base task tool"),
    }
}
pub async fn execute(app:&App,sid:&str,rid:&str,pending_id:&str,name:&str,args:&Value,
    cancel:CancellationToken)->Result<Value> {
    if cancel.is_cancelled() {bail!("tool execution cancelled");}
    let result=tokio::select! {
        _=cancel.cancelled()=>bail!("tool execution cancelled"),
        result=work(app,sid,rid,pending_id,name,args)=>result,
    };
    if cancel.is_cancelled() {bail!("tool execution cancelled");}
    Ok(match result {
        Ok(output)=>output,
        Err(error)=>json!({"status":"error","error_kind":"base_task_rejected","output":error.to_string()}),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn checkpoint_and_components_are_bounded_and_declarative() {
        let valid=json!({"summary":"Worked through implementation; next verify persistence.",
            "steps":[{"label":"Implement storage","status":"in_progress"}]});
        let update:Update=serde_json::from_value(valid).unwrap();
        assert_eq!(checkpoint(update).unwrap()["provenance"],"agent_claim");
        assert!(serde_json::from_value::<Update>(json!({"summary":"x","html":"<script>"})).is_err());
        assert!(serde_json::from_value::<ComponentArgs>(json!({"type":"status","expected_revision":0,
            "data":{"sections":[{"label":"Progress","value":"Ready"}]},"lifecycle":{"script":"alert(1)"}})).is_err());
        let (_,spec,_)=component(serde_json::from_value(json!({"type":"approval","expected_revision":0,
            "data":{"message":"Please approve","status":"approved"}})).unwrap(),"c").unwrap();
        assert_eq!(spec["authority"],"display_only");
        assert_eq!(spec["data"]["display_only"],true);
    }
}
