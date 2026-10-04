//! Account model preferences. Browser auth/CSRF are supplied by the owning router.
use crate::{App,Api,account_catalog::Preferences};
use axum::{extract::State,Extension,Json};
use serde_json::Value;
pub async fn read(State(app):State<App>,Extension(identity):Extension<Value>)->Api {
    let user=identity["_user_id"].as_str().unwrap_or("");
    Ok(Json(app.store.account_model_choices(user).await?))
}
pub async fn save(State(app):State<App>,Extension(identity):Extension<Value>,Json(input):Json<Preferences>)->Api {
    let _guard=app.gate.lock().await;
    let user=identity["_user_id"].as_str().unwrap_or("");
    let current=app.store.account_model_preferences(user).await?;
    if input.revision!=current.revision {return Err(crate::conflict("Account catalog changed; reload before saving"));}
    app.store.save_account_model_preferences(user,&input).await
        .map_err(|_|crate::bad("Model preferences invalid or no longer entitled"))?;
    Ok(Json(app.store.account_model_choices(user).await?))
}

/// A narrow write avoids replaying aliases or selections from an unrelated editor.
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DefaultUpdate {
    expected_revision: u64,
    default_entry_id: Option<String>,
}
pub async fn set_default(State(app):State<App>, Extension(identity):Extension<Value>,
    Json(input):Json<DefaultUpdate>)->Api {
    let _guard=app.gate.lock().await;
    let user=identity["_user_id"].as_str().unwrap_or("");
    let mut preferences=app.store.account_model_preferences(user).await?;
    if input.expected_revision!=preferences.revision {
        return Err(crate::conflict("Account catalog changed; reload before saving"));
    }
    preferences.default_entry_id=input.default_entry_id;
    app.store.save_account_model_preferences(user,&preferences).await
        .map_err(|_|crate::bad("Default model is no longer available"))?;
    Ok(Json(app.store.account_model_choices(user).await?))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn default_write_is_fenced_and_rejects_forged_sources() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let identity=serde_json::json!({"_user_id":"admin"});
        let Json(saved)=set_default(State(app.clone()),Extension(identity.clone()),
            Json(DefaultUpdate{expected_revision:0,default_entry_id:None})).await.unwrap();
        assert_eq!(saved["revision"],1);
        assert!(set_default(State(app.clone()),Extension(identity.clone()),
            Json(DefaultUpdate{expected_revision:0,default_entry_id:None})).await.is_err());
        assert!(set_default(State(app.clone()),Extension(identity),
            Json(DefaultUpdate{expected_revision:1,default_entry_id:Some("platform:base:forged".into())})).await.is_err());
        assert_eq!(app.store.account_model_preferences("admin").await.unwrap().revision,1);
        assert!(serde_json::from_value::<DefaultUpdate>(serde_json::json!({
            "expected_revision":1,"default_entry_id":null,"credential_ref":"forged"})).is_err());
    }
}

/// Source rehydration and persistence own the security boundary, not a client catalog replay.
pub async fn adopt(State(app):State<App>,Extension(identity):Extension<Value>,
    axum::extract::Path(agent):axum::extract::Path<String>,Json(input):Json<crate::account_catalog::Adoption>)->Api {
    let _guard=app.gate.lock().await;
    let user=identity["_user_id"].as_str().unwrap_or("");
    Ok(Json(app.store.adopt_byo_source(user,&agent,&input).await
        .map_err(|_|crate::bad("Adoption rejected; verify source, authority and roster revision"))?))
}
