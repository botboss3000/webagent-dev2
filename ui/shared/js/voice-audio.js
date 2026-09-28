'use strict';

// Optional server speech playback. The server sanitizes text before sending it
// to a configured speech provider; browser speech remains the zero-config path.

import { app } from './state.js';
import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';
import { getVoiceSessionId } from './voice-stream.js';

let _capability = null;
let _capabilityAt = 0;
let _abort = null;
let _audio = null;
let _objectUrl = '';

export function stopVoiceAudio() {
  try { _abort?.abort(); } catch (_) {}
  _abort = null;
  try { _audio?.pause(); } catch (_) {}
  _audio = null;
  if (_objectUrl) {
    try { URL.revokeObjectURL(_objectUrl); } catch (_) {}
    _objectUrl = '';
  }
}

async function _serverAvailable() {
  if (_capability && Date.now() - _capabilityAt < 300_000) return !!_capability.available;
  if (!app.currentUserId || !app.currentSessionId) return false;
  try {
    const query = new URLSearchParams({
      user_id: app.currentUserId,
      session_id: app.currentSessionId,
    });
    const response = await fetch(apiPath(`/api/v1/chat/voice/speech/capabilities?${query}`), {
      headers: authHeaders(),
    });
    _capability = response.ok ? await response.json() : { available: false };
  } catch (_) {
    _capability = { available: false };
  }
  _capabilityAt = Date.now();
  return !!_capability.available;
}

export async function speakVoiceAudio(text) {
  const spoken = String(text || '').trim();
  const voiceSessionId = getVoiceSessionId(app.currentUserId, app.currentSessionId);
  if (!spoken || !voiceSessionId || !await _serverAvailable()) return false;
  stopVoiceAudio();
  const abort = new AbortController();
  _abort = abort;
  try {
    const response = await fetch(apiPath('/api/v1/chat/voice/speech'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      signal: abort.signal,
      body: JSON.stringify({
        user_id: app.currentUserId,
        session_id: app.currentSessionId,
        voice_session_id: voiceSessionId,
        text: spoken,
      }),
    });
    if (!response.ok) return false;
    const blob = await response.blob();
    if (abort.signal.aborted || !blob.size) return false;
    _objectUrl = URL.createObjectURL(blob);
    _audio = new Audio(_objectUrl);
    _audio.addEventListener('ended', stopVoiceAudio, { once: true });
    await _audio.play();
    return true;
  } catch (_) {
    if (!abort.signal.aborted) _capabilityAt = 0;
    stopVoiceAudio();
    return false;
  }
}

document.addEventListener('tts:stop', stopVoiceAudio);
