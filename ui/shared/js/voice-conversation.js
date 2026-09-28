'use strict';

// Ephemeral streaming voice conversation. Audio and quick voice questions never enter
// the interactions table. Only explicit work-changing messages are handed to
// app.sendChatMessage, where they follow the ordinary durable agent path.

import { app } from './state.js';
import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';
import { textForSpeech } from './voice-conversation-core.js';
import { speakVoiceAudio } from './voice-audio.js';
import {
  getVoiceSessionId,
  isStreamingVoiceActive,
  isStreamingVoiceSupported,
  setVoiceSessionId,
  startStreamingVoice,
  stopStreamingVoice,
} from './voice-stream.js';

let _initialized = false;
let _capture = null;
let _awaitingFinalForSession = '';
let _awaitingFinalSince = 0;
let _caption = null;
let _captionTimer = null;
let _voiceControls = null;
let _pendingProposalId = '';
let _narrationLoopKey = '';
let _narrationAbort = null;
let _lastSpokenText = '';
let _lastSpokenAt = 0;

function _dispatchInput(input) {
  if (!input) return;
  const EventCtor = input.ownerDocument?.defaultView?.Event || globalThis.Event;
  if (EventCtor) input.dispatchEvent(new EventCtor('input', { bubbles: true }));
}

function _ensureCaption() {
  if (_caption?.isConnected) return _caption;
  const area = document.getElementById('chat-input-area');
  const pill = document.getElementById('chat-input-row');
  if (!area || !pill) return null;
  const el = document.createElement('div');
  el.className = 'voice-conversation-caption';
  el.hidden = true;
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  area.insertBefore(el, pill);
  _caption = el;
  return el;
}

function _ensureVoiceControls() {
  if (_voiceControls?.isConnected) return _voiceControls;
  const area = document.getElementById('chat-input-area');
  const pill = document.getElementById('chat-input-row');
  if (!area || !pill) return null;
  const controls = document.createElement('div');
  controls.className = 'voice-conversation-controls';
  controls.setAttribute('role', 'group');
  controls.setAttribute('aria-label', 'Voice conversation settings');

  const detail = document.createElement('button');
  detail.type = 'button';
  detail.className = 'voice-conversation-control';
  const syncDetail = () => {
    const value = localStorage.getItem('webagent_voice_narration_detail') || 'normal';
    detail.textContent = `Updates: ${value}`;
    detail.title = 'Change spoken run-update detail';
  };
  detail.addEventListener('click', () => {
    const levels = ['quiet', 'normal', 'detailed'];
    const current = localStorage.getItem('webagent_voice_narration_detail') || 'normal';
    localStorage.setItem('webagent_voice_narration_detail', levels[(levels.indexOf(current) + 1) % levels.length]);
    syncDetail();
  });
  syncDetail();

  const mute = document.createElement('button');
  mute.type = 'button';
  mute.className = 'voice-conversation-control';
  const syncMute = () => {
    const muted = localStorage.getItem('webagent_voice_muted') === 'true';
    mute.textContent = muted ? 'Unmute' : 'Mute';
    mute.setAttribute('aria-pressed', String(muted));
    mute.title = muted ? 'Enable spoken responses' : 'Mute spoken responses';
  };
  mute.addEventListener('click', () => {
    const muted = localStorage.getItem('webagent_voice_muted') === 'true';
    localStorage.setItem('webagent_voice_muted', String(!muted));
    if (!muted) _stopSpeech();
    syncMute();
  });
  syncMute();
  controls.append(detail, mute);
  area.insertBefore(controls, pill);
  _voiceControls = controls;
  return controls;
}

function _showCaption(text, { kind = '', persist = false } = {}) {
  const el = _ensureCaption();
  if (!el) return;
  if (_captionTimer) clearTimeout(_captionTimer);
  _captionTimer = null;
  el.textContent = String(text || '').trim();
  el.dataset.kind = kind;
  el.hidden = !el.textContent;
  if (!persist && el.textContent) {
    _captionTimer = setTimeout(() => {
      el.hidden = true;
      el.textContent = '';
      el.dataset.kind = '';
      _captionTimer = null;
    }, 8000);
  }
}

function _systemText(text) {
  const clean = String(text || '').trim();
  return clean.startsWith('System:') ? clean : 'System: ' + clean;
}

