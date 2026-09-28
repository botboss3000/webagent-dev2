'use strict';

// Render Recorder header control.
//
// Installing this standalone plugin controls availability. Live
// capture state belongs to this document: no setting, poll, BroadcastChannel, or
// storage event can turn another tab on. A short-lived server lease authorizes
// uploads from this document's client_id + recording_id pair.

import { apiPath } from '/ui/shared/js/config.js';
import { authHeaders } from '/ui/shared/js/left-login.js';
import { randomUUID } from '/ui/shared/js/uuid.js';

import { initRecorder, detachRecorder, armRecorder, disarmRecorder, prepareRecorderCapture, cancelPreparedCapture, recorderElapsedMs, recorderEventSeq } from './recorder.js';
import { browserPersistenceAllowed, getBrowserStorageContext } from '/ui/shared/js/browser-storage-policy.js';
import { createUploader } from './uploader.js';
import { app } from '/ui/shared/js/state.js';
initRecorder();
const _style=document.createElement('link');_style.rel='stylesheet';
_style.href=new URL('./controls.css',import.meta.url).href;document.head.append(_style);

const STATUS_URL = '/api/v1/recordings/enabled';
const INTENT_KEY = 'webagent.recorder.reloadIntent';
const FINISH_INTENT_KEY='webagent.recorder.pending-finalization-v1';
const STATUS_POLL_MS = 5000;
const _clientId = randomUUID(); // deliberately new for every loaded document

let _phase='idle';
let _popoverTimer=null;
let _configOpen=false;
let _prebufferEnabled=false;
let _preferenceOwner='';
let _armed=false;
let _arming=false;
let _armBarrier=Promise.resolve();
let _armExpiresAt=0;
let _armRenewAt=0;
let _armError='';
let _adminKnown=false;
let _triggerCoverage=null;
const _finishing=new Map();
let _lastPopoverPhase='idle';
let _gaps=new Set();
let _screenStream=null;
let _screenVideo=null;
let _scopeChain=Promise.resolve();
let _scopeHook=null;
let _button = null;
let _control = null;
let _available = false;
let _enabled = false;
let _busy = false;
let _renewing = false;
let _isAdminUser = false;
let _autoStopTimer = null;
let _countdownTimer = null;
let _endsAt = 0;
let _config = {};
let _recordingId = null;
let _leaseToken = '';
let _leaseExpiresAt = 0;
let _leaseRenewAt = 0;
let _reloadIntent = null;

function _isAdmin() {
  return _adminKnown ? _isAdminUser : document.body.classList.contains('is-admin');
}

function _durationMs(config) {
  const configured = Number(config && config.capture_duration_ms);
  return Number.isFinite(configured) ? Math.max(0, configured) : 0;
}

function _render() {
  if (!_button || !_control) return;
  const visible = _isAdmin() && _available;
  _control.hidden = !visible;
  _button.disabled = _busy;
  _button.classList.toggle('active', _enabled);
  _button.classList.toggle('is-busy', _busy);
  _control.classList.toggle('is-recording', _enabled);
  document.body.classList.toggle('render-recorder-active', visible && _enabled);
  _button.setAttribute('aria-pressed', String(_enabled));
  _button.setAttribute('aria-busy', String(_busy));
  const durationMs = _durationMs(_config);
  const seconds = Math.round(durationMs / 1000);
  let title = durationMs > 0
    ? `Start recording this tab (automatic stop after ${seconds}s)`
    : 'Start recording this tab (no automatic time limit)';
  if (_busy) title = _enabled ? 'Stopping recording in this tab…' : 'Starting recording in this tab…';
  else if (_enabled) title = 'This tab is recording — click to stop';
  _button.title = title;
  _button.setAttribute('aria-label', title);
  const live = _button.querySelector('.render-recorder-live');
  if (live) live.textContent = title;
  _control.dataset.captureState=_enabled?_phase:(_armed?'buffering':_phase);
  const preference=_control.querySelector('[data-recorder-prebuffer]');
  if(preference) {preference.checked=_prebufferEnabled;preference.disabled=_busy;}
  for (const button of _control.querySelectorAll('[data-recorder-action]')) button.hidden=!_enabled || _busy || _phase==='upload_pending';
  _renderPopover();
  if (_phase!==_lastPopoverPhase) {
    _lastPopoverPhase=_phase;
    _showPopover();
  }
}

