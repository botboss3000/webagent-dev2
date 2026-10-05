//! Disposable accounting owned by one executing task. Durable run input is authoritative.
//! Call reset after replacing history (compaction); otherwise input is append-only.
use anyhow::Result;
use serde::Serialize;
use serde_json::Value;
#[derive(Debug, Clone, Serialize)]
pub struct Section { pub bytes: usize, pub items: usize }

#[derive(Debug, Clone, Serialize)]
pub struct HistoryReport {
    pub messages: Section,
    pub tool_calls: Section,
    pub tool_results: Section,
    pub reasoning: Section,
    pub other: Section,
    /// Subfield sizes overlap their parent sections; do not add them to totals.
    pub argument_content_bytes: usize,
    pub result_content_bytes: usize,
}
fn size<T: Serialize + ?Sized>(value: &T) -> Result<usize> {
    json_size(value)
}
fn empty_section() -> Section { Section { bytes: 0, items: 0 } }
/// Classify and measure history items without retaining their private content.
/// Argument/result subfield counts overlap their parent sections.
pub(crate) fn history_report(history: &[Value]) -> Result<HistoryReport> {
    let mut report = HistoryReport {
        messages: empty_section(), tool_calls: empty_section(),
        tool_results: empty_section(), reasoning: empty_section(), other: empty_section(),
        argument_content_bytes: 0, result_content_bytes: 0,
    };
    for item in history {
        let section = match item["type"].as_str() {
            Some("function_call") => {
                report.argument_content_bytes += item["arguments"].as_str().map_or(0, str::len);
                &mut report.tool_calls
            },
            Some("function_call_output") => {
                report.result_content_bytes += item["output"].as_str().map_or(0, str::len);
                &mut report.tool_results
            },
            Some("reasoning") => &mut report.reasoning,
            Some("message") => &mut report.messages,
            _ if item["role"].is_string() => &mut report.messages,
            _ => &mut report.other,
        };
        section.items += 1;
        section.bytes += size(item)?;
    }
    Ok(report)
}

struct Counter(usize);
impl std::io::Write for Counter { fn write(&mut self,b:&[u8])->std::io::Result<usize>{self.0+=b.len();Ok(b.len())} fn flush(&mut self)->std::io::Result<()>{Ok(())} }
/// Count serialized JSON bytes without allocating a temporary output buffer.
pub fn json_size<T:Serialize+?Sized>(v:&T)->Result<usize>{let mut n=Counter(0);serde_json::to_writer(&mut n,v)?;Ok(n.0)}
/// Per-executing-task measurement cache, not conversation storage or policy.
/// Prefix items are assumed immutable until the caller explicitly resets it.
#[derive(Default)]
pub struct ContextAccounting { measured:usize, sum:usize, sizes:Vec<usize>, characters:Vec<usize>, report:Option<HistoryReport>, pub measured_items:usize }
impl ContextAccounting {
 /// Forget cached measurements after history replacement; append-only updates need no reset.
 pub fn reset(&mut self){self.measured=0;self.sum=0;self.sizes.clear();self.characters.clear();self.report=None;}
 /// Measure only the unmeasured suffix. Same-length replacements require an explicit reset.
 pub fn sync(&mut self,history:&[Value])->Result<()> {
  if history.len()<self.measured {self.reset();}
  if self.report.is_none(){self.report=Some(history_report(&[])?);}
  for item in &history[self.measured..] {let delta=history_report(std::slice::from_ref(item))?;self.sizes.push(delta.messages.bytes+delta.tool_calls.bytes+delta.tool_results.bytes+delta.reasoning.bytes+delta.other.bytes);self.characters.push(serde_json::to_string(item)?.chars().count());self.sum+=delta.messages.bytes+delta.tool_calls.bytes+delta.tool_results.bytes+delta.reasoning.bytes+delta.other.bytes;let r=self.report.as_mut().unwrap();
   for (a,b) in [(&mut r.messages,&delta.messages),(&mut r.tool_calls,&delta.tool_calls),(&mut r.tool_results,&delta.tool_results),(&mut r.reasoning,&delta.reasoning),(&mut r.other,&delta.other)] {a.bytes+=b.bytes;a.items+=b.items;}
   r.argument_content_bytes+=delta.argument_content_bytes;r.result_content_bytes+=delta.result_content_bytes;self.measured_items+=1;
  }
  self.measured=history.len();Ok(())
 }
 /// Cached per-item measurements are valid until history replacement; callers must reset then.
 pub fn measurements(&self)->(&[usize], &[usize]){(&self.sizes,&self.characters)}
 /// Include JSON array brackets and separators in the cached history total.
 pub fn bytes(&self)->usize{2+self.sum+self.measured.saturating_sub(1)}
 /// Return content-free classification totals after sync has initialized the cache.
 pub fn report(&self)->HistoryReport{self.report.as_ref().unwrap().clone()}
}
#[cfg(test)] mod tests {use super::*;use serde_json::json;
 #[test] fn append_only_measures_delta_and_reset_handles_replacement(){let mut s=ContextAccounting::default();let mut h=vec![json!({"role":"user","content":"\" unicode é"})];s.sync(&h).unwrap();assert_eq!(s.bytes(),serde_json::to_vec(&h).unwrap().len());s.sync(&h).unwrap();assert_eq!(s.measured_items,1);h.push(json!({"type":"function_call","arguments":"{}"}));s.sync(&h).unwrap();assert_eq!(s.measured_items,2);assert_eq!(s.bytes(),json_size(&h).unwrap());h[0]=json!({"role":"user","content":"summary"});s.reset();s.sync(&h).unwrap();assert_eq!(s.bytes(),json_size(&h).unwrap());}
}

#[cfg(test)] mod measurements {
 use super::*;use serde_json::json;use std::time::Instant;
 #[test]
 fn repeated_accounting_measurement() {
  let history=(0..512).map(|i|json!({"role":"user","content":format!("{}-{i}","x".repeat(100))})).collect::<Vec<_>>();
  let mut state=ContextAccounting::default();state.sync(&history).unwrap();let expected=json_size(&history).unwrap();assert_eq!(state.bytes(),expected);
  let start=Instant::now();let mut baseline=0;for _ in 0..500 {baseline+=std::hint::black_box(serde_json::to_vec(&history).unwrap()).len();}let full=start.elapsed();
  let start=Instant::now();let mut cached=0;for _ in 0..500 {state.sync(std::hint::black_box(&history)).unwrap();cached+=std::hint::black_box(state.bytes());}let incremental=start.elapsed();
  assert_eq!(baseline,cached);assert_eq!(state.measured_items,512);
  println!("accounting measurement: items=512 repetitions=500 bytes_per_context={expected} full={full:?} incremental={incremental:?} baseline_temporary_json_bytes={baseline}; incremental unchanged-history measurements=0");
 }
}
