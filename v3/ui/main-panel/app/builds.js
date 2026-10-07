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
const stoppedStatuses = new Set(['failed', 'cancelled', 'stale', 'uncertain', 'interrupted']);
const stateFor = job => job.status === 'completed'
  ? {tone: 'success', icon: '⚑', label: 'Complete'}
  : stoppedStatuses.has(job.status)
    ? {tone: 'danger', icon: '⚑', label: 'Incomplete · ' + phaseLabel(job.status)}
    : {tone: 'progress', icon: '●', label: 'In progress · ' + phaseLabel(job.status)};

export function mountBuilds(panel, onStatus = () => {}) {
  let stopped = false;
  let loading = false;
  let snapshot = {jobs: [], containers: {}};
  panel.replaceChildren();
  const header = document.createElement('div');
  header.className = 'build-header';
  const intro = document.createElement('div');
  const heading = document.createElement('h2');
  heading.textContent = 'Builds';
  const subtitle = document.createElement('p');
  subtitle.textContent = 'Development builds, tests, and deployments';
  intro.append(heading, subtitle);
  const update = document.createElement('button');
  update.type = 'button';
  update.className = 'build-update';
  update.textContent = 'Update status';
  const status = document.createElement('p');
  status.className = 'build-summary';
  status.setAttribute('role', 'status');
  status.textContent = 'Loading build status…';
  const list = document.createElement('div');
  list.className = 'build-list';
  list.setAttribute('aria-label', 'Recent development builds');
  header.append(intro, update);
  panel.append(header, status, list);

  const fact = (facts, label, value) => {
    const item = document.createElement('div');
    item.className = 'build-fact';
    const term = document.createElement('dt');
    term.textContent = label;
    const description = document.createElement('dd');
    description.textContent = String(value ?? '—');
    item.append(term, description);
    facts.append(item);
  };
  function render() {
    const jobs = snapshot.jobs;
    const linkedId = new URL(location.href).searchParams.get('job_id');
    list.replaceChildren();
    if (!jobs.length) {
      status.textContent = 'No development builds recorded.';
      return;
    }
    status.textContent = snapshot.busy ? 'A development build is active.' : jobs.length + ' recent builds';
    if (linkedId && !jobs.some(job => job.job_id === linkedId)) {
      const notice = document.createElement('p');
      notice.className = 'build-missing';
      notice.textContent = 'The linked job is outside the recent build list.';
      list.append(notice);
    }
    for (const job of jobs) {
      const card = document.createElement('article');
      card.className = 'build-card';
      card.dataset.status = stateFor(job).tone;
      if (job.job_id === linkedId) card.classList.add('linked');
      const top = document.createElement('div');
      top.className = 'build-card-top';
      const title = document.createElement('h3');
      title.textContent = job.summary || job.task_id || job.job_id;
      const flag = document.createElement('span');
      flag.className = 'build-flag';
      const marker = document.createElement('span');
      marker.setAttribute('aria-hidden', 'true');
      marker.textContent = stateFor(job).icon;
      const label = document.createElement('span');
      label.textContent = stateFor(job).label;
      flag.append(marker, label);
      top.append(title, flag);
      card.append(top);
      const facts = document.createElement('dl');
      facts.className = 'build-facts';
      if (job.phase !== 'finished') fact(facts, 'Current phase', phaseLabel(job.phase));
      fact(facts, 'Rust tests', job.rust_tests?.status || 'not started');
      fact(facts, 'Test result', job.rust_tests?.summary || 'Awaiting a confirmed test result');
      fact(facts, 'Started', when(job.started_at || job.created_at));
      fact(facts, 'Elapsed', duration(job.created_at, job.finished_at));
      fact(facts, 'Job ID', job.job_id);
      fact(facts, 'Task ID', job.task_id || '—');
      fact(facts, 'Source', job.origin || '—');
      fact(facts, 'Input digest', job.inputs_sha256 || '—');
      for (const role of ['api', 'loop']) {
        const container = snapshot.containers?.[role] || {};
        fact(facts, 'Current ' + role.toUpperCase() + ' container',
          [container.state, container.health].filter(Boolean).join(' / ') || 'Unavailable');
      }
      card.append(facts);
      if (job.reason) {
        const reason = document.createElement('p');
        reason.className = 'build-reason';
        reason.textContent = job.reason;
        card.append(reason);
      }
      // Broker receipts own phase history. The known sequence only marks steps not yet reached.
      const phaseTitle = document.createElement('h4');
      phaseTitle.textContent = 'Build phases';
      const phases = document.createElement('ol');
      phases.className = 'build-phases';
      phases.setAttribute('aria-label', 'Build phases and current step');
      const sequence = phasesFor(job);
      const currentIndex = sequence.indexOf(job.phase);
      const history = new Map((job.phase_history || []).map(item => [item.phase, item.at]));
      for (const [index, phase] of sequence.entries()) {
        const row = document.createElement('li');
        const completed = history.has(phase) && (index < currentIndex || job.status === 'completed');
        const stoppedHere = phase === job.phase && stoppedStatuses.has(job.status);
        const current = phase === job.phase && !stoppedHere && job.status !== 'completed';
        row.className = stoppedHere ? 'stopped' : current ? 'current' : completed ? 'completed' : 'upcoming';
        if (current || stoppedHere) row.setAttribute('aria-current', 'step');
        const state = stoppedHere ? 'stopped here' : current ? 'current step' : completed ? 'complete' : 'upcoming';
        row.textContent = phaseLabel(phase) + ' · ' + state +
          (history.has(phase) ? ' · ' + when(history.get(phase)) : '');
        phases.append(row);
      }
      card.append(phaseTitle, phases);
      if (job.log) {
        const log = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = 'Sanitized build log excerpt';
        const content = document.createElement('pre');
        content.className = 'build-log';
        content.textContent = job.log;
        log.append(summary, content);
        card.append(log);
      }
      list.append(card);
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
      if (!stopped) { snapshot = data; onStatus(Boolean(data.busy)); render(); }
    } catch (error) {
      if (!stopped) { onStatus(false); status.textContent = error.message; }
    } finally {
      loading = false;
      update.disabled = false;
    }
  }
  update.addEventListener('click', () => { void load(); });
  void load();
  const timer = setInterval(load, 5000);
  return () => { stopped = true; clearInterval(timer); onStatus(false); };
}
