//! Non-secret runtime authority: <state root>/config/loop-runtime.json.
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, path::{Path, PathBuf}, io::Write};
use crate::workflow;
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scheduler { pub max_active_runs:usize, pub max_unresolved_runs:usize, pub poll_interval_ms:u64, pub recovery_interval_ms:u64 }
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Context { pub max_context_bytes:usize }
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Cache { pub document_budget_bytes:usize, pub context_budget_bytes:usize }
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Workflow { pub max_turns:u64, pub max_attempts:u64, pub max_publication_bytes:usize, pub modules:BTreeMap<String,Vec<String>> }
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config { pub schema_version:u32, pub scheduler:Scheduler, pub context:Context, pub cache:Cache, pub workflow:Workflow }
// Bootstrap only. Once present, the validated data file is the sole authority.
impl Default for Config { fn default()->Self { Self {schema_version:2,scheduler:Scheduler {max_active_runs:1,max_unresolved_runs:200,poll_interval_ms:300,recovery_interval_ms:10000},context:Context {max_context_bytes:262144},cache:Cache {document_budget_bytes:67108864,context_budget_bytes:16777216},workflow:Workflow {max_turns:50,max_attempts:3,max_publication_bytes:262144,modules:BTreeMap::new()}} } }
impl Config {
 pub fn policy(&self)->Result<workflow::Policy> { workflow::Policy::resolve(Some(&serde_json::json!({"schema_version":1,"max_turns":self.workflow.max_turns,"max_attempts":self.workflow.max_attempts,"max_publication_bytes":self.workflow.max_publication_bytes,"modules":self.workflow.modules,"max_context_bytes":self.context.max_context_bytes}))) }
 pub fn apply_policy(&mut self,p:&workflow::Policy) { self.context.max_context_bytes=p.max_context_bytes;self.workflow=Workflow {max_turns:p.max_turns,max_attempts:p.max_attempts,max_publication_bytes:p.max_publication_bytes,modules:p.modules.clone()}; }
 pub fn validate(&self)->Result<()> { self.policy()?; if self.schema_version!=2 || !(1..=16).contains(&self.scheduler.max_active_runs) || self.scheduler.max_unresolved_runs<self.scheduler.max_active_runs || self.scheduler.max_unresolved_runs>200 || !(10..=60000).contains(&self.scheduler.poll_interval_ms) || !(1000..=3600000).contains(&self.scheduler.recovery_interval_ms) || self.cache.document_budget_bytes>268435456 || self.cache.context_budget_bytes>67108864 { bail!("Invalid loop runtime configuration"); } Ok(()) }
 pub fn read(path:&Path)->Result<Self> { let mut value:serde_json::Value=serde_json::from_slice(&std::fs::read(path)?)?;
 // Schema 1's reserve was redundant. Preserve all other settings and reject unknown fields.
 if value["schema_version"]==1 { value["schema_version"]=serde_json::json!(2); if let Some(c)=value["context"].as_object_mut(){c.remove("summary_reserve_bytes");} }
 let c:Self=serde_json::from_value(value)?;c.validate()?;Ok(c) }
 pub fn persist(&self,path:&Path)->Result<()> { self.validate()?;let parent=path.parent().ok_or_else(||anyhow::anyhow!("Config parent missing"))?;std::fs::create_dir_all(parent)?;let temp=parent.join(format!(".loop-runtime.{}.tmp",uuid::Uuid::new_v4()));let result=(||->Result<()> { let mut f=std::fs::OpenOptions::new().write(true).create_new(true).open(&temp)?;f.write_all(&serde_json::to_vec_pretty(self)?)?;f.sync_all()?;std::fs::rename(&temp,path)?;std::fs::File::open(parent)?.sync_all()?;Ok(()) })();if result.is_err(){let _=std::fs::remove_file(temp);}result }
 pub async fn initialize(root:&Path,store:&crate::store::Store)->Result<(Self,PathBuf)> { let path=root.join("config/loop-runtime.json");let config=match std::fs::metadata(&path) { Ok(_)=>{let c=Self::read(&path)?;
 // Preserve the exact schema-1 file before rewriting schema 2. A conflicting
 // backup fails rather than silently losing a prior operator configuration.
 let original=std::fs::read(&path)?;let v:serde_json::Value=serde_json::from_slice(&original)?;
 if v["schema_version"]==1 {let backup=path.with_extension("json.v1.bak");match std::fs::OpenOptions::new().write(true).create_new(true).open(&backup){Ok(mut f)=>{f.write_all(&original)?;f.sync_all()?;},Err(e) if e.kind()==std::io::ErrorKind::AlreadyExists=>{if std::fs::read(&backup)?!=original {bail!("Conflicting loop runtime migration backup");}},Err(e)=>return Err(e.into())};c.persist(&path)?;}c},Err(e) if e.kind()==std::io::ErrorKind::NotFound=>{let mut c=Self::default();if let Some(old)=store.get("workflow_policy").await? {let mut old=old; if let Some(o)=old.as_object_mut(){o.remove("summary_reserve_bytes");} let p=workflow::Policy::resolve(Some(&old))?;c.apply_policy(&p);} c.persist(&path)?;c},Err(e)=>return Err(e.into()) };Ok((config,path)) }
}
#[cfg(test)] mod tests { use super::*; #[test] fn defaults_and_validation(){let c=Config::default();c.validate().unwrap();assert_eq!(c.scheduler.max_active_runs,1);let mut c=c;c.scheduler.max_active_runs=0;assert!(c.validate().is_err());} #[test] fn atomic_roundtrip_and_invalid_present(){let d=tempfile::tempdir().unwrap();let p=d.path().join("config/loop-runtime.json");Config::default().persist(&p).unwrap();assert_eq!(Config::read(&p).unwrap().context.max_context_bytes,262144);std::fs::write(&p,b"{}").unwrap();assert!(Config::read(&p).is_err());} }

#[cfg(test)] mod migration_tests {
 use super::*;
 #[tokio::test]
 async fn legacy_runtime_migrates_with_exact_backup_and_rejects_duplicate_authority() {
  let root=tempfile::tempdir().unwrap();
  let store=crate::store::Store::open(root.path()).await.unwrap();
  let path=root.path().join("config/loop-runtime.json");
  std::fs::create_dir_all(path.parent().unwrap()).unwrap();
  let mut old=serde_json::to_value(Config::default()).unwrap();
  old["schema_version"]=serde_json::json!(1);
  old["context"]["summary_reserve_bytes"]=serde_json::json!(999);
  old["scheduler"]["poll_interval_ms"]=serde_json::json!(700);
  let original=serde_json::to_vec(&old).unwrap();
  std::fs::write(&path,&original).unwrap();
  let (config,_)=Config::initialize(root.path(),&store).await.unwrap();
  assert_eq!(config.schema_version,2);assert_eq!(config.scheduler.poll_interval_ms,700);
  assert_eq!(std::fs::read(path.with_extension("json.v1.bak")).unwrap(),original);
  let migrated:serde_json::Value=serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
  assert!(migrated["context"].get("summary_reserve_bytes").is_none());
  assert!(workflow::Policy::resolve(Some(&serde_json::json!({"summary_reserve_bytes":999}))).is_err());
  Config::initialize(root.path(),&store).await.unwrap();
 }
}