function _showSystemMessage(text, { persist = false } = {}) {
  _showCaption(_systemText(text), { kind: 'system', persist });
}

function _showRouteResponse(route, fallback = '') {
  const text = String(route?.answer || fallback || '').trim();
  if (!text) return;
  if (route?.response_source === 'system') {
    if (!route?.issue) _speak(_systemText(text));
    _showSystemMessage(text);
    return;
  }
  _showCaption(text, { kind: 'answer' });
  _speak(text);
}

function _stopSpeech() {
  try { document.dispatchEvent(new CustomEvent('tts:stop')); } catch (_) {}
  if (!('speechSynthesis' in window)) return;
  try { window.speechSynthesis.cancel(); } catch (_) {}
}

function _renderStreamingButton(button, state = 'idle') {
  if (!button) return;
  const recording = state === 'listening' || state === 'user_speaking';
  const busy = !recording && state !== 'idle';
  const labels = {
    requesting_microphone: 'Requesting microphone…',
    connecting: 'Connecting voice…',
    reconnecting: 'Voice reconnecting…',
    listening: 'Listening — click to finish',
    user_speaking: 'Listening — click to finish',
    finishing: 'Finishing voice turn…',
    transcribing: 'Understanding…',
  };
  const label = labels[state] || 'Voice conversation';
  button.classList.toggle('recording', recording);
  button.classList.toggle('transcribing', busy);
  button.setAttribute('aria-pressed', state === 'idle' ? 'false' : 'true');
  button.setAttribute('aria-label', label);
  button.title = label;
  button.closest('.chat-pill')?.classList.toggle('dictating', state !== 'idle');
}

function _handleStreamingState(state) {
  _renderStreamingButton(_capture?.button, state);
  const captions = {
    requesting_microphone: 'Waiting for microphone permission…',
    connecting: 'Connecting voice…',
    reconnecting: 'Voice connection interrupted. Reconnecting…',
    listening: 'Listening… Speak naturally; silence will finish the turn.',
    user_speaking: 'Listening…',
    finishing: 'Finishing the voice turn…',
    transcribing: 'Understanding your message…',
  };
  if (captions[state]) {
    _showCaption(captions[state], {
      kind: state === 'listening' || state === 'user_speaking' ? 'listening' : 'working',
      persist: true,
    });
  }
}

function _streamErrorMessage(code, detail = '') {
  if (code === 'microphone_denied') return detail || 'Microphone permission is blocked for this site. Use the address-bar site controls to allow it, then try again.';
  if (code === 'voice_access_denied') return detail || 'Voice streaming is not available for this account.';
  if (code === 'transcription_failed') return detail || 'The voice message could not be transcribed.';
  if (code === 'connection_lost') return 'The voice connection was lost. Tap the microphone to try again.';
  return detail || 'The voice connection could not start.';
}

function _speak(text) {
  if (localStorage.getItem('webagent_voice_muted') === 'true') return false;
  const spoken = textForSpeech(text);
  if (!spoken) return false;
  if (spoken === _lastSpokenText && Date.now() - _lastSpokenAt < 2500) return false;
  _lastSpokenText = spoken;
  _lastSpokenAt = Date.now();
  _stopSpeech();
  speakVoiceAudio(spoken).then(played => {
    if (played || !('speechSynthesis' in window)) return;
    try {
      const utterance = new SpeechSynthesisUtterance(spoken);
      utterance.rate = 1.03;
      window.speechSynthesis.speak(utterance);
    } catch (_) {}
  });
  return true;
}

