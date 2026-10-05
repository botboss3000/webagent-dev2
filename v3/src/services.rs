//! Installation-owned startup service configuration, separate from agent ability grants.
//! Encrypted app-plane reads are always fresh; caller stores and caches cannot route this policy.
use crate::{access::Principal,store::Store};
use anyhow::{anyhow,bail,Result};
use serde::{Deserialize,Serialize};
use serde_json::json;

pub(crate) const BOOT_ORPHAN_RESUME:&str="boot_orphan_resume";
const CONFIG_KEY:&str="service:boot_orphan_resume";

#[derive(Clone,Debug,Eq,PartialEq,Deserialize,Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ServiceConfig {
    pub enabled:bool,
    pub revision:u64,
}
impl Default for ServiceConfig {
    fn default()->Self {Self {enabled:true,revision:0}}
}
fn decode(store:&Store,blob:Option<Vec<u8>>)->Result<ServiceConfig> {
    let Some(blob)=blob else {return Ok(ServiceConfig::default());};
    let config:ServiceConfig=serde_json::from_value(store.unseal(&blob)?)?;
    if config.revision==0 {bail!("Invalid service configuration revision");}
    Ok(config)
}
pub(crate) async fn boot_orphan_resume_config(store:&Store)->Result<ServiceConfig> {
    let blob:Option<Vec<u8>>=sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
        .bind(CONFIG_KEY).fetch_optional(&store.app_pool).await?;
    decode(store,blob)
}
/// Only startup recovery consumes this setting. Uncertain-effect classification
/// and ordinary expired-lease recovery remain mandatory independent mechanisms.
pub(crate) async fn startup_resume_enabled(store:&Store)->Result<bool> {
    Ok(boot_orphan_resume_config(store).await?.enabled)
}
pub(crate) async fn save_boot_orphan_resume(store:&Store,actor:&str,enabled:bool,expected_revision:u64)->Result<ServiceConfig> {
    if !crate::access::valid_id(actor) {bail!("Installation administrator required");}
    let mut tx=store.app_pool.begin().await?;
    // Even an absent first-start row takes SQLite's writer lock before the read;
    // concurrent creators cannot both accept revision zero.
    sqlx::query("UPDATE docs SET value=value WHERE key=?").bind(CONFIG_KEY).execute(&mut *tx).await?;
    let actor_blob:Vec<u8>=sqlx::query_scalar("SELECT value FROM user_accounts WHERE user_id=?")
        .bind(actor).fetch_optional(&mut *tx).await?.ok_or_else(||anyhow!("Installation administrator required"))?;
    let principal=Principal::from_account(&store.unseal(&actor_blob)?)?;
    if principal.user_id!=actor || !principal.administrator() {bail!("Installation administrator required");}
    let blob:Option<Vec<u8>>=sqlx::query_scalar("SELECT value FROM docs WHERE key=?")
        .bind(CONFIG_KEY).fetch_optional(&mut *tx).await?;
    let previous=decode(store,blob)?;
    if previous.revision!=expected_revision {bail!("Service revision changed");}
    let next=ServiceConfig {enabled,revision:expected_revision.checked_add(1)
        .ok_or_else(||anyhow!("Service revision overflow"))?};
    sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        .bind(CONFIG_KEY).bind(store.seal(&serde_json::to_value(&next)?)?).execute(&mut *tx).await?;
    // The exact authenticated actor and configuration commit together. No tool,
    // tenant administrator or metadata edit may appoint installation authority.
    let audit=json!({"service_id":BOOT_ORPHAN_RESUME,"actor_id":actor,
        "revision":next.revision,"enabled":enabled,"previous_enabled":previous.enabled,
        "changed_at":crate::now(),"applies":"next_loop_startup"});
    sqlx::query("INSERT INTO events(session_id,kind,payload) VALUES('','service_config_changed',?)")
        .bind(store.seal(&audit)?).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(next)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn default_cas_and_audit_are_durable_and_installation_owned() {
        let root=tempfile::tempdir().unwrap();let store=Store::open(root.path()).await.unwrap();
        store.create_admin_account("admin","fixture-password-hash","fixture-generation").await.unwrap();
        let user=store.create_user_account("service-member","long-fixture-password-hash").await.unwrap();
        let user_id=user["user_id"].as_str().unwrap();
        let user_store=store.for_user(user_id).await.unwrap();
        assert_eq!(boot_orphan_resume_config(&store).await.unwrap(),ServiceConfig::default());
        assert!(startup_resume_enabled(&user_store).await.unwrap());
        assert!(save_boot_orphan_resume(&store,user_id,false,0).await.is_err());
        assert_eq!(boot_orphan_resume_config(&store).await.unwrap().revision,0);
        let saved=save_boot_orphan_resume(&user_store,"admin",false,0).await.unwrap();
        assert_eq!(saved,ServiceConfig {enabled:false,revision:1});
        assert_eq!(boot_orphan_resume_config(&store).await.unwrap(),saved);
        assert_eq!(boot_orphan_resume_config(&user_store).await.unwrap(),saved);
        assert!(!startup_resume_enabled(&store).await.unwrap());
        assert!(save_boot_orphan_resume(&store,"admin",true,0).await.is_err());
        let audits:Vec<Vec<u8>>=sqlx::query_scalar("SELECT payload FROM events WHERE kind='service_config_changed'")
            .fetch_all(&store.app_pool).await.unwrap();
        assert_eq!(audits.len(),1);
        let audit=store.unseal(&audits[0]).unwrap();
        assert_eq!(audit["actor_id"],"admin");assert_eq!(audit["enabled"],false);
        assert_eq!(audit["revision"],1);assert_eq!(audit["previous_enabled"],true);
        let user_count:i64=sqlx::query_scalar("SELECT COUNT(*) FROM docs WHERE key=?")
            .bind(CONFIG_KEY).fetch_one(&user_store.pool).await.unwrap();
        assert_eq!(user_count,0);
        let reopened=Store::open(root.path()).await.unwrap();
        assert_eq!(boot_orphan_resume_config(&reopened).await.unwrap(),saved);
    }
    #[tokio::test]
    async fn concurrent_first_start_writes_have_one_winner_and_one_audit() {
        let root=tempfile::tempdir().unwrap();let store=Store::open(root.path()).await.unwrap();
        store.create_admin_account("admin","fixture-password-hash","fixture-generation").await.unwrap();
        let (a,b)=tokio::join!(save_boot_orphan_resume(&store,"admin",false,0),
            save_boot_orphan_resume(&store,"admin",true,0));
        assert_eq!(usize::from(a.is_ok())+usize::from(b.is_ok()),1);
        let loser=if a.is_err() {a.unwrap_err()} else {b.unwrap_err()};
        assert_eq!(loser.to_string(),"Service revision changed");
        assert_eq!(boot_orphan_resume_config(&store).await.unwrap().revision,1);
        let count:i64=sqlx::query_scalar("SELECT COUNT(*) FROM events WHERE kind='service_config_changed'")
            .fetch_one(&store.app_pool).await.unwrap();
        assert_eq!(count,1);
    }
    #[tokio::test]
    async fn invalid_configuration_never_defaults_to_automatic_resume() {
        let root=tempfile::tempdir().unwrap();let store=Store::open(root.path()).await.unwrap();
        for config in [json!({"enabled":true,"revision":0}),json!({"enabled":"true","revision":1}),
            json!({"enabled":true,"revision":1,"unknown":"field"})] {
            sqlx::query("INSERT INTO docs(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
                .bind(CONFIG_KEY).bind(store.seal(&config).unwrap()).execute(&store.app_pool).await.unwrap();
            assert!(startup_resume_enabled(&store).await.is_err());
        }
    }
}
