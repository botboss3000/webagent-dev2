//! Tenant authority and the same live access calculation used by dispatch.
use super::*;
use axum::Extension;
use crate::access::Permission;
use crate::capabilities::CapabilityLevel;
fn denied()->ApiError {ApiError(StatusCode::FORBIDDEN,"Agent ability access unavailable".into())}
async fn authority(app:&App,identity:&Value,id:&str,write:bool)->std::result::Result<Value,ApiError> {
    let user=identity["_user_id"].as_str().ok_or_else(denied)?;
    let rights=app.store.agent_authority(user,id).await.map_err(|_|ApiError(StatusCode::NOT_FOUND,"Agent not found".into()))?;
    if (write && rights["can_manage"]!=true) || (!write && rights["can_manage"]!=true
        && !app.store.live_access(user,id).await.is_ok_and(|a|a.admitted())) {return Err(denied());}
    Ok(rights)
}
fn update_error(error:anyhow::Error)->ApiError {
    if error.to_string().contains("revision") {conflict("Ability settings changed; reload before saving")}
    else {bad("Invalid ability policy update")}
}
fn map_permission(map:&Value,id:&str,default:Permission)->std::result::Result<Permission,ApiError> {
    match map.get(id).or_else(||map.get("*")) {
        Some(v)=>Permission::from_policy(v.as_str().ok_or_else(||bad("Invalid permission"))?).map_err(Into::into),None=>Ok(default),
    }
}
async fn grant_ceiling(app:&App,identity:&Value,abilities:&Value,tools:&Value,previous_abilities:&Value,previous_tools:&Value,base:bool)->std::result::Result<(),ApiError> {
    let account=app.store.account_by_id(identity["_user_id"].as_str().ok_or_else(denied)?).await?.ok_or_else(denied)?;
    let principal=crate::access::Principal::from_account(&account)?;
    let tier=app.store.entitlement_tier(&principal.tier_id).await?.ok_or_else(denied)?;
    for (grants,previous,ability) in [(abilities,previous_abilities,true),(tools,previous_tools,false)] {
        let map=grants.as_object().ok_or_else(||bad("Permission maps required"))?;
        let mut ids:std::collections::BTreeSet<String>=map.keys().cloned().collect();
        if let Some(old)=previous.as_object() {ids.extend(old.keys().cloned());}
        if ability {ids.extend(app.abilities.ids());} else {ids.extend(crate::tools::schemas().iter().filter_map(|s|s["name"].as_str().map(str::to_owned)));}
        ids.insert("*".into());
        let default=if ability && !base {Permission::Deny} else {Permission::Auto};
        for name in ids {
            let next=map_permission(grants,&name,default)?;let before=map_permission(previous,&name,default)?;
            // Compare resolved policies, including removed restrictions and wildcard
            // inheritance. An unchanged higher ceiling can be tightened by a delegate.
            if next.meet(before)==next {continue;}
            let ceiling=if ability {tier.ability_permission(&name)} else {tier.tool_permission(&name)};
            if next.meet(ceiling)!=next {return Err(ApiError(StatusCode::FORBIDDEN,"Grant exceeds your account tier".into()));}
        }
    }
    // Ability and tool permissions interact: raising an ability can expose an
    // inherited Auto tool even when its own map did not change.
    for schema in crate::tools::schemas() {
        let name=schema["name"].as_str().unwrap_or("");
        let owners=app.abilities.tool_abilities(name,&Value::Null,&Value::Null,&Value::Null);
        if owners.is_empty() {continue;}
        let mut next=map_permission(tools,name,Permission::Auto)?;
        let mut before=map_permission(previous_tools,name,Permission::Auto)?;
        let mut ceiling=tier.tool_permission(name);
        for id in owners {
            let default=if base {Permission::Auto} else {Permission::Deny};
            next=next.meet(map_permission(abilities,&id,default)?);
            before=before.meet(map_permission(previous_abilities,&id,default)?);
            ceiling=ceiling.meet(tier.ability_permission(&id));
        }
        if next.meet(before)!=next && next.meet(ceiling)!=next {
            return Err(ApiError(StatusCode::FORBIDDEN,"Ability grant exceeds your account tool tier".into()));
        }
    }
    Ok(())
}
async fn catalog(app:&App,identity:&Value,id:&str,params:&HashMap<String,String>)->Api {
    let rights=authority(app,identity,id,false).await?;
    let caller=identity["_user_id"].as_str().ok_or_else(denied)?;
    let user=params.get("user_id").map(String::as_str).unwrap_or(caller);
    if (user!=caller || params.contains_key("level_id")) && rights["can_manage"]!=true {return Err(denied());}
    // Preview only members of this agent, never arbitrary installation accounts.
    let mut access=app.store.live_access(user,id).await.map_err(|_|denied())?;
    if user!=caller && !access.admitted() {return Err(denied());}
    let levels=app.store.capability_levels(id).await?;
    if let Some(level)=params.get("level_id").filter(|v|!v.is_empty()) {
        access.capability_level=levels.levels.get(level).ok_or_else(||bad("Unknown capability level"))?.clone();
    }
    let scoped=admin_agent_view(app,id).await?;
    let mut visibility=agent_abilities(&scoped).await?;
    let policy=scoped.store.get("policy").await?.unwrap_or_else(policy_default);
    let mut abilities=app.abilities.configuration_catalog(&policy,&visibility);
    let call_counts = app.store.for_user(caller).await?.tool_call_counts(id).await?;
    // The counts describe this caller's recorded model calls, including older sessions.
    for ability in &mut abilities {
        let ability_id=ability["id"].as_str().unwrap_or("").to_owned();
        let ready=ability["available"]==true;
        let permission=access.ability_permission(&ability_id);
        let reason=if !ready {"pending_port"} else if access.tier.ability_permission(&ability_id)==Permission::Deny {"tier_denied"}
            else if access.capability_level.ability_permission(&ability_id)==Permission::Deny {"capability_denied"}
            else if permission==Permission::Deny {"agent_denied"} else if permission==Permission::Ask {"approval_required"} else {"allowed"};
        ability["declared"]=json!(true);ability["ready"]=json!(ready && ability["readiness"]=="ready");
        ability["installed"]=json!(ability["tools"].as_array().is_some_and(|ts|ts.iter().any(|t|t["supported"]==true)));
        // Current adapters have no per-agent credential settings. Unsupported workflows
        // cannot claim configured or ready from their declaration alone.
        ability["configured"]=json!(ready);
        ability["effective"]=json!({"permission":if ready {permission} else {Permission::Deny},"reason":reason});
        let active=json!({"active":[ability_id],"suppressed":[]});let loaded=json!([ability_id]);
        let mut usable=false;
        let mut effective=crate::access::ToolDecision {permission:Permission::Deny,reason:crate::access::Reason::Unavailable};
        let mut first=true;
        if let Some(tools)=ability["tools"].as_array_mut() {
            for tool in tools {
                let name=tool["name"].as_str().unwrap_or("").to_owned();
                tool["call_count"]=json!(call_counts.get(&name).copied().unwrap_or(0));
                let decision=live_tool_decision(&scoped,&access,&name,&policy,&visibility,&active,&loaded,false);
                if first || (decision.permission!=Permission::Deny && (effective.permission==Permission::Deny
                    || decision.permission==Permission::Auto)) {effective=decision;}
                first=false;
                usable |= decision.permission!=Permission::Deny;
                tool["effective"]=serde_json::to_value(decision).map_err(anyhow::Error::from)?;
            }
        }
        ability["available"]=json!(ready && permission!=Permission::Deny && usable);
        if ready {ability["effective"]=serde_json::to_value(effective).map_err(anyhow::Error::from)?;}
    }
    if rights["can_manage"]!=true {
        abilities.retain(|a|a["available"]==true);
        for ability in &mut abilities {
            if let Some(tools)=ability["tools"].as_array_mut() {tools.retain(|t|t["effective"]["permission"]!="deny");}
        }
        // Members see effective entries, never raw private configuration maps.
        visibility["visibility"]=json!({});visibility["skill_visibility"]=json!({});visibility["tool_modes"]=json!({});
    }
    let mut permissions=app.store.agent_ability_permissions(id).await?;
    permissions["tool_permissions"]=app.store.agent_tool_permissions(id).await?["tool_permissions"].clone();
    Ok(Json(json!({"agent_id":id,"catalog_version":app.abilities.version(),"revision":visibility["revision"],
        "visibility":visibility["visibility"],"skill_visibility":visibility["skill_visibility"],"tool_modes":visibility["tool_modes"],"settings":if rights["can_manage"]==true {visibility["settings"].clone()} else {json!({})},
        "authority":{"can_manage":rights["can_manage"],"is_installation_admin":rights["installation_administrator"],"owner_id":rights["owner_id"]},
        "capability_levels":if rights["can_manage"]==true {serde_json::to_value(levels).map_err(anyhow::Error::from)?} else {json!({})},
        "capability_level_id":access.capability_level.id,"permissions":if rights["can_manage"]==true {permissions} else {json!({})},"abilities":abilities})))
}
pub(crate) async fn read(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>)->Api {
    catalog(&app,&identity,&id,&HashMap::new()).await
}
pub(crate) async fn preview(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>,Query(params):Query<HashMap<String,String>>)->Api {
    catalog(&app,&identity,&id,&params).await
}
pub(crate) async fn members(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>)->Api {
    authority(&app,&identity,&id,true).await?;
    Ok(Json(json!({"members":app.store.agent_memberships(&id).await?})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct LevelBody {expected_revision:u64,level:CapabilityLevel}
pub(crate) async fn level_set(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,level)):Path<(String,String)>,Json(body):Json<LevelBody>)->Api {
    let _guard=app.gate.lock().await;authority(&app,&identity,&id,true).await?;
    if body.level.id!=level {return Err(bad("Capability level identity mismatch"));}
    let levels=app.store.capability_levels(&id).await?;
    let previous=levels.levels.get(&level).map(serde_json::to_value).transpose().map_err(anyhow::Error::from)?.unwrap_or(json!({}));
    grant_ceiling(&app,&identity,&serde_json::to_value(&body.level.ability_permissions).map_err(anyhow::Error::from)?,
        &serde_json::to_value(&body.level.tool_permissions).map_err(anyhow::Error::from)?,&previous["ability_permissions"],&previous["tool_permissions"],false).await?;
    let actor_store=app.store.for_user(identity["_user_id"].as_str().ok_or_else(denied)?).await?;
    let saved=actor_store.save_capability_level(&id,body.level,body.expected_revision).await.map_err(update_error)?;
    phase3_api::quiesce_agent(&app,&id).await?;Ok(Json(json!({"capability_levels":saved})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct MemberBody {expected_revision:u64,capability_level_id:String}
pub(crate) async fn member_set(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,user)):Path<(String,String)>,Json(body):Json<MemberBody>)->Api {
    let _guard=app.gate.lock().await;authority(&app,&identity,&id,true).await?;
    let old=app.store.live_access(&user,&id).await?.capability_level;
    let levels=app.store.capability_levels(&id).await?;
    let next=levels.levels.get(&body.capability_level_id).ok_or_else(||bad("Capability level not found"))?;
    grant_ceiling(&app,&identity,&serde_json::to_value(&next.ability_permissions).map_err(anyhow::Error::from)?,
        &serde_json::to_value(&next.tool_permissions).map_err(anyhow::Error::from)?,
        &serde_json::to_value(&old.ability_permissions).map_err(anyhow::Error::from)?,
        &serde_json::to_value(&old.tool_permissions).map_err(anyhow::Error::from)?,false).await?;
    let member=app.store.assign_capability_level(identity["_user_id"].as_str().ok_or_else(denied)?,&user,&id,&body.capability_level_id,body.expected_revision).await.map_err(update_error)?;
    phase3_api::quiesce_agent(&app,&id).await?;Ok(Json(json!({"membership":member})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PermissionsBody {expected_revision:u64,ability_permissions:Value,tool_permissions:Value}
pub(crate) async fn permissions_set(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>,Json(body):Json<PermissionsBody>)->Api {
    let _guard=app.gate.lock().await;authority(&app,&identity,&id,true).await?;
    let previous_abilities=app.store.agent_ability_permissions(&id).await?;let previous_tools=app.store.agent_tool_permissions(&id).await?;
    grant_ceiling(&app,&identity,&body.ability_permissions,&body.tool_permissions,&previous_abilities["ability_permissions"],&previous_tools["tool_permissions"],true).await?;
    let actor_store=app.store.for_user(identity["_user_id"].as_str().ok_or_else(denied)?).await?;
    let value=actor_store.set_agent_permissions(&id,body.expected_revision,body.ability_permissions,body.tool_permissions).await.map_err(update_error)?;
    phase3_api::quiesce_agent(&app,&id).await?;Ok(Json(value))
}
async fn visibility(app:App,identity:Value,id:String,ability:String,tool:Option<String>,skill:Option<String>,body:AbilityVisibilityBody)->Api {
    let _guard=app.gate.lock().await;authority(&app,&identity,&id,true).await?;
    if !app.abilities.known(&ability) || tool.as_ref().is_some_and(|t|!app.abilities.has_tool(&ability,t))
        || skill.as_ref().is_some_and(|name|name!="core" && app.abilities.configuration_skill_asset(&ability,"skill",name).is_none())
        || (skill.as_deref()==Some("core") && app.abilities.configuration_skill(&ability).is_none()) {return Err(bad("Unknown ability, skill or tool"));}
    if !matches!(body.visibility.as_str(),"off"|"visible"|"discoverable") {return Err(bad("Invalid visibility mode"));}
    let mut scoped=admin_agent_view(&app,&id).await?;
    scoped.store=scoped.store.for_user(identity["_user_id"].as_str().ok_or_else(denied)?).await?;
    let mut value=agent_abilities(&scoped).await?;
    if value["revision"]!=body.expected_revision {return Err(conflict("Agent ability revision changed"));}
    let (key,name)=match tool {Some(t)=>("tool_modes",t),None=>match skill {
        Some(t) if t!="core"=>("skill_visibility",format!("{ability}:{t}")),
        Some(_)=>("skill_visibility",ability),
        None=>("visibility",ability),
    }};
    let fallback=if key=="visibility" {"discoverable"} else {"visible"};
    if value[key][&name].as_str().unwrap_or(fallback)==body.visibility {return Ok(Json(value));}
    value[key][name]=json!(body.visibility);value["revision"]=json!(body.expected_revision+1);
    scoped.store.save_agent_abilities(&value,body.expected_revision).await?;
    phase3_api::quiesce_agent(&app,&id).await?;Ok(Json(value))
}
pub(crate) async fn visibility_set(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,ability)):Path<(String,String)>,Json(body):Json<AbilityVisibilityBody>)->Api {
    visibility(app,identity,id,ability,None,None,body).await
}
pub(crate) async fn skill_set(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,ability)):Path<(String,String)>,Json(body):Json<AbilityVisibilityBody>)->Api {
    visibility(app,identity,id,ability,None,Some("core".into()),body).await
}
pub(crate) async fn skill_named_set(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,ability,skill)):Path<(String,String,String)>,Json(body):Json<AbilityVisibilityBody>)->Api {
    visibility(app,identity,id,ability,None,Some(skill),body).await
}
pub(crate) async fn tool_set(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,ability,tool)):Path<(String,String,String)>,Json(body):Json<AbilityVisibilityBody>)->Api {
    visibility(app,identity,id,ability,Some(tool),None,body).await
}
pub(crate) async fn skill_named_content(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,ability,skill)):Path<(String,String,String)>)->Api {
    authority(&app,&identity,&id,true).await?;
    let content=app.abilities.configuration_skill_asset(&ability,"skill",&skill)
        .ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Skill content unavailable; reload the validated catalog".into()))?;
    Ok(Json(json!({"content":content,"catalog_version":app.abilities.version()})))
}
pub(crate) async fn tool_skill_content(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,ability,tool)):Path<(String,String,String)>)->Api {
    authority(&app,&identity,&id,true).await?;
    let content=app.abilities.configuration_skill_asset(&ability,"tool",&tool)
        .ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Tool skill unavailable; reload the validated catalog".into()))?;
    Ok(Json(json!({"content":content,"catalog_version":app.abilities.version()})))
}

