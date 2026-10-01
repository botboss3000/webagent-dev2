use axum::{http::StatusCode, Json};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{fs, path::Path};

#[derive(Deserialize)]
struct Descriptor {
    id: String,
    label: String,
    #[serde(default)]
    order: i64,
    html: Option<String>,
    css: Option<Vec<String>>,
    entry: Option<String>,
}

fn asset(folder: &Path, base: &str, name: &str) -> Result<String, String> {
    if name.is_empty() || name == "." || name == ".."
        || !name.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
        || !folder.join(name).is_file() || folder.join(name).is_symlink()
    {
        return Err(format!("Invalid page asset: {base}/{name}"));
    }
    Ok(format!("{base}/{name}"))
}

fn discover(ui: &Path) -> Result<Value, String> {
    let root = ui.join("main-panel");
    let mut pages = Vec::new();
    for item in fs::read_dir(&root).map_err(|e| format!("Cannot read main page directory: {e}"))? {
        let folder = item.map_err(|e| e.to_string())?.path();
        if !folder.is_dir() || folder.is_symlink() || folder.file_name().unwrap().to_string_lossy().starts_with('_') {
            continue;
        }
        let descriptor = folder.join("page.json");
        if !descriptor.is_file() { continue; }
        let page: Descriptor = serde_json::from_str(
            &fs::read_to_string(&descriptor).map_err(|e| e.to_string())?
        ).map_err(|e| format!("Invalid {}: {e}", descriptor.display()))?;
        let folder_name = folder.file_name().unwrap().to_string_lossy();
        if page.id != folder_name || page.label.trim().is_empty()
            || !page.id.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-')) {
            return Err(format!("Invalid page identity: {}", descriptor.display()));
        }
        let base = format!("/v3-ui/main-panel/{}", page.id);
        let html = page.html.as_deref().map(|name| asset(&folder, &base, name)).transpose()?;
        let css = page.css.unwrap_or_default().iter()
            .map(|name| asset(&folder, &base, name)).collect::<Result<Vec<_>, _>>()?;
        let entry = page.entry.as_deref().map(|name| asset(&folder, &base, name)).transpose()?;
        pages.push(json!({"id":page.id,"label":page.label,"order":page.order,
            "html":html,"css":css,"entry":entry}));
    }
    pages.sort_by(|a,b| a["order"].as_i64().cmp(&b["order"].as_i64())
        .then_with(|| a["label"].as_str().cmp(&b["label"].as_str())));
    Ok(json!({"main":pages}))
}

pub async fn catalog() -> Result<Json<Value>, (StatusCode, String)> {
    let ui = std::env::var("V3_UI_ROOT").unwrap_or_else(|_| "/opt/v3/ui".into());
    discover(Path::new(&ui)).map(Json).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn discovers_drop_in_pages_in_order() {
        let root = tempfile::tempdir().unwrap();
        for (name, order) in [("later", 8), ("first", 1)] {
            let folder = root.path().join("main-panel").join(name);
            fs::create_dir_all(&folder).unwrap();
            fs::write(folder.join("page.js"), "export function start() {}").unwrap();
            fs::write(folder.join("page.json"),
                format!(r#"{{"id":"{name}","label":"{name}","order":{order},"entry":"page.js"}}"#)
            ).unwrap();
        }
        let pages = discover(root.path()).unwrap();
        assert_eq!(pages["main"][0]["id"], "first");
        assert_eq!(pages["main"][1]["entry"], "/v3-ui/main-panel/later/page.js");
    }
    #[test]
    fn rejects_asset_escape() {
        let root = tempfile::tempdir().unwrap();
        let folder = root.path().join("main-panel/agents");
        fs::create_dir_all(&folder).unwrap();
        fs::write(folder.join("page.json"),
            r#"{"id":"agents","label":"Agents","entry":"../app.js"}"#).unwrap();
        assert!(discover(root.path()).is_err());
    }
}