function _startNarrationLoop(voiceSessionId) {
  const sessionId = String(app.currentSessionId || '');
  const userId = String(app.currentUserId || '');
  const id = String(voiceSessionId || '');
  if (!sessionId || !userId || !id) return;
  _ensureVoiceControls();
  const key = `${userId}\u0000${sessionId}\u0000${id}`;
  if (_narrationLoopKey === key) return;
  try { _narrationAbort?.abort(); } catch (_) {}
  _narrationAbort = new AbortController();
  _narrationLoopKey = key;
  const signal = _narrationAbort.signal;
  (async () => {
    while (!signal.aborted && _narrationLoopKey === key) {
      if (String(app.currentSessionId || '') !== sessionId) break;
      try {
        const response = await fetch(apiPath('/api/v1/chat/voice/narration/next'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          signal,
          body: JSON.stringify({
            user_id: userId,
            session_id: sessionId,
            voice_session_id: id,
            detail: localStorage.getItem('webagent_voice_narration_detail') || 'normal',
            timeout_seconds: 20,
          }),
        });
        if (response.status === 410 || response.status === 404) break;
        if (!response.ok) throw new Error('Narration unavailable');
        const body = await response.json();
        const narration = body.narration || null;
        const spoken = String(narration?.text || '').trim();
        if (!spoken) continue;
        if (_capture || isStreamingVoiceActive()) {
          continue;
        }
        _showCaption(spoken, { kind: 'answer' });
        _speak(spoken);
      } catch (_) {
        if (signal.aborted) break;
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
    }
    if (_narrationLoopKey === key) _narrationLoopKey = '';
  })();
}

async function _routeUtterance(utterance) {
  if (!app.currentSessionId || !app.currentUserId) {
    return {
      action: 'noop',
      answer: 'There is no active chat session yet.',
      response_source: 'system',
      issue: 'no_active_session',
    };
  }
  const response = await fetch(apiPath('/api/v1/chat/voice/route'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      user_id: app.currentUserId,
      session_id: app.currentSessionId,
      utterance,
      voice_session_id: getVoiceSessionId(app.currentUserId, app.currentSessionId) || undefined,
      proposal_id: _pendingProposalId || undefined,
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || 'Voice routing failed');
  if (body.voice_session_id) {
    setVoiceSessionId(app.currentUserId, app.currentSessionId, body.voice_session_id);
    _startNarrationLoop(body.voice_session_id);
  }
  const proposal = body.proposal || null;
  _pendingProposalId = proposal?.state === 'pending'
    ? String(proposal.proposal_id || '')
    : '';
  return body;
}

async function _requestVoiceControl(action) {
  const response = await fetch(apiPath('/api/v1/chat/voice/control'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      user_id: app.currentUserId,
      session_id: app.currentSessionId,
      voice_session_id: getVoiceSessionId(app.currentUserId, app.currentSessionId) || undefined,
      action,
      idempotency_key: globalThis.crypto?.randomUUID?.()
        || `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || 'Voice control failed');
  return body;
}

async function _handleTranscript(transcript) {
  const text = String(transcript || '').trim();
  const capture = _capture;
  _capture = null;
  _renderStreamingButton(capture?.button, 'idle');
  if (capture?.input) {
    capture.input.value = capture.draft;
    try { capture.input.setSelectionRange(capture.start, capture.end); } catch (_) {}
    _dispatchInput(capture.input);
  }
  if (!text) {
    _showSystemMessage('No speech was detected. Tap the microphone to try again.');
    return;
  }
  _showCaption('Understanding your request…', { kind: 'working', persist: true });
  let route;
  try {
    route = await _routeUtterance(text);
  } catch (_) {
    _showSystemMessage('The Voice routing request failed. Please try again or use text chat.');
    return;
  }

  const answer = String(route.answer || '').trim();
  if (route.action === 'interrupt' || route.control_action) {
    _awaitingFinalForSession = '';
    _awaitingFinalSince = 0;
    let controlAnswer = '';
    let controlIssue = '';
    try {
      const controlled = await _requestVoiceControl(route.control_action || 'stop');
      controlAnswer = String(controlled.message || '').trim();
    } catch (_) {
      if ((route.control_action || 'stop') === 'stop') {
        // Existing stop plumbing is a safe compatibility fallback.
        try { app.abortChatStream?.(); } catch (_) {}
        try { app._sendStopMessage?.(); } catch (_) {}
        controlIssue = 'The Voice control request failed; the chat stop fallback was used.';
      } else {
        controlIssue = 'The Voice control request failed, so the run control was not applied.';
      }
    }
    if (controlIssue) {
      _showSystemMessage(controlIssue);
    } else {
      _showSystemMessage(controlAnswer || answer || 'The run control was applied.');
    }
    return;
  }

  if (route.action !== 'dispatch_work' || !String(route.command || '').trim()) {
    _showRouteResponse(route, 'Voice returned no response.');
    return;
  }

  if (typeof app.sendChatMessage !== 'function') {
    _showSystemMessage('Chat is not ready, so the Voice work request was not sent.');
    return;
  }
  _awaitingFinalForSession = app.currentSessionId || '';
  _awaitingFinalSince = Date.now();
  _showSystemMessage('Sent to the work agent.');
  try {
    await app.sendChatMessage(String(route.command).trim());
  } catch (_) {
    _awaitingFinalForSession = '';
    _awaitingFinalSince = 0;
    _showSystemMessage('The Voice work request could not be sent to the agent.');
  }
}

function _cancelCapture() {
  const capture = _capture;
  _capture = null;
  _renderStreamingButton(capture?.button, 'idle');
  if (capture?.input) {
    capture.input.value = capture.draft;
    try { capture.input.setSelectionRange(capture.start, capture.end); } catch (_) {}
    _dispatchInput(capture.input);
  }
  _showSystemMessage(capture?.error || 'No voice message was captured. Tap the microphone to try again.');
}

export async function beginVoiceTurn({
  button,
  input,
  captureActive,
  toggleCapture,
}) {
  if (!button || !input || typeof toggleCapture !== 'function') return false;
  _stopSpeech();

  if (_capture?.starting && !captureActive() && !isStreamingVoiceActive()) return true;

  if (isStreamingVoiceActive()) {
    _showCaption('Finishing the voice turn…', { kind: 'working', persist: true });
    stopStreamingVoice();
    return true;
  }

  if (captureActive()) {
    _showCaption('Finishing the recording…', { kind: 'working', persist: true });
    await toggleCapture({ onComplete: _handleTranscript, onCancel: _cancelCapture });
    return true;
  }

  _capture = {
    button,
    input,
    draft: input.value || '',
    start: Number.isInteger(input.selectionStart) ? input.selectionStart : (input.value || '').length,
    end: Number.isInteger(input.selectionEnd) ? input.selectionEnd : (input.value || '').length,
    starting: true,
  };
  input.value = '';
  _dispatchInput(input);
  _showCaption('Listening… Tap the stop button when you are finished.', {
    kind: 'listening', persist: true,
  });

  if (isStreamingVoiceSupported() && app.currentSessionId && app.currentUserId) {
    const started = await startStreamingVoice({
      button,
      userId: app.currentUserId,
      sessionId: app.currentSessionId,
      agentId: app.currentAgentId,
      onState: _handleStreamingState,
      onPartial: text => _showCaption(`“${text}”`, { kind: 'partial', persist: true }),
      onSpeechStart: _stopSpeech,
      onVoiceSession: voiceSessionId => _startNarrationLoop(voiceSessionId),
      onComplete: _handleTranscript,
      onCancel: _cancelCapture,
      onError: (code, detail) => {
        if (_capture) _capture.error = _streamErrorMessage(code, detail);
      },
    });
    if (_capture) _capture.starting = false;
    if (started) return true;
    if (!_capture) return false;
  }

  const started = await toggleCapture({
    onComplete: _handleTranscript,
    onCancel: _cancelCapture,
  });
  if (_capture) _capture.starting = false;
  if (!started && _capture) {
    input.value = _capture.draft;
    try { input.setSelectionRange(_capture.start, _capture.end); } catch (_) {}
    _capture = null;
    _dispatchInput(input);
    _showSystemMessage('The microphone could not start.');
  }
  return !!started;
}

export function initVoiceConversation() {
  if (_initialized) return;
  _initialized = true;
  _ensureCaption();
  window.addEventListener('webagent-agent-response', event => {
    const detail = event?.detail || {};
    const sid = String(detail.sessionId || '');
    if (!_awaitingFinalForSession || sid !== _awaitingFinalForSession) return;
    if (detail.replayed) return;
    const createdAt = Date.parse(String(detail.createdAt || ''));
    if (Number.isFinite(createdAt) && createdAt < _awaitingFinalSince - 2000) return;
    _awaitingFinalForSession = '';
    _awaitingFinalSince = 0;
    const content = String(detail.content || '').trim();
    if (!content) return;
    _showCaption('The agent finished. The full response is in the chat.', { kind: 'answer' });
    _speak(content);
  });
  document.addEventListener('tts:stop', () => {
    // Deliberately no state reset: composing a typed message may stop speech,
    // but the next final response for a voice-started turn should still speak.
  });
}

export { textForSpeech };
