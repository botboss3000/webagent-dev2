//! Disposable version-checked LRU cache. Runtime documents remain authoritative.
use serde_json::Value;
use std::collections::HashMap;
pub struct Cache {
    entries: HashMap<String, (Value, usize, u64)>,
    budget: usize,
    weight: usize,
    clock: u64,
    pub epoch: u64,
}
impl Cache {
    pub fn new(budget: usize) -> Self {
        Self {
            entries: HashMap::new(),
            budget,
            weight: 0,
            clock: 0,
            epoch: 0,
        }
    }
    pub fn get(&mut self, key: &str) -> Option<Value> {
        self.clock += 1;
        self.entries.get_mut(key).map(|(value, _, stamp)| {
            *stamp = self.clock;
            value.clone()
        })
    }
    pub fn insert(&mut self, key: String, value: Value, epoch: u64) {
        if epoch != self.epoch {
            return;
        }
        // Conservative accounting includes JSON tree overhead, not just string bytes.
        let weight = serde_json::to_vec(&value)
            .map(|v| v.len().saturating_mul(4) + 256)
            .unwrap_or(self.budget + 1);
        if weight > self.budget {
            return;
        }
        if let Some((_, old, _)) = self.entries.remove(&key) {
            self.weight -= old;
        }
        while self.weight + weight > self.budget {
            let victim = self
                .entries
                .iter()
                .min_by_key(|(_, (_, _, stamp))| *stamp)
                .map(|(key, _)| key.clone());
            if let Some(victim) = victim {
                if let Some((_, old, _)) = self.entries.remove(&victim) {
                    self.weight -= old;
                }
            } else {
                break;
            }
        }
        self.clock += 1;
        self.weight += weight;
        self.entries.insert(key, (value, weight, self.clock));
    }
    pub fn invalidate(&mut self, keys: &[String]) {
        self.epoch += 1;
        for key in keys {
            if let Some((_, old, _)) = self.entries.remove(key) {
                self.weight -= old;
            }
        }
    }
}

/// Disposable, bounded assembled context. SQLite checkpoints and interactions
/// remain authoritative; this only avoids decrypting old interaction rows.
pub struct ContextCache {
    entries: HashMap<String, ContextEntry>,
    budget: usize,
    weight: usize,
    clock: u64,
}

struct ContextEntry {
    checkpoint_through: i64,
    cursor: i64,
    input: Vec<Value>,
    weight: usize,
    stamp: u64,
}

impl ContextCache {
    pub fn new(budget: usize) -> Self {
        Self {
            entries: HashMap::new(),
            budget,
            weight: 0,
            clock: 0,
        }
    }

    pub fn get(&mut self, sid: &str, checkpoint_through: i64) -> Option<(Vec<Value>, i64)> {
        if self.entries.get(sid)?.checkpoint_through != checkpoint_through {
            self.invalidate(sid);
            return None;
        }
        self.clock = self.clock.wrapping_add(1);
        let entry = self.entries.get_mut(sid)?;
        entry.stamp = self.clock;
        Some((entry.input.clone(), entry.cursor))
    }

    pub fn insert(&mut self, sid: String, checkpoint_through: i64, cursor: i64, input: Vec<Value>) {
        if self.entries.get(&sid).is_some_and(|entry| {
            entry.checkpoint_through == checkpoint_through && entry.cursor > cursor
        }) {
            return;
        }
        let weight = serde_json::to_vec(&input)
            .map(|bytes| bytes.len().saturating_mul(4).saturating_add(256))
            .unwrap_or(usize::MAX);
        self.invalidate(&sid);
        if weight > self.budget {
            return;
        }
        while self.weight.saturating_add(weight) > self.budget {
            let victim = self
                .entries
                .iter()
                .min_by_key(|(_, entry)| entry.stamp)
                .map(|(sid, _)| sid.clone());
            if let Some(victim) = victim {
                self.invalidate(&victim);
            } else {
                return;
            }
        }
        self.clock = self.clock.wrapping_add(1);
        self.weight += weight;
        self.entries.insert(
            sid,
            ContextEntry {
                checkpoint_through,
                cursor,
                input,
                weight,
                stamp: self.clock,
            },
        );
    }

    pub fn invalidate(&mut self, sid: &str) {
        if let Some(entry) = self.entries.remove(sid) {
            self.weight -= entry.weight;
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn least_recently_used_entries_are_evicted_within_budget() {
        let mut cache = Cache::new(1000);
        cache.insert("a".into(), json!("a".repeat(50)), 0);
        cache.insert("b".into(), json!("b".repeat(50)), 0);
        assert!(cache.get("a").is_some());
        cache.insert("c".into(), json!("c".repeat(50)), 0);
        assert!(cache.get("b").is_none());
        assert!(cache.get("a").is_some());
        assert!(cache.weight <= cache.budget);
    }
    #[test]
    fn invalidated_inflight_reads_cannot_repopulate_old_versions() {
        let mut cache = Cache::new(1024);
        let read_epoch = cache.epoch;
        cache.invalidate(&["a".into()]);
        cache.insert("a".into(), json!("old"), read_epoch);
        assert!(cache.get("a").is_none());
        cache.insert("a".into(), json!("new"), cache.epoch);
        assert_eq!(cache.get("a"), Some(json!("new")));
    }

    #[test]
    fn context_cache_appends_without_regressing_cursor_and_evicts() {
        let mut cache = ContextCache::new(1200);
        cache.insert("a".into(), 0, 1, vec![json!("a".repeat(20))]);
        cache.insert("a".into(), 0, 2, vec![json!("a".repeat(20)), json!("b")]);
        cache.insert("a".into(), 0, 1, vec![json!("stale")]);
        assert_eq!(cache.get("a", 0).unwrap().1, 2);
        cache.insert("b".into(), 0, 1, vec![json!("b".repeat(200))]);
        assert!(cache.weight <= cache.budget);
        assert!(cache.get("a", 1).is_none());
    }

    #[test]
    fn oversized_context_is_not_cached() {
        let mut cache = ContextCache::new(128);
        cache.insert("a".into(), 0, 1, vec![json!("x".repeat(100))]);
        assert!(cache.get("a", 0).is_none());
    }
}