// Prompt configuration belongs to the same selected tenant, not the installation.
pub(crate) async fn composition(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>)->Api {
    authority(&app,&identity,&id,true).await?;agent_context_composition(State(app),Path(id)).await
}
pub(crate) async fn skill_content(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,ability)):Path<(String,String)>)->Api {
    authority(&app,&identity,&id,true).await?;
    let content=app.abilities.configuration_skill(&ability)
        .ok_or_else(||ApiError(StatusCode::NOT_FOUND,"Skill content unavailable; reload the validated catalog".into()))?;
    Ok(Json(json!({"content":content,"catalog_version":app.abilities.version()})))
}
pub(crate) async fn slots(State(app):State<App>,Extension(identity):Extension<Value>,Path(id):Path<String>)->Api {
    authority(&app,&identity,&id,true).await?;prompt_slots_api(State(app),Path(id)).await
}
pub(crate) async fn slot(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,name)):Path<(String,String)>)->Api {
    authority(&app,&identity,&id,true).await?;prompt_slot_api(State(app),Path((id,name))).await
}
pub(crate) async fn slot_put(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,name)):Path<(String,String)>,Json(body):Json<PromptSlotPutBody>)->Api {
    let _guard=app.gate.lock().await;authority(&app,&identity,&id,true).await?;
    prompt_slot_put_api(State(app.clone()),Path((id.clone(),name)),Json(body)).await
}
pub(crate) async fn slot_delete(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,name)):Path<(String,String)>,Json(body):Json<PromptSlotDeleteBody>)->Api {
    let _guard=app.gate.lock().await;authority(&app,&identity,&id,true).await?;
    prompt_slot_delete_api(State(app.clone()),Path((id.clone(),name)),Json(body)).await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn delegated_ceiling_checks_removed_denials_wildcards_and_ability_tool_interaction() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let user=app.store.create_user_account("limited_editor","fixture-only-password-hash").await.unwrap();
        let id=user["user_id"].as_str().unwrap();
        app.store.update_account_access(id,1,crate::access::AccountStatus::Active,crate::access::Role::User,"basic").await.unwrap();
        let identity=json!({"_user_id":id});let unchanged=json!({"codebase_admin":"auto","web_access":"auto"});
        assert!(grant_ceiling(&app,&identity,&unchanged,&json!({"*":"auto"}),&unchanged,&json!({"*":"auto","file_read":"deny"}),true).await.is_err());
        assert!(grant_ceiling(&app,&identity,&json!({}),&json!({}),&json!({"base":"deny"}),&json!({}),true).await.is_err());
        assert!(grant_ceiling(&app,&identity,&json!({"codebase_admin":"auto"}),&json!({}),&json!({}),&json!({}),false).await.is_err());
        assert!(grant_ceiling(&app,&identity,&json!({"codebase_admin":"deny"}),&json!({}),&unchanged,&json!({}),false).await.is_ok());
    }
    #[tokio::test]
    async fn named_skill_save_is_independent_and_authorized() {
        let (_root,app)=crate::runtime_tests::fixture().await;
        let identity=json!({"_user_id":app.store.current_user_id()});
        let before=app.store.agent_abilities().await.unwrap();
        let revision=before["revision"].as_u64().unwrap();
        let path=("shared_default".into(),"codebase_admin".into(),"ui_theming".into());
        skill_named_set(State(app.clone()),Extension(identity.clone()),Path(path.clone()),
            Json(AbilityVisibilityBody{visibility:"off".into(),expected_revision:revision})).await.unwrap();
        let saved=app.store.agent_abilities().await.unwrap();
        assert_eq!(saved["skill_visibility"]["codebase_admin:ui_theming"],"off");
        assert_eq!(saved["skill_visibility"]["codebase_admin"],before["skill_visibility"]["codebase_admin"]);
        assert!(skill_named_set(State(app.clone()),Extension(identity.clone()),Path(path.clone()),
            Json(AbilityVisibilityBody{visibility:"visible".into(),expected_revision:revision})).await.is_err());
        assert!(skill_named_content(State(app.clone()),Extension(identity),Path(path.clone())).await.is_ok());
        assert!(skill_named_content(State(app),Extension(json!({"_user_id":"outsider"})),Path(path)).await.is_err());
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SettingsBody {expected_revision:u64,settings:Value}
pub(crate) async fn settings_set(State(app):State<App>,Extension(identity):Extension<Value>,Path((id,ability)):Path<(String,String)>,Json(body):Json<SettingsBody>)->Api {
    let _guard=app.gate.lock().await;authority(&app,&identity,&id,true).await?;
    let valid=match ability.as_str() {
        "git_control"=>body.settings.as_object().is_some_and(|m|m.len()==1 && m.get("notify_repo_changes").is_some_and(Value::is_boolean)),
        "terminal_control"=>body.settings.as_object().is_some_and(|m|m.len()==1 && matches!(m.get("terminal_caution").and_then(Value::as_str),Some("cautious"|"balanced"|"trusted"))),
        "codebase_admin"=>body.settings.as_object().is_some_and(|m|m.len()==1 && matches!(m.get("destructive_caution").and_then(Value::as_str),Some("cautious"|"balanced"|"trusted"))),
        _=>false,
    };
    if !valid{return Err(bad("Unsupported Administrator settings"));}
    let mut scoped=admin_agent_view(&app,&id).await?;
    scoped.store=scoped.store.for_user(identity["_user_id"].as_str().ok_or_else(denied)?).await?;
    let mut value=agent_abilities(&scoped).await?;
    if value["revision"]!=body.expected_revision{return Err(conflict("Agent ability revision changed"));}
    if !value["settings"].is_object(){value["settings"]=json!({});}
    value["settings"][ability]=body.settings;value["revision"]=json!(body.expected_revision+1);
    scoped.store.save_agent_abilities(&value,body.expected_revision).await?;
    phase3_api::quiesce_agent(&app,&id).await?;Ok(Json(value))
}