// RECORDER-POPOVER: a separate label button owns visibility; only the switch
// changes capture state. Polling the status must not keep reopening the panel.
function _closePopover() {
  _configOpen=false;
  if (_popoverTimer) clearTimeout(_popoverTimer);
  _popoverTimer=null;
  _control?.classList.remove('is-panel-open');
  _control?.querySelector('.render-recorder-label')?.setAttribute('aria-expanded','false');
  _control?.querySelector('.render-recorder-popover')?.setAttribute('aria-hidden','true');
}
function _showPopover(config=false) {
  if (!_control) return;
  if (config===true) _configOpen=true;
  if (_popoverTimer) clearTimeout(_popoverTimer);
  _control.classList.add('is-panel-open');
  _control.querySelector('.render-recorder-label')?.setAttribute('aria-expanded','true');
  _control.querySelector('.render-recorder-popover')?.setAttribute('aria-hidden','false');
  if (!_configOpen) _popoverTimer=setTimeout(_closePopover,2000);
}

function _renderPopover() {
  if (!_control) return;
  const status = _control.querySelector('.render-recorder-status-title');
  const detail = _control.querySelector('.render-recorder-status-detail');
  const limits = _control.querySelector('.render-recorder-status-limits');
  const countdown = _control.querySelector('.render-recorder-countdown');
  const durationMs = _durationMs(_config);
  const durationSeconds = Math.round(durationMs / 1000);
  if (status) status.textContent = !_enabled && !_busy && _armed ? 'Buffering · 5 seconds'
    : ({idle:'Off',starting:'Starting…',recording:'Recording this tab',ready:'Report ready',degraded:'Recording evidence incomplete'})[_phase] || 'Off';
  const finishing=_control.querySelector('.render-recorder-finishing');
  if(finishing) {
    finishing.hidden=!_finishing.size && _phase!=='ready';
    finishing.textContent=_finishing.size ? String(_finishing.size)+' report'+(_finishing.size===1?'':'s')+' finishing in the background' : (_phase==='ready'?'Report ready':'');
  }
  const armedStatus=_control.querySelector('.render-recorder-buffer-status');
  if(armedStatus) armedStatus.textContent=_armError || (_prebufferEnabled ? 'Keeps recent evidence in memory until you start recording.' : 'No evidence is collected before recording starts.');
  if (detail) {
    detail.textContent = _gaps.size ? 'Evidence gaps: '+[..._gaps].join(', ') : '';
    detail.style.display = _gaps.size ? '' : 'none';
  }
  if (limits) {
    const scope = _config.capture_whole_page ? 'Whole page' : 'Chat only';
    const timeLimit = durationMs > 0 ? `${durationSeconds}s automatic stop` : 'No time limit';
    const backgrounds = _config.capture_decorative_animations === true
      ? 'Backgrounds included'
      : 'Backgrounds excluded';
    limits.textContent = `${scope} · ${timeLimit} · No data limit · ${backgrounds}`;
  }
  if (countdown) {
    const remaining = _enabled && _endsAt ? Math.max(0, Math.ceil((_endsAt - Date.now()) / 1000)) : 0;
    countdown.textContent = _enabled ? (_endsAt ? `${remaining}s left` : 'No limit') : '';
  }
}

function _stopCountdown() {
  if (_countdownTimer) clearInterval(_countdownTimer);
  _countdownTimer = null;
  _endsAt = 0;
}

function _scheduleAutoStop(durationMs, restoredEndsAt = 0) {
  if (_autoStopTimer) clearTimeout(_autoStopTimer);
  _autoStopTimer = null;
  if (!(durationMs > 0)) {
    _stopCountdown();
    return;
  }
  _endsAt = restoredEndsAt > Date.now() ? restoredEndsAt : Date.now() + durationMs;
  if (!_countdownTimer) _countdownTimer = setInterval(_renderPopover, 1000);
  const arm = () => {
    const remaining = _endsAt - Date.now();
    if (remaining <= 0) {
      _autoStopTimer = null;
      _stopLocal('duration-limit');
      return;
    }
    _autoStopTimer = setTimeout(arm, Math.min(remaining, 2147483647));
  };
  arm();
}

