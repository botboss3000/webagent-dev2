//! Disposable accounting owned by one executing task. Durable run input is authoritative.
//! Call reset after replacing history (compaction); otherwise input is append-only.
use anyhow::Result;
use serde::Serialize;
use serde_json::Value;
use crate::context_assembly::{HistoryReport, history_report};
struct Counter(usize);
impl std::io::Write for Counter { fn write(&mut self,b:&[u8])->std::io::Result<usize>{self.0+=b.len();Ok(b.len())} fn flush(&mut self)->std::io::Result<()>{Ok(())} }
pub fn json_size<T:Serialize+?Sized>(v:&T)->Result<usize>{let mut n=Counter(0);serde_json::to_writer(&mut n,v)?;Ok(n.0)}
#[derive(Default)]
pub struct ContextState { measured:usize, sum:usize, report:Option<HistoryReport>, pub measured_items:usize }
impl ContextState {
 pub fn reset(&mut self){self.measured=0;self.sum=0;self.report=None;}
 pub fn sync(&mut self,history:&[Value])->Result<()> {
  if history.len()<self.measured {self.reset();}
  if self.report.is_none(){self.report=Some(history_report(&[])?);}
  for item in &history[self.measured..] {let delta=history_report(std::slice::from_ref(item))?;self.sum+=delta.messages.bytes+delta.tool_calls.bytes+delta.tool_results.bytes+delta.reasoning.bytes+delta.other.bytes;let r=self.report.as_mut().unwrap();
   for (a,b) in [(&mut r.messages,&delta.messages),(&mut r.tool_calls,&delta.tool_calls),(&mut r.tool_results,&delta.tool_results),(&mut r.reasoning,&delta.reasoning),(&mut r.other,&delta.other)] {a.bytes+=b.bytes;a.items+=b.items;}
   r.argument_content_bytes+=delta.argument_content_bytes;r.result_content_bytes+=delta.result_content_bytes;self.measured_items+=1;
  }
  self.measured=history.len();Ok(())
 }
 pub fn bytes(&self)->usize{2+self.sum+self.measured.saturating_sub(1)}
 pub fn report(&self)->HistoryReport{self.report.as_ref().unwrap().clone()}
}
#[cfg(test)] mod tests {use super::*;use serde_json::json;
 #[test] fn append_only_measures_delta_and_reset_handles_replacement(){let mut s=ContextState::default();let mut h=vec![json!({"role":"user","content":"\" unicode é"})];s.sync(&h).unwrap();assert_eq!(s.bytes(),serde_json::to_vec(&h).unwrap().len());s.sync(&h).unwrap();assert_eq!(s.measured_items,1);h.push(json!({"type":"function_call","arguments":"{}"}));s.sync(&h).unwrap();assert_eq!(s.measured_items,2);assert_eq!(s.bytes(),json_size(&h).unwrap());h[0]=json!({"role":"user","content":"summary"});s.reset();s.sync(&h).unwrap();assert_eq!(s.bytes(),json_size(&h).unwrap());}
}

#[cfg(test)] mod measurements {
 use super::*;use serde_json::json;use std::time::Instant;
 #[test]
 fn repeated_accounting_measurement() {
  let history=(0..512).map(|i|json!({"role":"user","content":format!("{}-{i}","x".repeat(100))})).collect::<Vec<_>>();
  let mut state=ContextState::default();state.sync(&history).unwrap();let expected=json_size(&history).unwrap();assert_eq!(state.bytes(),expected);
  let start=Instant::now();let mut baseline=0;for _ in 0..500 {baseline+=std::hint::black_box(serde_json::to_vec(&history).unwrap()).len();}let full=start.elapsed();
  let start=Instant::now();let mut cached=0;for _ in 0..500 {state.sync(std::hint::black_box(&history)).unwrap();cached+=std::hint::black_box(state.bytes());}let incremental=start.elapsed();
  assert_eq!(baseline,cached);assert_eq!(state.measured_items,512);
  println!("accounting measurement: items=512 repetitions=500 bytes_per_context={expected} full={full:?} incremental={incremental:?} baseline_temporary_json_bytes={baseline}; incremental unchanged-history measurements=0");
 }
}
