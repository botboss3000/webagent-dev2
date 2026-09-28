'use strict';

/* system_resources — host resource overview. */

import { gauge, list, cpuTone, fmtDur, fmtInt, fmtMs, fmtNum, latTone, _esc } from '../_lib/card-lib.js';

function pctTone(value) {
  const v = Number(value) || 0;
  return v > 90 ? 'danger' : (v > 75 ? 'warning' : 'success');
}

function fmtBytes(value) {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  let n = Number(value);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  while (Math.abs(n) >= 1024 && unit < units.length - 1) {
    n /= 1024;
    unit += 1;
  }
  return (unit === 0 ? Math.round(n) : n.toFixed(n >= 10 ? 1 : 2)) + ' ' + units[unit];
}

function fmtRate(value) {
  return value == null ? 'collecting…' : fmtBytes(value) + '/s';
}

function pressureTone(value, warning, danger) {
  const n = Number(value) || 0;
  return n >= danger ? 'danger' : (n >= warning ? 'warning' : 'success');
}

function capacityDetails(capacity) {
  if (capacity.estimated_users == null) return null;
  const lines = [`Active workload sample: ${fmtInt(capacity.sample_users)} user${capacity.sample_users === 1 ? '' : 's'}.`, '', 'Projected limits:'];
  for (const projection of (capacity.projections || [])) {
    if (projection.unit === 'users') {
      lines.push(`${projection.metric}: ${fmtInt(projection.estimated_users)} users (configured hard cap).`);
      continue;
    }
    const unit = projection.unit === 'milliseconds' ? 'ms' : '%';
    const current = Number(projection.current).toFixed(1);
    const baseline = Number(projection.baseline).toFixed(1);
    const incremental = Number(projection.incremental).toFixed(1);
    const limit = Number(projection.limit).toFixed(0);
    lines.push(`${projection.metric}: ${fmtInt(projection.estimated_users)} users — ${current}${unit} current − ${baseline}${unit} idle = ${incremental}${unit} incremental, against ${limit}${unit}.`);
  }
  lines.push('', `Weakest link: ${capacity.limiting_metric || 'unknown'}.`);
  return { title: 'Estimated Cap calculation', body: lines.join('\n') };
}

