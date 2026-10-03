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
pub struct Context {
 pub max_context_bytes:usize,
 /// MODEL-BUDGET-PROFILES: optional operator estimates, not provider tokenizers
 /// or compaction settings. Keys are provider/model; unknown models use bytes.
 #[serde(default)] pub model_profiles:BTreeMap<String,ModelBudget>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelBudget { pub context_tokens:usize, pub output_reserve_tokens:usize, pub bytes_per_token:usize }
impl Context {
 /// Conservative logical-byte proxy; never expands the existing safety ceiling.
 /// Output reserve is budgeting only, not a claim about upstream output controls.
 pub fn ceiling(&self,selection:&crate::provider::ProviderSelection)->Result<usize> {
  let provider=match selection.provider {crate::provider::ProviderId::Deepseek=>"deepseek",crate::provider::ProviderId::OpenaiSubscription=>"openai_subscription"};
  // Luna's requested 128,000 context ceiling is a hard upper bound. Without a
  // provider tokenizer, use one token per logical JSON byte and reserve 8,192
  // for output/protocol overhead. Operator profiles may tighten this ceiling.
  let ceiling=if matches!(selection.provider,crate::provider::ProviderId::OpenaiSubscription)
      && selection.model=="gpt-6-luna" {
      self.max_context_bytes.min(128_000-8_192)
  } else {self.max_context_bytes};
  match self.model_profiles.get(&format!("{provider}/{}",selection.model)) {
   None=>Ok(ceiling), Some(p)=> {
    if p.context_tokens==0 || p.output_reserve_tokens>=p.context_tokens || !(1..=8).contains(&p.bytes_per_token) {bail!("Invalid model budget profile");}
    Ok(ceiling.min((p.context_tokens-p.output_reserve_tokens).checked_mul(p.bytes_per_token).ok_or_else(||anyhow::anyhow!("Model budget overflow"))?))
   }
  }
 }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Cache { pub document_budget_bytes:usize, pub context_budget_bytes:usize }
// Explicit parallel modes are per tool; omitted tools reject multi-call batches.
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tools { pub max_parallel_calls:usize, pub parallel_modes:BTreeMap<String,String> }
impl Default for Tools { fn default()->Self {
 let parallel_modes=["file_list","file_read","file_search","source_review","state_inspect","prompt_slot_read"]
  .into_iter().map(|name|(name.to_owned(),"concurrent".to_owned())).collect();
 Self {max_parallel_calls:4,parallel_modes}
} }
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Workflow { pub max_turns:u64, pub max_attempts:u64, pub max_publication_bytes:usize, pub modules:BTreeMap<String,Vec<String>> }
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config { pub schema_version:u32, pub scheduler:Scheduler, pub context:Context, pub cache:Cache, #[serde(default)] pub tools:Tools, pub workflow:Workflow }
// Bootstrap only. Once present, the validated data file is the sole authority.
impl Default for Config { fn default()->Self { Self {schema_version:2,scheduler:Scheduler {max_active_runs:1,max_unresolved_runs:200,poll_interval_ms:300,recovery_interval_ms:10000},context:Context {max_context_bytes:262144,model_profiles:BTreeMap::new()},cache:Cache {document_budget_bytes:67108864,context_budget_bytes:16777216},tools:Tools::default(),workflow:Workflow {max_turns:50,max_attempts:3,max_publication_bytes:262144,modules:BTreeMap::new()}} } }
impl Config {
 pub fn policy(&self)->Result<workflow::Policy> { workflow::Policy::resolve(Some(&serde_json::json!({"schema_version":1,"max_turns":self.workflow.max_turns,"max_attempts":self.workflow.max_attempts,"max_publication_bytes":self.workflow.max_publication_bytes,"modules":self.workflow.modules,"max_context_bytes":self.context.max_context_bytes}))) }
 pub fn apply_policy(&mut self,p:&workflow::Policy) { self.context.max_context_bytes=p.max_context_bytes;self.workflow=Workflow {max_turns:p.max_turns,max_attempts:p.max_attempts,max_publication_bytes:p.max_publication_bytes,modules:p.modules.clone()}; }
 pub fn validate(&self)->Result<()> { self.policy()?;
 // MODEL-PROFILE-VALIDATION: reject invalid estimates before any provider call.
 for (key,p) in &self.context.model_profiles {
  if self.context.model_profiles.len()>128 || key.len()>256 || !key.contains('/') || p.context_tokens==0 || p.context_tokens>10_000_000 || p.output_reserve_tokens>=p.context_tokens || !(1..=8).contains(&p.bytes_per_token) {bail!("Invalid model budget profile");}
 }
 // BATCH-CONFIG-VALIDATION: unknown names and modes fail closed at startup.
 if !(1..=4).contains(&self.tools.max_parallel_calls) || self.tools.parallel_modes.len()>64 {bail!("Invalid parallel tool configuration");}
 for (name,mode) in &self.tools.parallel_modes {
  if !crate::tools::schemas().iter().any(|schema|schema["name"]==name.as_str())
   || !matches!(mode.as_str(),"concurrent"|"reject") {bail!("Invalid parallel tool mode");}
 }
 if self.schema_version!=2 || !(1..=16).contains(&self.scheduler.max_active_runs) || self.scheduler.max_unresolved_runs<self.scheduler.max_active_runs || self.scheduler.max_unresolved_runs>200 || !(10..=60000).contains(&self.scheduler.poll_interval_ms) || !(1000..=3600000).contains(&self.scheduler.recovery_interval_ms) || self.cache.document_budget_bytes>268435456 || self.cache.context_budget_bytes>67108864 { bail!("Invalid loop runtime configuration"); } Ok(()) }
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

#[cfg(test)] mod parallel_tool_tests {
 use super::*;
 #[test] fn old_config_gets_bounded_defaults_and_bad_modes_fail_closed() {
  let d=tempfile::tempdir().unwrap();let p=d.path().join("config/loop-runtime.json");
  let mut old=serde_json::to_value(Config::default()).unwrap();
  old.as_object_mut().unwrap().remove("tools");
  std::fs::create_dir_all(p.parent().unwrap()).unwrap();
  std::fs::write(&p,serde_json::to_vec(&old).unwrap()).unwrap();
  let mut c=Config::read(&p).unwrap();
  assert_eq!(c.tools.max_parallel_calls,4);
  assert_eq!(c.tools.parallel_modes.get("file_read").map(String::as_str),Some("concurrent"));
  assert!(c.tools.parallel_modes.get("file_write").is_none());
  c.tools.parallel_modes.insert("file_write".into(),"concurrent".into());
  c.validate().unwrap();
  c.tools.parallel_modes.insert("unknown".into(),"concurrent".into());
  assert!(c.validate().is_err());
  c.tools.parallel_modes.remove("unknown");
  c.tools.parallel_modes.insert("file_read".into(),"serial".into());
  assert!(c.validate().is_err());
 }
}
#[cfg(test)] mod model_budget_tests {
 use super::*;
 // MODEL-BUDGET-REGRESSION: unknown models preserve bytes; explicit estimates tighten only.
 #[test] fn profiles_tighten_and_reserve_output() {
  let mut c=Config::default();let selection=crate::provider::ProviderSelection::parse(&serde_json::json!({"provider":"deepseek","model":"fixture"}),"unused").unwrap();
  assert_eq!(c.context.ceiling(&selection).unwrap(),262144);
  c.context.model_profiles.insert("deepseek/fixture".into(),ModelBudget{context_tokens:1000,output_reserve_tokens:100,bytes_per_token:2});
  c.validate().unwrap();assert_eq!(c.context.ceiling(&selection).unwrap(),1800);
  c.context.model_profiles.get_mut("deepseek/fixture").unwrap().output_reserve_tokens=1000;assert!(c.validate().is_err());
 }
}

