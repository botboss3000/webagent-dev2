//! Read-only private projection. Public chat is never hydrated from this journal.
use super::*;
fn boundary(row: &SqliteRow) -> String {
    row.get::<Option<String>,_>("boundary_item_id").unwrap_or_else(||format!("user:{}",row.get::<String,_>("run_id")))
}
fn event(row: &SqliteRow, store: &Store, sid: &str) -> Result<Value> {
    let seq=row.get::<i64,_>("seq");
    let run=row.get::<String,_>("run_id");
    let item=store.unseal(&row.get::<Vec<u8>,_>("item"))?;
    let (kind,payload)=match item["type"].as_str().unwrap_or("") {
        "function_call" => ("tool_call",json!({"tool_call_id":item["call_id"],"tool":item["name"],"arguments":item["arguments"]})),
        "function_call_output" => ("tool_result",json!({"tool_call_id":item["call_id"],"output":item["output"]})),
        "message" => {
            let text=if let Some(text)=item["content"].as_str() {text.to_owned()} else {
                item["content"].as_array().map(|parts|parts.iter().filter_map(|p|p["text"].as_str()).collect::<Vec<_>>().join("\n")).unwrap_or_default()
            };
            ("agent_step_end",json!({"asst_id":format!("interaction:{seq}"),"content":text}))
        },
        _ => ("diagnostic",item),
    };
    Ok(json!({"seq":seq,"event_id":format!("interaction:{seq}"),"session_id":sid,"run_id":run,"kind":kind,"payload":payload,
        "created_at":row.get::<i64,_>("created_at")*1000,"boundary_quality":if row.get::<Option<String>,_>("boundary_item_id").is_some(){"recorded"}else{"legacy_unverified"}}))
}
impl Store {
    /// Exact encrypted interaction lookup for bounded current-run result recall.
    pub async fn tool_result(&self, sid:&str, rid:&str, call_id:&str) -> Result<Option<Value>> {
        let row:Option<Vec<u8>>=sqlx::query_scalar(
            "SELECT item FROM interactions WHERE session_id=? AND run_id=? AND step=? ORDER BY seq DESC LIMIT 1")
            .bind(sid).bind(rid).bind(format!("tool:{call_id}"))
            .fetch_optional(&self.pool).await?;
        row.map(|bytes| self.unseal(&bytes)).transpose()
    }
    pub async fn tool_call_name(&self, sid:&str, rid:&str, call_id:&str) -> Result<Option<String>> {
        let rows:Vec<Vec<u8>>=sqlx::query_scalar(
            "SELECT item FROM interactions WHERE session_id=? AND run_id=? AND step LIKE 'model:%' ORDER BY seq DESC LIMIT 2048")
            .bind(sid).bind(rid).fetch_all(&self.pool).await?;
        for bytes in rows {
            let item=self.unseal(&bytes)?;
            if item["type"]=="function_call" && item["call_id"]==call_id {
                return Ok(item["name"].as_str().map(str::to_owned));
            }
        }
        Ok(None)
    }
    pub async fn private_activity(&self,sid:&str,after:i64)->Result<Value> {
        let mut tx=self.pool.begin().await?;
        let rows=sqlx::query("SELECT i.seq,i.run_id,i.item,i.created_at,b.boundary_item_id FROM interactions i LEFT JOIN activity_boundaries b ON b.interaction_seq=i.seq WHERE i.session_id=? AND i.seq>? ORDER BY i.seq LIMIT 201")
            .bind(sid).bind(after).fetch_all(&mut *tx).await?;
        let more=rows.len()>200;
        let page=&rows[..rows.len().min(200)];
        let events=page.iter().map(|row|event(row,self,sid)).collect::<Result<Vec<_>>>()?;
        let cursor=page.last().map(|r|r.get::<i64,_>("seq")).unwrap_or(after);
        let groups=sqlx::query("SELECT i.run_id,b.boundary_item_id,MIN(i.seq) first_seq,MAX(i.seq) last_seq FROM interactions i LEFT JOIN activity_boundaries b ON b.interaction_seq=i.seq WHERE i.session_id=? GROUP BY i.run_id,b.boundary_item_id ORDER BY MIN(i.seq)")
            .bind(sid).fetch_all(&mut *tx).await?;
        let latest:Option<String>=sqlx::query_scalar("SELECT id FROM chat WHERE session_id=? ORDER BY seq DESC LIMIT 1").bind(sid).fetch_optional(&mut *tx).await?;
        let segments=groups.iter().map(|r|{
            let anchor=boundary(r);
            json!({"id":format!("activity:{}:{}",r.get::<String,_>("run_id"),anchor),"run_id":r.get::<String,_>("run_id"),"boundary_item_id":anchor,
                "first_seq":r.get::<i64,_>("first_seq"),"last_seq":r.get::<i64,_>("last_seq"),"closed":latest.as_deref()!=Some(anchor.as_str()),
                "boundary_quality":if r.get::<Option<String>,_>("boundary_item_id").is_some(){"recorded"}else{"legacy_unverified"}})
        }).collect::<Vec<_>>();
        tx.commit().await?;
        Ok(json!({"events":events,"segments":segments,"cursor":cursor,"has_more":more}))
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn boundaries_split_at_promotion_and_retries_keep_original_anchor() {
        let root=tempfile::tempdir().unwrap();let store=Store::open(root.path()).await.unwrap();
        let mut tx=store.pool.begin().await.unwrap();
        store.insert_chat(&mut tx,"s","r","user",&json!("Hello")).await.unwrap();
        let first=json!({"type":"message","role":"assistant","content":[{"type":"output_text","text":"PRIVATE step"}]});
        store.insert_interaction(&mut tx,"s","r","step:1",&first).await.unwrap();
        tx.commit().await.unwrap();
        let mut run=json!({"id":"r","session_id":"s","status":"running","generation":"g","lease_epoch":0,"input":[],"turns":1});
        store.save_run(&mut run,"working_message",&json!({"id":"working:r:c1","text":"Promoted"})).await.unwrap();
        let mut tx=store.pool.begin().await.unwrap();
        store.insert_interaction(&mut tx,"s","r","step:2",&json!({"type":"function_call","call_id":"tool1","name":"read","arguments":"{}"})).await.unwrap();
        store.insert_interaction(&mut tx,"s","r","step:1",&first).await.unwrap();
        tx.commit().await.unwrap();
        let data=store.private_activity("s",0).await.unwrap();
        assert_eq!(data["events"].as_array().unwrap().len(),2);
        assert_eq!(data["segments"].as_array().unwrap().len(),2);
        assert_eq!(data["segments"][0]["boundary_item_id"],"user:r");
        assert_eq!(data["segments"][1]["boundary_item_id"],"working:r:c1");
        assert_eq!(data["segments"][0]["closed"],true);
        assert_eq!(data["events"][0]["payload"]["content"],"PRIVATE step");
        let bytes:Vec<u8>=sqlx::query_scalar("SELECT item FROM interactions WHERE step='step:1'").fetch_one(&store.pool).await.unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains("PRIVATE step"));
        let (chat,_)=store.chat_snapshot("s").await.unwrap();assert_eq!(chat.len(),2);
        assert!(chat.iter().all(|r|!r.to_string().contains("PRIVATE step")));
        let next=store.private_activity("s",data["cursor"].as_i64().unwrap()).await.unwrap();
        assert!(next["events"].as_array().unwrap().is_empty());assert_eq!(next["segments"],data["segments"]);
        drop(store);let reopened=Store::open(root.path()).await.unwrap();assert_eq!(reopened.private_activity("s",0).await.unwrap(),data);
    }
}