function _broadcast() {
  try {
    const detail = {
      phase:_phase, gaps:[..._gaps], armed:_armed, finishing:_finishing.size,
      available: _available,
      enabled: _enabled,
      config: _config,
      client_id: _clientId,
      recording_id: _recordingId,
      lease_token: _leaseToken,
      lease_expires_at: _leaseExpiresAt ? new Date(_leaseExpiresAt).toISOString() : null,
    };
    window.__renderRecorderState = detail;
    window.dispatchEvent(new CustomEvent('render-recorder-changed', { detail }));
  } catch (_) {}
}

function _readReloadIntent() {
  try {
    const nav = performance.getEntriesByType && performance.getEntriesByType('navigation')[0];
    const isTrueReload = !!(nav && nav.type === 'reload');
    const raw = sessionStorage.getItem(INTENT_KEY);
    if (!isTrueReload) {
      sessionStorage.removeItem(INTENT_KEY); // also defeats duplicated-tab copies
      return null;
    }
    const parsed = raw ? JSON.parse(raw) : null;
    if (!parsed || parsed.enabled !== true || !parsed.recording_id) return null;
    if (parsed.ends_at && Number(parsed.ends_at) <= Date.now()) {
      sessionStorage.removeItem(INTENT_KEY);
      return null;
    }
    return parsed;
  } catch (_) {
    return null;
  }
}

function _writeReloadIntent() {
  try {
    if (!_enabled || !_recordingId) {
      sessionStorage.removeItem(INTENT_KEY);
      return;
    }
    sessionStorage.setItem(INTENT_KEY, JSON.stringify({
      enabled: true,
      recording_id: _recordingId,
      ends_at: _endsAt || 0,
    }));
  } catch (_) {}
}