export default {
  render(snapshot) {
    const resources = snapshot.system_resources || {};
    const cpu = resources.cpu || {};
    const memory = resources.memory || {};
    const swap = resources.swap || {};
    const disk = resources.disk || {};
    const network = resources.network || {};
    const workload = resources.workload || {};
    const database = workload.database || {};
    const llm = workload.llm || {};
    const connections = workload.connections || {};
    const scheduler = workload.scheduler || {};
    const descriptors = workload.descriptors || {};
    const idleBaseline = workload.idle_baseline || {};
    const baselineSignals = idleBaseline.signals || {};
    const capacity = workload.capacity || {};
    const sessionCap = Number(workload.session_cap) || 0;
    const queueLabel = sessionCap
      ? `${fmtInt(workload.holders)} / ${fmtInt(sessionCap)} running · ${fmtInt(workload.queued)} queued`
      : `${fmtInt(workload.active_chat_runs)} running · ${fmtInt(workload.queued)} queued`;
    const descriptorLabel = descriptors.open == null ? 'Unavailable'
      : `${fmtInt(descriptors.open)}${descriptors.limit ? ` / ${fmtInt(descriptors.limit)}` : ''}`;
    const baselineParts = [];
    if (baselineSignals.cpu_percent != null) baselineParts.push(`CPU ${Number(baselineSignals.cpu_percent).toFixed(1)}%`);
    if (baselineSignals.memory_percent != null) baselineParts.push(`RAM ${Number(baselineSignals.memory_percent).toFixed(1)}%`);
    if (baselineSignals.db_p95_ms != null) baselineParts.push(`DB ${fmtMs(baselineSignals.db_p95_ms)}`);
    if (baselineSignals.descriptor_percent != null) baselineParts.push(`FD ${Number(baselineSignals.descriptor_percent).toFixed(1)}%`);
    const baselineLabel = baselineParts.length ? baselineParts.join(' · ') : 'Awaiting idle sample';
    const estimateLabel = capacity.estimated_users == null
      ? (capacity.awaiting_baseline ? 'Awaiting idle baseline' : 'Need active workload sample')
      : `~${fmtInt(capacity.estimated_users)} users · ${_esc(capacity.limiting_metric || 'unknown limit')}`;
    const load = cpu.load || {};
    const cpuValue = cpu.percent == null ? 0 : cpu.percent;
    const memoryValue = memory.percent == null ? 0 : memory.percent;
    const diskValue = disk.percent == null ? 0 : disk.percent;
    const networkLabel = network.scope === 'webagent-container' ? 'WebAgent network' : 'Host network';

    const gauges = '<div class="dash-system-gauges">'
      + gauge(cpuValue, cpu.percent == null ? 'CPU · collecting…' : 'CPU · host total', cpuTone(cpuValue), 'Percentage of host CPU capacity currently in use.')
      + gauge(memoryValue, 'Memory · physical', pctTone(memoryValue), 'Percentage of physical RAM currently in use.')
      + gauge(diskValue, 'Disk · checkout volume', pctTone(diskValue), 'Percentage of the filesystem holding the WebAgent checkout that is in use.')
      + '</div>';

    const rows = [
      { k: 'Load average', v: load.one == null ? '—' : `${load.one} · ${load.five} · ${load.fifteen}`, help: 'Runnable and uninterruptible tasks averaged over 1, 5, and 15 minutes. Compare with CPU core count.' },
      { k: 'CPU cores', v: cpu.logical_cores == null ? '—' : _esc(String(cpu.logical_cores)), help: 'Logical CPU cores available to this host or container.' },
      { k: 'RAM', v: `${fmtBytes(memory.used_bytes)} / ${fmtBytes(memory.total_bytes)}`, help: 'Physical memory currently used versus total available RAM.' },
      { k: _esc(swap.label || 'Swap'), v: swap.total_bytes ? `${fmtBytes(swap.used_bytes)} / ${fmtBytes(swap.total_bytes)}` : 'Disabled', help: 'Swap is disk-backed memory used when RAM is under pressure; sustained use can slow the server.' },
      { k: 'Disk free', v: `${fmtBytes(disk.free_bytes)} / ${fmtBytes(disk.total_bytes)}`, help: 'Free storage versus total storage on the filesystem containing the WebAgent checkout.' },
      { k: 'Uptime', v: resources.uptime_s == null ? '—' : fmtDur(resources.uptime_s), help: 'Time since the operating system last started.' },
      { k: 'Temperature', v: resources.temperature_c == null ? 'Unavailable' : `${Number(resources.temperature_c).toFixed(1)} °C`, help: 'Reported CPU or system temperature, when the host exposes a sensor.' },
      { k: networkLabel, v: `↓ ${fmtRate(network.received_bytes_per_s)} · ↑ ${fmtRate(network.transmitted_bytes_per_s)}`, help: 'Current receive and transmit throughput for the host or WebAgent container.' },
      { k: 'Active chat runs', v: _esc(String(workload.active_chat_runs ?? 0)), help: 'Agent chat runs currently executing across all users; idle chat sessions are not counted.' },
      { k: 'Chat concurrency', v: queueLabel, tone: workload.queued ? 'warning' : 'success', help: 'Runs admitted by the global chat concurrency gate. Queued work is waiting for a capacity slot.' },
      { k: 'Chromium sessions', v: `${_esc(String(workload.active_chromium_sessions ?? 0))} active · ${_esc(String(workload.live_chromium_sessions ?? 0))} live${workload.attached_chromium_sessions ? ` · ${_esc(String(workload.attached_chromium_sessions))} attached` : ''}`, tone: workload.active_chromium_sessions ? 'warning' : 'success', help: 'Active sessions are performing browser work. Live sessions include idle headless browsers; attached sessions are user-owned Chrome windows connected to WebAgent.' },
      { k: 'Database p95', v: `${fmtMs(database.p95_ms)} · ${fmtNum(database.rate_per_min)} calls/min`, tone: latTone(database.p95_ms), help: '95th-percentile database query latency, followed by the recent database call rate.' },
      { k: 'LLM', v: `${fmtNum(llm.rate_per_min)} calls/min · ${fmtNum(llm.tokens_per_min)} tokens/min`, tone: 'brand', help: 'Recent large-language-model request rate and total token throughput.' },
      { k: 'Event-loop yield', v: fmtMs(workload.event_loop_yield_ms), tone: pressureTone(workload.event_loop_yield_ms, 20, 100), help: 'Time for the Python async event loop to yield and resume. A high value can indicate a blocked or overloaded server.' },
      { k: 'Agent connections', v: `${fmtInt(connections.connections)} sockets · ${fmtInt(connections.users)} users`, tone: 'brand', help: 'Open agent event WebSockets and the number of distinct users currently receiving agent updates.' },
      { k: 'Background work', v: `${fmtInt(scheduler.in_flight)} automations in flight${scheduler.running === false ? ' · scheduler stopped' : ''}`, tone: scheduler.last_error ? 'danger' : 'success', help: 'Scheduled automation jobs currently running. The status also flags a stopped scheduler or its latest error.' },
      { k: 'Open descriptors', v: descriptorLabel, tone: pressureTone(descriptors.percent, 70, 90), help: 'Process file descriptors currently open, compared with the operating system soft limit when available. This includes files and sockets.' },
      { k: 'Idle workload baseline', v: baselineLabel, tone: baselineParts.length ? 'success' : 'muted', help: `This server\'s minimum observed zero-workload footprint. It is captured only when no chat run is active and no user is connected (${fmtInt(idleBaseline.samples)} calibration samples). Capacity calculations subtract these floors so they use only incremental user load.` },
      { k: 'Estimated Cap', v: estimateLabel, tone: capacity.estimated_users == null ? 'muted' : 'brand', details: capacityDetails(capacity), help: 'A calibrated linear projection from the current active-workload sample. It subtracts this server\'s idle baseline, then scales the remaining per-user footprint until the tightest of CPU, memory, database p95 latency (500 ms target), open descriptors, or the configured chat cap is reached. Click the result to see the full calculation. The displayed metric is the current weakest link, not a guarantee.' },
    ];

    return `<div class="dash-system-resources">${gauges}${list(rows)}</div>`;
  },
};
