//! Private title/chat-visible text search. No plaintext index or runtime input scan.
use super::*;
use serde::{Deserialize, Serialize};
const ROWS: usize = 128;
const RECORD_BYTES: usize = 2 * 1024 * 1024;
const PAGE_BYTES: usize = 8 * 1024 * 1024;

#[derive(Debug)]
pub struct InvalidSearch;
impl std::fmt::Display for InvalidSearch {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("Invalid or expired search parameters/continuation") }
}
impl std::error::Error for InvalidSearch {}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Cursor {
    version: u8, user: String, query: String, status: String, expires: u64,
    last_key: String, upper_key: String, titles_done: bool, seq: i64, upper_seq: i64,
    skipped: usize,
}
fn invalid() -> anyhow::Error { InvalidSearch.into() }
// Offsets refer to original Unicode characters, not byte offsets in lowercase text.
fn snippet(text: &str, query: &str) -> Option<String> {
    let offset = text.to_lowercase().find(query)?;
    let mut bytes = 0;
    let mut position: usize = 0;
    for ch in text.chars() {
        if bytes >= offset { break; }
        bytes += ch.to_lowercase().collect::<String>().len();
        position += 1;
    }
    let start = position.saturating_sub(60);
    Some(text.chars().skip(start).take(240).map(|c| if c.is_control() { ' ' } else { c }).collect())
}
impl Store {
    pub async fn search_private_sessions(&self, query: &str, status: &str, limit: usize, continuation: Option<&str>) -> Result<Value> {
        let query = query.trim().to_lowercase();
        if query.is_empty() || query.len() > 256 || query.chars().count() > 128 || query.chars().any(char::is_control)
            || !matches!(status, "active" | "recycled") || !(1..=50).contains(&limit) { return Err(invalid()); }
        let mut tx = self.pool.begin().await?;
        let mut cursor = if let Some(token) = continuation {
            if token.len() > 4096 || token.len() % 2 != 0 || !token.bytes().all(|b| b.is_ascii_hexdigit()) { return Err(invalid()); }
            let bytes: Vec<u8> = (0..token.len()).step_by(2).map(|i| u8::from_str_radix(&token[i..i+2],16).map_err(|_| invalid())).collect::<Result<_>>()?;
            let c: Cursor = serde_json::from_value(self.unseal(&bytes).map_err(|_| invalid())?).map_err(|_| invalid())?;
            if c.version != 1 || c.user != self.current_user_id() || c.query != query || c.status != status || c.expires <= crate::now() || c.seq < 0 || c.seq > c.upper_seq { return Err(invalid()); }
            c
        } else {
            Cursor { version: 1, user: self.current_user_id().to_owned(), query: query.clone(), status: status.to_owned(), expires: crate::now()+900,
                last_key: "session:".into(), upper_key: sqlx::query_scalar("SELECT COALESCE(MAX(key),'session:') FROM docs WHERE key GLOB 'session:*'").fetch_one(&mut *tx).await?,
                titles_done: false, seq: 0, upper_seq: sqlx::query_scalar("SELECT COALESCE(MAX(seq),0) FROM chat").fetch_one(&mut *tx).await?, skipped: 0 }
        };
        let mut results = Vec::new();
        let mut scanned = 0;
        let mut bytes = 0;
        let initial_skipped = cursor.skipped;
        while scanned < ROWS && results.len() < limit {
            if !cursor.titles_done {
                // CASE avoids fetching oversized encrypted checkpoints into memory.
                let row = sqlx::query("SELECT key,length(value) AS size,CASE WHEN length(value)<=? THEN value ELSE NULL END AS value FROM docs WHERE key GLOB 'session:*' AND key>? AND key<=? ORDER BY key LIMIT 1")
                    .bind(RECORD_BYTES as i64).bind(&cursor.last_key).bind(&cursor.upper_key).fetch_optional(&mut *tx).await?;
                let Some(row) = row else { cursor.titles_done = true; continue; };
                let size = row.get::<i64,_>("size") as usize;
                if size <= RECORD_BYTES && bytes + size > PAGE_BYTES { break; }
                cursor.last_key = row.get("key"); scanned += 1;
                if size > RECORD_BYTES { cursor.skipped += 1; continue; }
                bytes += size;
                let doc = self.unseal(&row.get::<Vec<u8>,_>("value"))?;
                if doc["status"] != status { continue; }
                if let Some(text) = snippet(doc["title"].as_str().unwrap_or(""), &query) {
                    results.push(json!({"kind":"title","session_id":cursor.last_key.trim_start_matches("session:"),"agent_id":doc["agent_id"].as_str().unwrap_or("shared_default"),"title":doc["title"],"snippet":text}));
                }
            } else {
                let row = sqlx::query("SELECT c.seq,c.session_id,c.role,length(c.content) AS size,length(d.value) AS doc_size,CASE WHEN length(c.content)<=? THEN c.content ELSE NULL END AS content,CASE WHEN length(d.value)<=? THEN d.value ELSE NULL END AS value FROM chat c JOIN docs d ON d.key='session:'||c.session_id WHERE c.seq>? AND c.seq<=? AND c.role IN ('user','assistant') ORDER BY c.seq LIMIT 1")
                    .bind(RECORD_BYTES as i64).bind(RECORD_BYTES as i64).bind(cursor.seq).bind(cursor.upper_seq).fetch_optional(&mut *tx).await?;
                let Some(row) = row else { cursor.seq = cursor.upper_seq; break; };
                let size = row.get::<i64,_>("size") as usize;
                let doc_size = row.get::<i64,_>("doc_size") as usize;
                if size <= RECORD_BYTES && doc_size <= RECORD_BYTES && bytes + size + doc_size > PAGE_BYTES { break; }
                cursor.seq = row.get("seq"); scanned += 1;
                if size > RECORD_BYTES || doc_size > RECORD_BYTES { cursor.skipped += 1; continue; }
                bytes += size + doc_size;
                let doc = self.unseal(&row.get::<Vec<u8>,_>("value"))?;
                if doc["status"] != status { continue; }
                let content = self.unseal(&row.get::<Vec<u8>,_>("content"))?;
                if let Some(text) = content.as_str().and_then(|s| snippet(s, &query)) {
                    results.push(json!({"kind":"message","session_id":row.get::<String,_>("session_id"),"agent_id":doc["agent_id"].as_str().unwrap_or("shared_default"),"title":doc["title"],"seq":cursor.seq,"role":row.get::<String,_>("role"),"snippet":text}));
                }
            }
        }
        tx.commit().await?;
        let complete = cursor.titles_done && cursor.seq >= cursor.upper_seq;
        let next = if complete { None } else { Some(self.seal(&serde_json::to_value(&cursor)?)?.iter().map(|b| format!("{b:02x}")).collect::<String>()) };
        Ok(json!({"results":results,"continuation":next,"scan_complete":complete,"scanned_records":scanned,"scanned_bytes":bytes,"skipped_oversized":cursor.skipped-initial_skipped,"skipped_oversized_total":cursor.skipped,"incomplete_due_to_size":cursor.skipped>0,"bounds":{"records":ROWS,"record_bytes":RECORD_BYTES,"page_bytes":PAGE_BYTES},"order":"titles_by_session_id_then_messages_by_sequence","consistency":"initial_upper_bounds_current_content"}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn private_search_bounds_continuation_and_encryption() {
        let root = tempfile::tempdir().unwrap(); let store = Store::open(root.path()).await.unwrap();
        store.put("session:a",&json!({"id":"a","title":"Needle α","checkpoint_input":[{"content":"internal-only"}]})).await.unwrap();
        store.put("session:b",&json!({"id":"b","title":"other"})).await.unwrap();
        let mut tx = store.pool.begin().await.unwrap();
        store.insert_chat(&mut tx,"a","r","assistant",&json!("你好 NEEDLE <script>alert(1)</script>")).await.unwrap(); tx.commit().await.unwrap();
        let first = store.search_private_sessions("needle","active",1,None).await.unwrap();
        assert_eq!(first["results"][0]["kind"],"title");
        let token = first["continuation"].as_str().unwrap(); assert!(!token.contains("needle"));
        let next = store.search_private_sessions("NEEDLE","active",50,Some(token)).await.unwrap();
        assert_eq!(next["results"][0]["kind"],"message"); assert!(next["results"][0]["snippet"].as_str().unwrap().contains("<script>"));
        assert_eq!(next["scan_complete"],true);
        assert!(store.search_private_sessions("different","active",50,Some(token)).await.is_err());
        assert!(store.search_private_sessions("needle","recycled",50,Some(token)).await.is_err());
        let mut tampered = token.to_owned(); tampered.replace_range(0..2,"ff"); assert!(store.search_private_sessions("needle","active",50,Some(&tampered)).await.is_err());
        sqlx::query("INSERT INTO user_accounts(user_id,username,value) VALUES('searchother','searchother',?)")
            .bind(store.seal(&json!({"user_id":"searchother","username":"searchother"})).unwrap()).execute(&store.app_pool).await.unwrap();
        let other = store.for_user("searchother").await.unwrap(); assert!(other.search_private_sessions("needle","active",50,Some(token)).await.is_err());
        assert!(other.search_private_sessions("needle","active",50,None).await.unwrap()["results"].as_array().unwrap().is_empty());
        assert!(store.search_private_sessions("internal-only","active",50,None).await.unwrap()["results"].as_array().unwrap().is_empty());
        for i in 0..140 {store.put(&format!("session:z{i:03}"),&json!({"id":format!("z{i:03}"),"title":"missing"})).await.unwrap();}
        let page=store.search_private_sessions("absent","active",50,None).await.unwrap(); assert_eq!(page["scanned_records"],128); assert!(page["continuation"].is_string());
        let end=store.search_private_sessions("absent","active",50,page["continuation"].as_str()).await.unwrap(); assert_eq!(end["scan_complete"],true);
        let reopened = Store::open(root.path()).await.unwrap(); assert_eq!(reopened.search_private_sessions("needle","active",50,Some(token)).await.unwrap()["results"].as_array().unwrap().len(),1);
        let raw:Vec<u8>=sqlx::query_scalar("SELECT value FROM docs WHERE key='session:a'").fetch_one(&store.pool).await.unwrap(); assert!(!String::from_utf8_lossy(&raw).contains("Needle"));
    }
    #[tokio::test]
    async fn private_search_byte_budget_preserves_unscanned_matches() {
        let root = tempfile::tempdir().unwrap(); let store = Store::open(root.path()).await.unwrap();
        for i in 0..6 {
            store.put(&format!("session:s{i}"), &json!({"id":format!("s{i}"),"title":"needle","checkpoint_input":"x".repeat(RECORD_BYTES-2048)})).await.unwrap();
        }
        let first = store.search_private_sessions("needle","active",50,None).await.unwrap();
        assert_eq!(first["results"].as_array().unwrap().len(),4);
        assert!(first["scanned_bytes"].as_u64().unwrap() <= PAGE_BYTES as u64);
        // Later keys above the initial upper bound are excluded by this search.
        store.put("session:z", &json!({"id":"z","title":"needle"})).await.unwrap();
        let next = store.search_private_sessions("needle","active",50,first["continuation"].as_str()).await.unwrap();
        assert_eq!(next["results"].as_array().unwrap().len(),2);
        assert_eq!(next["results"][0]["session_id"],"s4");
        assert_eq!(next["scan_complete"],true);
        assert_eq!(next["skipped_oversized_total"],0);
    }
    #[tokio::test]
    async fn private_search_recycle_size_expiry_and_validation() {
        let root=tempfile::tempdir().unwrap(); let store=Store::open(root.path()).await.unwrap();
        store.put("session:a",&json!({"id":"a","title":"needle","status":"recycled"})).await.unwrap();
        store.put("session:b",&json!({"id":"b","title":"needle","checkpoint_input":"x".repeat(RECORD_BYTES)})).await.unwrap();
        let result=store.search_private_sessions("needle","active",50,None).await.unwrap(); assert_eq!(result["skipped_oversized_total"],1); assert_eq!(result["incomplete_due_to_size"],true); assert!(result["results"].as_array().unwrap().is_empty());
        assert_eq!(store.search_private_sessions("needle","recycled",50,None).await.unwrap()["results"][0]["session_id"],"a");
        for (q,s,l) in [("","active",1),("needle","all",1),("needle","active",0),("needle","active",51),("bad\nquery","active",1)] { assert!(store.search_private_sessions(q,s,l,None).await.is_err()); }
        let page=store.search_private_sessions("needle","recycled",1,None).await.unwrap();
        let token=page["continuation"].as_str().unwrap();
        let bytes:Vec<u8>=(0..token.len()).step_by(2).map(|i|u8::from_str_radix(&token[i..i+2],16).unwrap()).collect();
        let mut c:Cursor=serde_json::from_value(store.unseal(&bytes).unwrap()).unwrap(); c.expires=0;
        let expired=store.seal(&serde_json::to_value(c).unwrap()).unwrap().iter().map(|b|format!("{b:02x}")).collect::<String>();
        assert!(store.search_private_sessions("needle","recycled",1,Some(&expired)).await.is_err());
        assert!(snippet("İstanbul 你好", "i\u{307}stanbul").is_some());
    }
}
