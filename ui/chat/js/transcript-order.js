'use strict';

/** True only for a real durable interaction sequence (NULL/blank is legacy). */
export function hasDurableSessionSeq(row) {
  if (!row) return false;
  const value = row.session_seq;
  return value !== null && value !== undefined && value !== ''
    && Number.isFinite(Number(value));
}

function _timeKey(value) {
  const raw = String(value || '');
  if (!raw) return { raw, parsed: NaN };

  // epoch-ms / epoch-s numeric timestamp from JS conversion paths
  if (/^\d+$/.test(raw)) {
    const parsed = Number(raw);
    return { raw, parsed: Number.isFinite(parsed) ? parsed : NaN };
  }

  // SQLite's datetime('now') format has neither a T nor a zone. Browsers may
  // interpret it as local time while ISO websocket timestamps are UTC, moving
  // the same interaction by the user's timezone offset. Normalize DB time to Z.
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(raw)
    ? raw.replace(' ', 'T') + 'Z'
    : raw;

  const parsed = Date.parse(normalized);
  return { raw, parsed: Number.isFinite(parsed) ? parsed : NaN };
}

function _compareCreatedAt(a, b) {
  const at = _timeKey(a && a.created_at);
  const bt = _timeKey(b && b.created_at);
  if (at.parsed !== null && bt.parsed !== null && at.parsed !== bt.parsed) {
    return at.parsed - bt.parsed;
  }
  if (at.parsed !== null && bt.parsed === null) return -1;
  if (at.parsed === null && bt.parsed !== null) return 1;
  if (at.raw !== bt.raw) return at.raw < bt.raw ? -1 : 1;
  return 0;
}

/**
 * Return transcript rows in canonical display order.
 *
 * Durable rows form an immutable session_seq ledger. Rows that have not been
 * stamped yet are inserted into that ledger by their authoritative created_at;
 * they may not change durable-to-durable order. This is the same rule used by
 * the live DOM reconciler and prevents a late NULL sequence from becoming row 0.
 */
export function sortTranscriptCanonical(rows, options = {}) {
  const decorated = (Array.isArray(rows) ? rows : []).map((row, index) => ({
    row,
    index,
  }));
  const stableTime = (a, b) => {
    const byTime = _compareCreatedAt(a.row, b.row);
    if (byTime) return byTime;
    const aid = String((a.row && a.row.id) || '');
    const bid = String((b.row && b.row.id) || '');
    if (aid !== bid) return aid < bid ? -1 : 1;
    return a.index - b.index;
  };

  const timestampRecovery = options.timestampOrderRecovery
    ? true
    : decorated.some(item => {
      if (item.row && item.row.timestamp_order_recovery) return true;
      try {
        const meta = typeof item.row?.metadata === 'string' ? JSON.parse(item.row.metadata || '{}') : item.row?.metadata;
        return !!meta?.timestamp_order_anomaly;
      } catch (_) { return false; }
    });

  if (timestampRecovery) return decorated.sort(stableTime).map(item => item.row);

  const sequenced = decorated.filter(item => hasDurableSessionSeq(item.row))
    .sort((a, b) => {
      const bySeq = Number(a.row.session_seq) - Number(b.row.session_seq);
      return bySeq || stableTime(a, b);
    });
  const provisional = decorated.filter(item => !hasDurableSessionSeq(item.row))
    .sort(stableTime);

  for (const item of provisional) {
    const at = sequenced.findIndex(saved => _compareCreatedAt(saved.row, item.row) > 0);
    sequenced.splice(at === -1 ? sequenced.length : at, 0, item);
  }
  return sequenced.map(item => item.row);
}