#[cfg(test)] mod luna_budget_tests {
 use super::*;
 #[test]
 fn luna_ceiling_reserves_headroom_and_cannot_be_expanded_by_a_profile() {
  let luna=crate::provider::ProviderSelection {provider:crate::provider::ProviderId::OpenaiSubscription,model:"gpt-6-luna".into()};
  let other=crate::provider::ProviderSelection {provider:crate::provider::ProviderId::OpenaiSubscription,model:"fixture".into()};
  let mut context=Config::default().context;
  assert_eq!(context.ceiling(&luna).unwrap(),119_808);
  assert_eq!(context.ceiling(&other).unwrap(),262_144);
  context.model_profiles.insert("openai_subscription/gpt-6-luna".into(),
      ModelBudget {context_tokens:1_000_000,output_reserve_tokens:8192,bytes_per_token:8});
  assert_eq!(context.ceiling(&luna).unwrap(),119_808);
  context.model_profiles.insert("openai_subscription/gpt-6-luna".into(),
      ModelBudget {context_tokens:10_000,output_reserve_tokens:1000,bytes_per_token:2});
  assert_eq!(context.ceiling(&luna).unwrap(),18_000);
  context.max_context_bytes=4096;
  assert_eq!(context.ceiling(&luna).unwrap(),4096);
 }
}