async function _requestLease(recordingId, binding={}) {
  const response = await fetch(apiPath(STATUS_URL), {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, binding.auth_headers || authHeaders()),
    body: JSON.stringify({
      enabled: true,
      client_id: binding.client_id || _clientId,
      recording_id: recordingId || null,
      prebuffer: !recordingId && !!_triggerCoverage,
      session_id:app.currentSessionId || null, agent_id:app.currentAgentId || null,
      started_midrun:!!app.isProcessing,
      trigger_time_ms:_triggerCoverage?.trigger_at ? Date.parse(_triggerCoverage.trigger_at) : undefined,
    }),
    signal:AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    let detail = response.statusText || `HTTP ${response.status}`;
    try { const body = await response.json(); detail = body.detail || detail; } catch (_) {}
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function _acceptLease(data) {
  _available = data.available === true;
  _config = (data.config && typeof data.config === 'object') ? data.config : _config;
  _recordingId = data.recording_id || _recordingId;
  _leaseToken = data.lease_token || '';
  _leaseExpiresAt = Date.parse(data.lease_expires_at || '') || 0;
  const renewSeconds = Math.max(1, Number(data.lease_renew_after_seconds) || 480);
  for (const gap of data.capture?.gaps || []) _gaps.add(typeof gap==='string'?gap:gap.reason || 'channel-unavailable');
  _leaseRenewAt = Date.now() + (renewSeconds * 1000);
}

async function _startLocal(recordingId = null, restoredEndsAt = 0) {
  if (!_isAdmin() || _busy || !_available || _enabled) return;
  _busy = true;
  _triggerCoverage=!recordingId && _armed ? prepareRecorderCapture() : null;
  _phase='starting'; _gaps.clear();
  _render();
  try {
    const data = await _requestLease(recordingId);
    _acceptLease(data);
    if (!_available || !_leaseToken || !_recordingId) throw new Error('Recorder lease was not issued');
    await _scope(app.currentSessionId,app.currentAgentId,{},_triggerCoverage ? {prebuffer_browser:_triggerCoverage} : {});
    _enabled = true;
    window.__recorderScope=_scopeHook;
    _phase=_gaps.size?'degraded':'recording';
    _scheduleAutoStop(_durationMs(_config), restoredEndsAt);
    _writeReloadIntent();
    _broadcast();
  } catch (error) {
    cancelPreparedCapture();
    _phase='degraded'; _gaps.add('start-failed');
    console.error('DEBUG-TAG: render recorder start failed');
    _enabled = false;
    _writeReloadIntent();
  } finally {
    _busy = false;
    _triggerCoverage=null;
    _render();
  }
}

async function _renewLease() {
  if (!_enabled || _renewing || !_recordingId) return;
  _renewing = true;
  try {
    const data = await _requestLease(_recordingId);
    _acceptLease(data);
    _broadcast(); // active recorder swaps to the renewed token without restarting
  } catch (error) {
    const expired = !_leaseExpiresAt || Date.now() >= _leaseExpiresAt;
    if (expired || error.status === 403 || error.status === 409) {
      _stopLocal('lease-renewal-failed');
    }
  } finally {
    _renewing = false;
  }
}

// RECORDER-FINALIZATION-RECOVERY: only saved captures get durable control
// metadata. It contains no buffered evidence and is owner-bound; another login
// cannot resume it. This bridges service-worker reloads after capture stops.
function _finishOwner() {
  return getBrowserStorageContext()?.owner_scope || app.currentUserId || '';
}
function _readFinishIntents() {
  if(!browserPersistenceAllowed())return {};
  try{return JSON.parse(sessionStorage.getItem(FINISH_INTENT_KEY) || '{}');}catch(_){return {};}
}
function _persistFinish(item,remove=false) {
  if(!browserPersistenceAllowed())return;
  try {
    const all=_readFinishIntents();
    if(remove)delete all[item.id];
    else all[item.id]={id:item.id,owner:item.owner,last_seq:item.job.last_seq,
      client_id:item.binding.client_id,lease_token:item.binding.lease_token,
      gaps:[...item.gaps],scope:item.scopeIntent || null};
    if(Object.keys(all).length)sessionStorage.setItem(FINISH_INTENT_KEY,JSON.stringify(all));
    else sessionStorage.removeItem(FINISH_INTENT_KEY);
  }catch(_){}
}
function _recoverFinishing() {
  const owner=_finishOwner();
  if(!owner || !_available || !_isAdmin())return;
  for(const intent of Object.values(_readFinishIntents())){
    if(!intent || intent.owner!==owner || !intent.id || !intent.client_id ||
       !Number.isInteger(intent.last_seq) || _finishing.has(intent.id))continue;
    const binding={recording_id:intent.id,client_id:intent.client_id,lease_token:intent.lease_token || '',auth_headers:{...authHeaders()}};
    const uploader=createUploader({recordingId:intent.id,clientId:intent.client_id,token:binding.lease_token,
      health:(reason,resolved,recording_id)=>window.dispatchEvent(new CustomEvent('render-recorder-health',{detail:{reason,resolved,recording_id}}))});
    const job={recording_id:intent.id,last_seq:intent.last_seq,renew:token=>uploader.renew(token),
      async finish(){return {recording_id:intent.id,last_seq:intent.last_seq,acknowledged:await uploader.drain()};}};
    const item={id:intent.id,owner,binding,job,gaps:new Set(intent.gaps || []),
      scopeIntent:intent.scope,scope:Promise.resolve(),busy:false,renewAt:0,timer:null,recovered:true};
    _finishing.set(item.id,item);_finishCapture(item);
  }
}
// RECORDER-DETACHED-FINALIZATION: each stopped capture owns its proof, scope
// barrier and upload job. Neither a new recording nor renewed arming can rebind it.
async function _finishCapture(item) {
  if(item.busy) return;
  item.busy=true;
  try {
    if(item.recovered){
      const response=await fetch(apiPath('/api/v1/recordings/captures/'+encodeURIComponent(item.id)),{
        headers:item.binding.auth_headers,signal:AbortSignal.timeout(15000)});
      if(!response.ok)throw new Error('recovery-status-'+response.status);
      const saved=await response.json();
      if(['completed','incomplete'].includes(saved.capture?.state)){
        _persistFinish(item,true);_finishing.delete(item.id);return;
      }
    }
    if(Date.now()>=item.renewAt) {
      const renewed=await _requestLease(item.id,item.binding);
      item.binding.lease_token=renewed.lease_token;
      item.job.renew(renewed.lease_token);
      item.renewAt=Date.now()+Math.max(1000,(renewed.lease_renew_after_seconds || 480)*1000);
      _persistFinish(item);
    }
    if(item.recovered && item.scopeIntent){
      await _scope(item.scopeIntent.session_id,item.scopeIntent.agent_id,item.binding);
    }
    item.recovered=false;
    const result=await item.job.finish();
    if(result?.acknowledged!==true) throw new Error('upload-pending');
    await item.scope;
    const response=await _capturePost('stop',{last_seq:result.last_seq,gaps:[...item.gaps],acknowledged:true},item.binding);
    _persistFinish(item,true);_finishing.delete(item.id);
    if(!_enabled && !_busy) _phase='ready';
    window.dispatchEvent(new CustomEvent('render-recorder-report-ready',{detail:response}));
  } catch (_) {
    // Saved outbox chunks and their original lease remain available. Retry is
    // independent of the switch and never enables collection for this capture.
    _persistFinish(item);item.timer=setTimeout(()=>_finishCapture(item),5000);
  } finally {
    item.busy=false;_broadcast();_render();
  }
}
async function _stopLocal(reason = 'disabled') {
  if (_busy || !_enabled) return;
  const id=_recordingId;
  const binding={recording_id:id,lease_token:_leaseToken,client_id:_clientId,auth_headers:{...authHeaders()}};
  const job=detachRecorder(reason);
  if(!job) return;
  const item={id,owner:_finishOwner(),scopeIntent:{session_id:app.currentSessionId || null,agent_id:app.currentAgentId || null},binding,job,gaps:new Set([..._gaps].filter(reason=>!['upload-pending','finalization-failed'].includes(reason))),scope:_scopeChain,busy:false,renewAt:_leaseRenewAt,timer:null};
  _finishing.set(id,item);
  _persistFinish(item); // Synchronous, before any re-arm or asynchronous drain.
  if (_autoStopTimer) clearTimeout(_autoStopTimer);
  _autoStopTimer=null;_stopCountdown();_stopScreen();
  _enabled=false;_recordingId=null;_leaseToken='';_phase='idle';_gaps.clear();
  _writeReloadIntent();
  if(_prebufferEnabled && _isAdmin() && _available) {
    armRecorder({client_id:_clientId,config:_config});_armed=true;
  } else {delete window.__recorderScope;_armed=false;}
  _broadcast();_render();
  await _finishCapture(item);
}

async function _capturePost(action, body={}, binding={}) {
  const {recording_id=_recordingId,lease_token=_leaseToken,client_id=_clientId,auth_headers=authHeaders()}=binding;
  const response=await fetch(apiPath('/api/v1/recordings/captures/'+encodeURIComponent(recording_id)+'/'+action),{
    method:'POST',headers:{'Content-Type':'application/json',...auth_headers},
    body:JSON.stringify({lease_token,client_id,...body}),
    signal:AbortSignal.timeout(action==='scope'?2000:20000),
  });
  if (!response.ok) throw new Error('Recorder '+action+' failed: '+response.status);
  return response.json();
}
async function _scope(sessionId,agentId,binding={},metadata={}) {
  if (!(binding.recording_id || _recordingId) || !(binding.lease_token || _leaseToken)) return;
  try { await _capturePost('scope',{session_id:sessionId || null,agent_id:agentId || null,metadata},binding); }
  catch (_) {
    const id=binding.recording_id || _recordingId;
    const pending=_finishing.get(id);
    if(pending) pending.gaps.add('scope-unavailable');
    else if(id===_recordingId) _gaps.add('scope-unavailable');
    _render();
  }
}
function _stopScreen() {
  for (const track of _screenStream?.getTracks() || []) track.stop();
  _screenStream=null;
  if (_screenVideo) _screenVideo.srcObject=null;
  _screenVideo=null;
}
async function _enableScreenshots() {
  try {
    _stopScreen();
    const stream=await navigator.mediaDevices.getDisplayMedia({video:{displaySurface:'browser'},audio:false,
      preferCurrentTab:true,selfBrowserSurface:'include'});
    // Capturing a whole screen/window could include unrelated private content.
    if (stream.getVideoTracks()[0]?.getSettings().displaySurface !== 'browser') {
      stream.getTracks().forEach(track=>track.stop());
      throw new Error('Choose the app browser tab');
    }
    if (!_enabled || _busy || _phase==='upload_pending') { stream.getTracks().forEach(track=>track.stop()); return; }
    _screenStream=stream; _screenVideo=document.createElement('video');
    _screenVideo.muted=true; _screenVideo.srcObject=stream; await _screenVideo.play();
    _gaps.delete('screenshots-not-enabled');
    stream.getVideoTracks()[0].addEventListener('ended',()=>{_stopScreen();_render();});
  } catch (_) { _gaps.add('screenshots-not-enabled'); }
  _render();
}
async function _captureIssue() {
  if (!_enabled || _busy || _phase==='upload_pending') return;
  // Screenshot access starts from this user gesture; no separate enable control.
  if (!_screenVideo?.videoWidth) await _enableScreenshots();
  if (!_enabled || _busy || _phase==='upload_pending') return;
  const binding={recording_id:_recordingId,lease_token:_leaseToken,client_id:_clientId};
  const payload={marker_id:randomUUID(),elapsed_ms:recorderElapsedMs(),session_id:app.currentSessionId || null,event_seq:recorderEventSeq()};
  if (_screenVideo?.videoWidth) {
    const canvas=document.createElement('canvas');
    canvas.width=_screenVideo.videoWidth; canvas.height=_screenVideo.videoHeight;
    canvas.getContext('2d').drawImage(_screenVideo,0,0);
    payload.screenshot_at_ms=recorderElapsedMs();
    payload.screenshot_data_url=canvas.toDataURL('image/png');
  }
  // Keep the original frame and timestamp until acknowledged or explicitly
  // discarded. Retry never captures a new frame or binds to a later recording.
  const dialog=document.createElement('dialog');
  dialog.dataset.recorderDialog='true';
  dialog.setAttribute('aria-label','Capture issue');
  const label=document.createElement('label');
  label.textContent='Describe the issue (optional)';
  const input=document.createElement('textarea');input.rows=3;label.append(input);
  const status=document.createElement('p');status.setAttribute('role','status');
  const save=document.createElement('button');
  save.textContent='Save issue';save.className='render-recorder-action';
  const cancel=document.createElement('button');
  cancel.textContent='Discard issue';cancel.className='render-recorder-action';
  let saving=false,discardArmed=false;
  const close=()=>{dialog.close();dialog.remove();};
  save.onclick=async()=>{
    if(saving) return;
    saving=true;save.disabled=true;cancel.disabled=true;status.textContent='Saving…';
    payload.note=input.value.slice(0,2000);
    const lease=binding.recording_id===_recordingId
      ? {...binding,lease_token:_leaseToken} : binding;
    try {
      await _capturePost('markers',payload,lease);
      _gaps.delete('marker-upload-pending');
      if(_enabled) _phase=_gaps.size?'degraded':'recording';
      close();
    } catch (_) {
      _gaps.add('marker-upload-pending');
      if(_enabled) _phase='degraded';
      status.textContent='Upload failed. Your original capture and note are still here. Retry when connected.';
      save.textContent='Retry save';
    } finally {
      saving=false;save.disabled=false;cancel.disabled=false;_render();
    }
  };
  const discard=()=>{
    if(saving) return;
    if(!discardArmed) {
      discardArmed=true;cancel.textContent='Confirm discard';
      status.textContent='Discard this unsaved issue? The capture cannot be recovered after closing.';
      return;
    }
    _gaps.delete('marker-upload-pending');_gaps.add('issue-explicitly-discarded');
    close();_render();
  };
  cancel.onclick=discard;
  dialog.addEventListener('cancel',event=>{event.preventDefault();discard();});
  dialog.append(label,status,save,cancel);
  if(payload.screenshot_data_url) {
    const download=document.createElement('a');download.className='render-recorder-action';
    download.href=payload.screenshot_data_url;download.download='recorder-issue-'+payload.elapsed_ms+'.png';
    download.textContent='Save image locally';dialog.append(download);
  }
  document.body.append(dialog);dialog.showModal();input.focus();
}

// RECORDER-ARMING: preferences contain only a boolean, keyed by account.
// Evidence stays in the collector RAM ring until the header switch is used.
function _preferenceKey() {
  return 'webagent.recorder.prebuffer.v1.'+encodeURIComponent(_preferenceOwner);
}
function _loadPreference() {
  const owner=String(app.currentUserId || getBrowserStorageContext().owner_scope || '');
  if(owner===_preferenceOwner) return;
  _stopLocal('account-changed');_disarmLocal();
  _preferenceOwner=owner;_prebufferEnabled=false;
  if(owner && browserPersistenceAllowed()) {
    try {_prebufferEnabled=localStorage.getItem(_preferenceKey())==='true';} catch (_) {}
  }
}
async function _sendArmed(enabled,sessionId=app.currentSessionId,agentId=app.currentAgentId) {
  const response=await fetch(apiPath('/api/v1/recordings/armed'),{
    method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},
    body:JSON.stringify({enabled,client_id:_clientId,session_id:sessionId || null,agent_id:agentId || null}),
    signal:AbortSignal.timeout(5000),
  });
  if(!response.ok) throw new Error('arming-'+response.status);
  return response.json();
}
function _disarmLocal() {
  _armed=false;_armExpiresAt=0;_armRenewAt=0;
  disarmRecorder();
  if(!_enabled) delete window.__recorderScope;
  _sendArmed(false).catch(()=>{}); // Expiring server lease covers an offline tab.
}
async function _armLocal(sessionId=app.currentSessionId,agentId=app.currentAgentId,force=false) {
  if(!_prebufferEnabled || !_isAdmin() || !_available) return;
  if(_arming) {
    if(force) {await _armBarrier;return _armLocal(sessionId,agentId,true);}
    return;
  }
  if(!force && _armed && Date.now()<_armRenewAt) return;
  const owner=_preferenceOwner;
  _arming=true;
  let release;_armBarrier=new Promise(resolve=>{release=resolve;});
  try {
    const data=await _sendArmed(true,sessionId,agentId);
    if(!_prebufferEnabled || !_isAdmin() || owner!==_preferenceOwner) {_sendArmed(false).catch(()=>{});return;}
    if(data.armed!==true) throw new Error('arming-unavailable');
    _armExpiresAt=Date.now()+30000;_armRenewAt=Date.now()+10000;
    _armed=true;_armError='';
    if(!_enabled && !_busy) armRecorder({client_id:_clientId,config:_config});
    window.__recorderScope=_scopeHook;
  } catch (_) {
    _armError='Pre-buffer connection unavailable; retrying.';
    if(Date.now()>=_armExpiresAt) {_armed=false;disarmRecorder();}
  } finally {_arming=false;release();_render();}
}
async function _setPrebuffer(value) {
  _prebufferEnabled=!!value;
  if(_preferenceOwner && browserPersistenceAllowed()) {
    try {localStorage.setItem(_preferenceKey(),String(_prebufferEnabled));} catch (_) {}
  }
  if(_prebufferEnabled) await _armLocal(undefined,undefined,true);
  else {_armError='';_disarmLocal();}
  _render();
}

async function _fetchStatus() {
  if (!_isAdmin()) {if(_armed) _disarmLocal(); _render(); return; }
  try {
    const response = await fetch(apiPath(STATUS_URL), { headers: authHeaders(), cache: 'no-store' });
    if (!response.ok) {
      if ([403,404,410].includes(response.status)) {
        _available = false;
        _stopLocal('admin-access-lost');_disarmLocal();
      }
      _render();
      return;
    }
    const data = await response.json();
    _available = data.available === true;
    _config = (data.config && typeof data.config === 'object') ? data.config : _config;
    if (!_available) {_stopLocal('unavailable');if(_armed) _disarmLocal();}
    _loadPreference();
    _recoverFinishing();
    if(_prebufferEnabled) _armLocal();
    _render();

    if (_reloadIntent && _available && !_enabled && !_busy) {
      const intent = _reloadIntent;
      _reloadIntent = null;
      await _startLocal(intent.recording_id, Number(intent.ends_at) || 0);
    } else if (_enabled && _leaseRenewAt && Date.now() >= _leaseRenewAt) {
      _renewLease();
    }
  } catch (_) {
    if (_enabled && _leaseExpiresAt && Date.now() >= _leaseExpiresAt) {
      _stopLocal('lease-expired');
    } else {
      _render();
    }
  }
}

function _toggle(forceOff = false) {
  if (!_isAdmin() || _busy || (!_available && !forceOff)) return;
  if (forceOff || _enabled) {
    _stopLocal(forceOff ? 'automatic-stop' : 'user-stop');
    return;
  }
  _startLocal();
}

function initRenderRecorderToggle() {
  _control = document.getElementById('render-recorder-control');
  _button = document.getElementById('render-recorder-toggle');
  if (!_button || !_control) return;
  // RECORDER-HOOK: materialize the label beside the inert index.html switch.
  // Removing this plugin leaves no active popover or capture event handlers.
  _button.querySelector('.render-recorder-label')?.remove();
  const label=document.createElement('button');
  label.type='button';label.className='render-recorder-label';label.textContent='Recorder';
  label.setAttribute('aria-controls','render-recorder-popover');
  label.setAttribute('aria-expanded','false');
  label.addEventListener('click',()=>_showPopover(true));
  _control.insertBefore(label,_button);
  _control.querySelector('.render-recorder-popover')?.setAttribute('aria-hidden','true');
  const panel=_control.querySelector('.render-recorder-popover');
  const setting=document.createElement('label');setting.className='render-recorder-setting';
  const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.dataset.recorderPrebuffer='true';
  checkbox.addEventListener('change',()=>_setPrebuffer(checkbox.checked));
  const text=document.createElement('span');text.textContent='Include the previous 5 seconds';
  setting.append(checkbox,text);
  const explanation=document.createElement('span');explanation.className='render-recorder-buffer-status';
  const finishing=document.createElement('span');finishing.className='render-recorder-finishing';finishing.setAttribute('role','status');finishing.hidden=true;
  panel?.append(setting,explanation,finishing);
  document.addEventListener('pointerdown',event=>{if(_configOpen && !_control.contains(event.target)) _closePopover();});
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape' && _configOpen) {_closePopover();label.focus();}
  });
  window.addEventListener('webagent-browser-storage-policy',()=>{
    if(!browserPersistenceAllowed() && _preferenceOwner) {
      try {localStorage.removeItem(_preferenceKey());} catch (_) {}
    }
    _loadPreference();_render();
  });

  for (const [action,label,handler] of [
    ['marker','Capture issue',_captureIssue]]) {
    const button=document.createElement('button');button.type='button';
    button.dataset.recorderAction=action;button.className='render-recorder-action';
    button.textContent=label;button.title=label;button.setAttribute('aria-label',label);button.hidden=true;
    if (action==='marker') { button.textContent='';const glyph=document.createElement('i');glyph.dataset.lucide='camera';glyph.style.cssText='width:16px;height:16px';button.append(glyph); }
    button.addEventListener('click',handler);
    _control.append(button);
  }
  _scopeHook = (sessionId,agentId) => {
    const binding={recording_id:_recordingId,lease_token:_leaseToken,client_id:_clientId};
    const captureScope=_enabled ? _scopeChain.catch(()=>{}).then(()=>_scope(sessionId,agentId,binding)) : Promise.resolve();
    _scopeChain=captureScope;
    if(_prebufferEnabled) return Promise.all([captureScope,_armLocal(sessionId,agentId,true)]);
    return captureScope;
  };
  window.addEventListener('render-recorder-health',event=>{
    const reason=event.detail?.reason || 'capture-gap';
    const captureId=event.detail?.recording_id;
    if(captureId && captureId!==_recordingId) {
      const pending=_finishing.get(captureId);
      if(pending && reason!=='upload-pending') {
        if(event.detail?.resolved) pending.gaps.delete(reason); else pending.gaps.add(reason);
        _persistFinish(pending);
      }
      _render();return;
    }
    if(event.detail?.resolved) _gaps.delete(reason); else _gaps.add(reason);
    if(_enabled && !['uploading','finalizing','upload_pending'].includes(_phase)) _phase=_gaps.size?'degraded':'recording';
    _render();
  });
  _reloadIntent = _readReloadIntent();

  document.addEventListener('click', (event) => {
    if (event.target && typeof event.target.closest === 'function'
        && event.target.closest('#render-recorder-toggle')) _toggle(false);
  });

  window.addEventListener('admin-status-loaded', (event) => {
    _adminKnown=true;
    _isAdminUser = !!(event.detail && event.detail.is_admin);
    if(!_isAdminUser) {_stopLocal('admin-access-lost');_disarmLocal();_preferenceOwner='';_prebufferEnabled=false;}
    _render();
    if (_isAdminUser) _fetchStatus();
  });

  window.addEventListener('app-function-changed', (event) => {
    if (event.detail && event.detail.id === 'render_recorder') _fetchStatus();
  });

  window.addEventListener('render-recorder-upload-rejected', () => {
    _stopLocal('upload-rejected');
  });

  _render();
  _fetchStatus();
  setInterval(_fetchStatus, STATUS_POLL_MS);
}

initRenderRecorderToggle();
