//! Native reads are confined to the caller-owned session supplied by Rust.
//! No tool argument can select a different user, session, database or private interaction.
use crate::{App, attachment_get};
use anyhow::{bail, Context, Result};
use axum::{extract::{State, Path}, Json};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_util::sync::CancellationToken;

pub fn owns(name: &str) -> bool { matches!(name, "read_attachment" | "search_this_session") }
pub fn schemas() -> Vec<Value> {
    [
        ("read_attachment", "Read a bounded Unicode-character page of a current-session text attachment", json!({"attachment_id":{"type":"string","format":"uuid"},"offset_chars":{"type":"integer","minimum":0},"limit_chars":{"type":"integer","minimum":1,"maximum":4096}}), vec!["attachment_id"]),
        ("search_this_session", "Search a bounded page of current-session public chat by case-sensitive literal text; continue with next_seq", json!({"query":{"type":"string","minLength":1,"maxLength":256},"after_seq":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":20}}), vec!["query"]),
    ].into_iter().map(|(name,description,properties,required)|json!({
        "type":"function","name":name,"description":description,
        "parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":false}
    })).collect()
}
fn attachment_limit() -> usize {4096}
fn search_limit() -> usize {10}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AttachmentArgs {
    attachment_id:String,
    #[serde(default)] offset_chars:usize,
    #[serde(default="attachment_limit")] limit_chars:usize,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SearchArgs {
    query:String,
    #[serde(default)] after_seq:i64,
    #[serde(default="search_limit")] limit:usize,
}
fn attachment_page(record:Value,args:&AttachmentArgs)->Result<Value> {
    if !(1..=4096).contains(&args.limit_chars) {
        bail!("Invalid attachment page bounds");
    }
    let media=record["media_type"].as_str().unwrap_or("");
    let textual=media.starts_with("text/")||media.starts_with("application/")&&media!="application/pdf";
    if !textual&&record["text"].as_str().is_some_and(|text|!text.is_empty())&&record["described_by"].as_str().is_none(){
        bail!("Binary content lacks native extraction provenance");
    }
    let text=record["text"].as_str().filter(|s|!s.is_empty()).unwrap_or("File metadata only; content extraction is unavailable for this media type. Do not guess its contents.");
    let total=text.chars().count();
    if args.offset_chars>total {bail!("Attachment cursor exceeds text length");}
    let page:String=text.chars().skip(args.offset_chars).take(args.limit_chars).collect();
    let next=args.offset_chars+page.chars().count();
    Ok(json!({"id":record["id"],"run_id":record["run_id"],"name":record["name"],
        "media_type":record["media_type"],"bytes":record["bytes"],"sha256":record["sha256"],
        "text":page,"offset_chars":args.offset_chars,"next_offset_chars":next,
        "total_chars":total,"has_more":next<total,"source_kind":"untrusted_attachment"}))
}
fn search_args(args:&SearchArgs)->Result<()> {
    if args.query.trim().is_empty() || args.query.len()>256
        || args.query.chars().any(char::is_control) || args.after_seq<0
        || !(1..=20).contains(&args.limit) {bail!("Invalid session search arguments");}
    Ok(())
}
fn snippet(content:&str,at:usize)->String {
    let before:Vec<char>=content[..at].chars().rev().take(120).collect();
    before.into_iter().rev().chain(content[at..].chars().take(392)).collect()
}
fn search_page(page:Value,args:&SearchArgs)->Result<Value> {
    search_args(args)?;
    let rows=page["messages"].as_array().context("Chat page unavailable")?;
    if rows.len()>50 {bail!("Chat scan exceeds limit");}
    let mut results=Vec::new();
    let mut next=args.after_seq;
    let mut scanned=0usize;
    for message in rows {
        let seq=message["seq"].as_i64().context("Chat cursor unavailable")?;
        if seq<=next {bail!("Chat cursor order conflict");}
        next=seq;scanned+=1;
        // Search only published user/assistant chat. Private tool evidence never
        // enters this helper, and unknown row roles cannot expand that contract.
        if !matches!(message["role"].as_str(),Some("user"|"assistant")) {continue;}
        let Some(content)=message["content"].as_str() else {continue;};
        let Some(at)=content.find(&args.query) else {continue;};
        let preview=snippet(content,at);
        results.push(json!({"seq":seq,"id":message["id"],"run_id":message["run_id"],
            "role":message["role"],"created_at":message["created_at"],
            "content_preview":preview,"preview_truncated":preview.chars().count()<content.chars().count()}));
        if results.len()==args.limit {break;}
    }
    Ok(json!({"messages":results,"next_seq":next,
        "has_more":scanned<rows.len() || page["has_more"]==true,
        "scanned":scanned,"scan_limit":50,"case_sensitive":true,
        "source_kind":"public_chat"}))
}
async fn work(app:&App,sid:&str,name:&str,args:&Value)->Result<Value> {
    // The store is already routed to the authenticated caller. Require this
    // session to exist there before any attachment or chat record is resolved.
    app.store.session_summary(sid).await.context("Current session unavailable")?;
    match name {
        "read_attachment" => {
            let query:AttachmentArgs=serde_json::from_value(args.clone()).context("Invalid attachment arguments")?;
            if !(1..=4096).contains(&query.limit_chars) {bail!("Invalid attachment page bounds");}
            // Reuse the API's encrypted lookup and exact session/provenance checks.
            let Json(record)=attachment_get(State(app.clone()),Path((sid.to_owned(),query.attachment_id.clone())))
                .await.map_err(|_|anyhow::anyhow!("Attachment unavailable in current session"))?;
            attachment_page(record,&query)
        }
        "search_this_session" => {
            let query:SearchArgs=serde_json::from_value(args.clone()).context("Invalid session search arguments")?;
            search_args(&query)?;
            let page=app.store.chat_page(sid,query.after_seq,50).await.context("Current-session chat unavailable")?;
            search_page(page,&query)
        }
        _=>bail!("Unknown session read tool"),
    }
}
pub async fn execute(app:&App,sid:&str,name:&str,args:&Value,cancel:CancellationToken)->Result<Value> {
    if cancel.is_cancelled() {bail!("tool execution cancelled");}
    let result=tokio::select! {
        _=cancel.cancelled()=>bail!("tool execution cancelled"),
        result=work(app,sid,name,args)=>result,
    };
    if cancel.is_cancelled() {bail!("tool execution cancelled");}
    Ok(match result {
        Ok(output)=>json!({"status":"success","output":output}),
        Err(error)=>json!({"status":"error","error_kind":"session_read_rejected","output":error.to_string()}),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unicode_attachment_pages_have_character_cursors_and_type_limits() {
        let record=json!({"id":"id","run_id":"run","name":"n","media_type":"text/plain",
            "bytes":8,"sha256":"digest","text":"aé🙂z"});
        let args=AttachmentArgs {attachment_id:"id".into(),offset_chars:1,limit_chars:2};
        let page=attachment_page(record.clone(),&args).unwrap();
        assert_eq!(page["text"],"é🙂");assert_eq!(page["next_offset_chars"],3);
        assert_eq!(page["has_more"],true);assert_eq!(page["total_chars"],4);
        assert!(attachment_page(record.clone(),&AttachmentArgs {offset_chars:5,..args}).is_err());
        let mut binary=record;binary["media_type"]=json!("image/png");
        assert!(attachment_page(binary,&AttachmentArgs {attachment_id:"id".into(),offset_chars:0,limit_chars:2}).is_err());
    }
    #[test]
    fn attachment_reader_has_no_builtin_maximum_offset() {
        let record=json!({"id":"id","run_id":"run","name":"n","media_type":"text/plain",
            "bytes":26214402,"sha256":"digest","text":"x".repeat(26214401)+"Z"});
        let page=attachment_page(record,&AttachmentArgs {attachment_id:"id".into(),
            offset_chars:26214401,limit_chars:1}).unwrap();
        assert_eq!(page["text"],"Z");
        assert_eq!(page["has_more"],false);
    }
    #[test]
    fn search_is_literal_bounded_and_keeps_unreturned_matches_after_cursor() {
        let rows=(1..=50).map(|n|json!({"seq":n,"id":format!("m{n}"),"run_id":"r",
            "role":if n==1 {"tool"} else {"assistant"},"created_at":1,
            "content":format!("{}needle[.*]{}", "é".repeat(600),"z".repeat(600))})).collect::<Vec<_>>();
        let args=SearchArgs {query:"needle[.*]".into(),after_seq:0,limit:2};
        let page=search_page(json!({"messages":rows,"has_more":false}),&args).unwrap();
        assert_eq!(page["messages"].as_array().unwrap().len(),2);
        assert_eq!(page["next_seq"],3);assert_eq!(page["has_more"],true);
        assert_eq!(page["scanned"],3);
        assert!(page["messages"][0]["content_preview"].as_str().unwrap().contains("needle[.*]"));
        assert!(page["messages"][0]["content_preview"].as_str().unwrap().chars().count()<=512);
        assert!(search_page(json!({"messages":vec![json!({"seq":1});51]}),&args).is_err());
        assert!(search_args(&SearchArgs {query:"".into(),..args}).is_err());
        assert!(serde_json::from_value::<SearchArgs>(json!({"query":"x","session_id":"foreign"})).is_err());
        assert!(serde_json::from_value::<AttachmentArgs>(json!({"attachment_id":"x","user_id":"foreign"})).is_err());
    }
    #[tokio::test]
    async fn encrypted_attachment_lookup_rejects_another_session_and_user_store() {
        let (_root,base)=crate::runtime_tests::fixture_uninitialized().await;
        let first=base.store.create_user_account("first-session-reader","long-fixture-hash-placeholder").await.unwrap();
        let second=base.store.create_user_account("second-session-reader","long-fixture-hash-placeholder").await.unwrap();
        let mut app=base.clone();
        app.store=base.store.for_user(first["user_id"].as_str().unwrap()).await.unwrap();
        let mut other_app=base.clone();
        other_app.store=base.store.for_user(second["user_id"].as_str().unwrap()).await.unwrap();
        app.store.put("session:current",&json!({"id":"current","title":"Current","created_at":1})).await.unwrap();
        app.store.put("session:foreign",&json!({"id":"foreign","title":"Foreign","created_at":1})).await.unwrap();
        let id="11111111-1111-4111-8111-111111111111";
        app.store.put(&format!("attachment:{id}"),&json!({"schema_version":1,"id":id,
            "session_id":"foreign","run_id":"r","name":"n","media_type":"text/plain",
            "bytes":6,"sha256":"hash","text":"secret"})).await.unwrap();
        let query=json!({"attachment_id":id});
        assert_eq!(execute(&app,"current","read_attachment",&query,CancellationToken::new()).await.unwrap()["status"],"error");
        assert_eq!(execute(&app,"foreign","read_attachment",&query,CancellationToken::new()).await.unwrap()["output"]["text"],"secret");
        // Same installation, shared encryption authority, and identical session/record
        // IDs: caller routing, rather than key uniqueness, must isolate these reads.
        other_app.store.put("session:foreign",&json!({"id":"foreign","title":"Other","created_at":1})).await.unwrap();
        assert_eq!(execute(&other_app,"foreign","read_attachment",&query,CancellationToken::new()).await.unwrap()["status"],"error");
        other_app.store.put(&format!("attachment:{id}"),&json!({"schema_version":1,"id":id,
            "session_id":"foreign","run_id":"r","name":"n","media_type":"text/plain",
            "bytes":5,"sha256":"other-hash","text":"other"})).await.unwrap();
        assert_eq!(execute(&other_app,"foreign","read_attachment",&query,CancellationToken::new()).await.unwrap()["output"]["text"],"other");
        assert_eq!(execute(&app,"foreign","read_attachment",&query,CancellationToken::new()).await.unwrap()["output"]["text"],"secret");
        for (reader,text) in [(&app,"first-only evidence"),(&other_app,"second-only evidence")] {
            sqlx::query("INSERT INTO chat(seq,id,session_id,run_id,role,content,created_at) VALUES(1,'same-message','foreign','r','user',?,1)")
                .bind(reader.store.seal(&json!(text)).unwrap()).execute(&reader.store.pool).await.unwrap();
        }
        let search=json!({"query":"first-only"});
        assert_eq!(execute(&app,"foreign","search_this_session",&search,CancellationToken::new()).await.unwrap()["output"]["messages"].as_array().unwrap().len(),1);
        assert!(execute(&other_app,"foreign","search_this_session",&search,CancellationToken::new()).await.unwrap()["output"]["messages"].as_array().unwrap().is_empty());
        let cancel=CancellationToken::new();cancel.cancel();
        assert!(execute(&app,"foreign","read_attachment",&query,cancel).await.is_err());
    }
    #[tokio::test]
    async fn encrypted_chat_search_returns_only_current_session_public_rows() {
        let (_root,app)=crate::runtime_tests::fixture_uninitialized().await;
        for sid in ["current","foreign"] {
            app.store.put(&format!("session:{sid}"),&json!({"id":sid,"title":sid,"created_at":1})).await.unwrap();
        }
        for (seq,sid,text) in [(1,"current","known needle"),(2,"foreign","foreign needle")] {
            sqlx::query("INSERT INTO chat(seq,id,session_id,run_id,role,content,created_at) VALUES(?,?,?,?,?,?,?)")
                .bind(seq).bind(format!("m{seq}")).bind(sid).bind("r").bind("user")
                .bind(app.store.seal(&json!(text)).unwrap()).bind(1)
                .execute(&app.store.pool).await.unwrap();
        }
        let output=execute(&app,"current","search_this_session",&json!({"query":"needle"}),CancellationToken::new()).await.unwrap();
        assert_eq!(output["output"]["messages"].as_array().unwrap().len(),1);
        assert_eq!(output["output"]["messages"][0]["content_preview"],"known needle");
        assert_eq!(output["output"]["has_more"],false);
        assert_eq!(execute(&app,"missing","search_this_session",&json!({"query":"needle"}),CancellationToken::new()).await.unwrap()["status"],"error");
    }
}
