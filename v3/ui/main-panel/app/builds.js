'use strict';

const phaseLabel = value => String(value || 'waiting').replaceAll('_', ' ');
const when = value => Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : '—';
const duration = (start, end) => Number.isFinite(start)
  ? (Math.max(0, (Number.isFinite(end) ? end : Date.now() / 1000) - start) / 60).toFixed(1) + ' min'
  : '—';
const deploymentPhases = [
  'capturing_rollback', 'stopping_loop', 'stopping_api',
  'renaming_api', 'creating_api', 'starting_api',
  'renaming_loop', 'creating_loop', 'starting_loop',
  'checking_api', 'checking_loop'
];
const phasesFor = job => [
  ...(job.operation === 'build' || job.operation === 'build_deploy'
    ? ['building_api', 'building_loop'] : []),
  ...(job.operation === 'deploy' || job.operation === 'build_deploy'
    ? deploymentPhases : []),
  ...(job.isolated ? ['promoting_checkout'] : []),
  'finished'
];

export function mountBuilds(panel) {
  let stopped = false;
  let loading = false;
  let snapshot = {jobs: [], containers: {}};
  panel.replaceChildren();
  const heading = document.createElement('h2');
  heading.textContent = 'Builds';
  const status = document.createElement('p');
  status.setAttribute('role', 'status');
  status.textContent = 'Loading build status…';
  const update = document.createElement('button');
  update.type = 'button';
  update.className = 'build-update';
  update.textContent = 'Update status';
  update.setAttribute('aria-label', 'Update build status');
  const layout = document.createElement('div');
  layout.className = 'build-layout';
  const list = document.createElement('div');
  list.className = 'build-list';
  list.setAttribute('aria-label', 'Recent development builds');
  const detail = document.createElement('div');
  detail.className = 'build-detail';
  layout.append(list, detail);
  panel.append(heading, update, status, layout);

  const fact = (facts, label, value) => {
    const term = document.createElement('dt');
    term.textContent = label;
    const description = document.createElement('dd');
    description.textContent = String(value ?? '—');
    facts.append(term, description);
  };
  function render() {
    const jobs = snapshot.jobs;
    const selectedId = new URL(location.href).searchParams.get('job_id');
    const selected = jobs.find(job => job.job_id === selectedId) || (!selectedId ? jobs[0] : null);
    list.replaceChildren();
    detail.replaceChildren();
    if (!jobs.length) {
      status.textContent = 'No development builds recorded.';
      return;
    }
    status.textContent = snapshot.busy ? 'A development build is active.' : 'No build is active.';
    for (const job of jobs) {
      const button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('aria-current', String(job.job_id === selected?.job_id));
      button.textContent = [job.summary || job.task_id || job.job_id, job.status, phaseLabel(job.phase)].join(' · ');
      button.onclick = () => {
        const url = new URL(location.href);
        url.searchParams.set('job_id', job.job_id);
        history.pushState({}, '', url);
        render();
      };
      list.append(button);
    }
    if (!selected) {
      detail.textContent = 'This job is outside the recent build list.';
      return;
    }
    const title = document.createElement('h3');
    title.textContent = selected.summary || selected.task_id || selected.job_id;
    const facts = document.createElement('dl');
    facts.className = 'build-facts';
    fact(facts, 'Status', selected.status);
    fact(facts, 'Current phase', phaseLabel(selected.phase));
    fact(facts, 'Rust tests', selected.rust_tests?.status || 'not started');
    fact(facts, 'Test result', selected.rust_tests?.summary || 'Awaiting a confirmed test result');
    fact(facts, 'Started', when(selected.started_at || selected.created_at));
    fact(facts, 'Elapsed', duration(selected.created_at, selected.finished_at));
    fact(facts, 'Job ID', selected.job_id);
    fact(facts, 'Task ID', selected.task_id || '—');
    fact(facts, 'Source', selected.origin || '—');
    fact(facts, 'Input digest', selected.inputs_sha256 || '—');
    for (const role of ['api', 'loop']) {
      const container = snapshot.containers?.[role] || {};
      fact(facts, role.toUpperCase() + ' container', [container.state, container.health].filter(Boolean).join(' / ') || 'Unavailable');
    }
    detail.append(title, facts);
    if (selected.reason) {
      const reason = document.createElement('p');
      reason.textContent = selected.reason;
      detail.append(reason);
    }
    // The broker owns the current phase and timestamps; the UI supplies only
    // the operation's known sequence so upcoming steps are visible as well.
    const phaseTitle = document.createElement('h4');
    phaseTitle.textContent = 'Build phases';
    const phases = document.createElement('ol');
    phases.className = 'build-phases';
    phases.setAttribute('aria-label', 'Build phases and current step');
    const sequence = phasesFor(selected);
    const currentIndex = sequence.indexOf(selected.phase);
    const history = new Map((selected.phase_history || []).map(item => [item.phase, item.at]));
    for (const [index, phase] of sequence.entries()) {
      const row = document.createElement('li');
      const completed = history.has(phase) && (index < currentIndex || selected.status === 'completed');
      const stoppedHere = phase === selected.phase && ['failed', 'cancelled', 'stale', 'uncertain', 'interrupted'].includes(selected.status);
      const current = phase === selected.phase && !stoppedHere && selected.status !== 'completed';
      row.className = stoppedHere ? 'stopped' : current ? 'current' : completed ? 'completed' : 'upcoming';
      if (current || stoppedHere) row.setAttribute('aria-current', 'step');
      const state = stoppedHere ? 'stopped here' : current ? 'current step' : completed ? 'complete' : 'upcoming';
      row.textContent = phaseLabel(phase) + ' · ' + state +
        (history.has(phase) ? ' · ' + when(history.get(phase)) : '');
      phases.append(row);
    }
    detail.append(phaseTitle, phases);
    if (selected.log) {
      const log = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = 'Sanitized build log excerpt';
      const content = document.createElement('pre');
      content.className = 'build-log';
      content.textContent = selected.log;
      log.append(summary, content);
      detail.append(log);
    }
  }
  async function load() {
    if (loading || stopped || document.hidden) return;
    loading = true;
    update.disabled = true;
    try {
      const response = await fetch('/api/v3/admin/builds', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw Error('Build status unavailable (' + response.status + ')');
      const data = await response.json();
      if (!Array.isArray(data.jobs)) throw Error('Build status response is invalid');
      if (!stopped) { snapshot = data; render(); }
    } catch (error) {
      if (!stopped) status.textContent = error.message;
    } finally {
      loading = false;
      update.disabled = false;
    }
  }
  update.addEventListener('click', () => { void load(); });
  void load();
  const timer = setInterval(load, 5000);
  return () => { stopped = true; clearInterval(timer); };
}
