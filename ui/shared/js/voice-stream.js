'use strict';

// Low-latency voice transport. Audio is downsampled to mono PCM16 in the
// browser, streamed to the server for VAD/transcription, and never persisted.

import { voiceStreamWsUrl } from './config.js';
import { getAuthToken } from './left-login.js';
import {
  microphoneAccessMessage,
  requestMicrophoneAccess,
} from './microphone-diagnostics.js';

const TARGET_SAMPLE_RATE = 16_000;
const MAX_RECONNECTS = 3;
const MAX_QUEUED_FRAMES = 12;
const voiceSessionIds = new Map();

function voiceSessionKey(userId, chatSessionId) {
  return `${String(userId || '')}\u0000${String(chatSessionId || '')}`;
}

export function getVoiceSessionId(userId, chatSessionId) {
  return voiceSessionIds.get(voiceSessionKey(userId, chatSessionId)) || '';
}

export function setVoiceSessionId(userId, chatSessionId, voiceSessionId) {
  const key = voiceSessionKey(userId, chatSessionId);
  const value = String(voiceSessionId || '').trim();
  if (value) voiceSessionIds.set(key, value);
  else voiceSessionIds.delete(key);
}

export function downsampleToPcm16(input, inputRate, outputRate = TARGET_SAMPLE_RATE) {
  if (!input?.length || !inputRate || outputRate > inputRate) return new ArrayBuffer(0);
  const ratio = inputRate / outputRate;
  const outputLength = Math.max(1, Math.floor(input.length / ratio));
  const output = new Int16Array(outputLength);
  for (let i = 0; i < outputLength; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.max(start + 1, Math.floor((i + 1) * ratio)));
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j];
    const sample = Math.max(-1, Math.min(1, sum / (end - start)));
    output[i] = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
  }
  return output.buffer;
}

export function isStreamingVoiceSupported() {
  const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
  return !!(
    window.isSecureContext
    && window.WebSocket
    && AudioContextCtor
    && navigator.mediaDevices?.getUserMedia
  );
}

class StreamingVoiceTransport {
  constructor() {
    this.session = null;
  }

  get active() {
    return this.session !== null;
  }

  async start(options = {}) {
    if (this.session || !isStreamingVoiceSupported()) return false;
    const session = {
      ...options,
      stream: null,
      context: null,
      source: null,
      processor: null,
      sink: null,
      socket: null,
      socketGeneration: 0,
      ready: false,
      stopping: false,
      finished: false,
      reconnects: 0,
      reconnectTimer: null,
      queuedFrames: [],
      voiceSessionRecoveryAttempted: false,
      latestRevision: -1,
      latestTurnId: 0,
      voiceSessionId: options.voiceSessionId
        || getVoiceSessionId(options.userId, options.sessionId),
    };
    this.session = session;
    this._state(session, 'requesting_microphone');
    try {
      session.stream = await requestMicrophoneAccess({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (this.session !== session) {
        session.stream.getTracks().forEach(track => track.stop());
        return false;
      }
      const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
      session.context = new AudioContextCtor();
      session.source = session.context.createMediaStreamSource(session.stream);
      session.processor = session.context.createScriptProcessor(4096, 1, 1);
      session.sink = session.context.createGain();
      session.sink.gain.value = 0;
      session.processor.onaudioprocess = event => {
        if (this.session !== session || session.stopping) return;
        const pcm = downsampleToPcm16(
          event.inputBuffer.getChannelData(0),
          session.context.sampleRate,
        );
        if (!pcm.byteLength) return;
        if (session.ready && session.socket?.readyState === WebSocket.OPEN) {
          session.socket.send(pcm);
        } else {
          session.queuedFrames.push(pcm);
          if (session.queuedFrames.length > MAX_QUEUED_FRAMES) session.queuedFrames.shift();
        }
      };
      session.source.connect(session.processor);
      session.processor.connect(session.sink);
      session.sink.connect(session.context.destination);
      this._connect(session);
      return true;
    } catch (error) {
      this._fail(session, 'microphone_failed', await microphoneAccessMessage(error));
      return false;
    }
  }

  stop() {
    const session = this.session;
    if (!session || session.stopping) return false;
    session.stopping = true;
    this._stopCapture(session);
    this._state(session, 'finishing');
    if (session.ready && session.socket?.readyState === WebSocket.OPEN) {
      session.socket.send(JSON.stringify({ type: 'stop' }));
    } else {
      this._fail(session, 'connection_lost');
    }
    return true;
  }

  cancel() {
    const session = this.session;
    if (!session) return;
    try {
      if (session.socket?.readyState === WebSocket.OPEN) {
        session.socket.send(JSON.stringify({ type: 'cancel' }));
      }
    } catch (_) {}
    this._finish(session, '', true);
  }

  _connect(session) {
    if (this.session !== session || session.finished) return;
    const generation = ++session.socketGeneration;
    session.ready = false;
    this._state(session, session.reconnects ? 'reconnecting' : 'connecting');
    let socket;
    try {
      socket = new WebSocket(voiceStreamWsUrl());
      socket.binaryType = 'arraybuffer';
      session.socket = socket;
    } catch (_) {
      this._scheduleReconnect(session);
      return;
    }
    socket.onopen = () => {
      if (this.session !== session || generation !== session.socketGeneration) return;
      socket.send(JSON.stringify({
        mode: 'voice_stream',
        user_id: session.userId,
        session_id: session.sessionId,
        agent_id: session.agentId || undefined,
        token: getAuthToken() || '',
        sample_rate: TARGET_SAMPLE_RATE,
        language: navigator.language || 'en-US',
        voice_session_id: session.voiceSessionId || undefined,
      }));
    };
    socket.onmessage = event => {
      if (this.session !== session || generation !== session.socketGeneration) return;
      let message;
      try { message = JSON.parse(event.data); } catch (_) { return; }
      this._message(session, message);
    };
    socket.onerror = () => {};
    socket.onclose = () => {
      if (this.session !== session || generation !== session.socketGeneration || session.finished) return;
      session.ready = false;
      if (session.stopping) this._fail(session, 'connection_lost');
      else this._scheduleReconnect(session);
    };
  }

  _message(session, message) {
    if (message.type === 'ready') {
      session.ready = true;
      session.reconnects = 0;
      if (message.voice_session_id) {
        session.voiceSessionId = String(message.voice_session_id);
        setVoiceSessionId(session.userId, session.sessionId, session.voiceSessionId);
        try { session.onVoiceSession?.(session.voiceSessionId, message); } catch (_) {}
      }
      this._state(session, 'listening');
      for (const frame of session.queuedFrames.splice(0)) session.socket.send(frame);
      return;
    }
    if (message.type === 'speech_start') {
      session.latestTurnId = Number(message.turn_id || 0);
      session.latestRevision = -1;
      this._state(session, 'user_speaking');
      try { session.onSpeechStart?.(); } catch (_) {}
      return;
    }
    if (message.type === 'turn_end') {
      this._stopCapture(session);
      this._state(session, 'transcribing');
      return;
    }
    if (message.type === 'transcript') {
      const turnId = Number(message.turn_id || 0);
      const revision = Number(message.revision || 0);
      if (turnId < session.latestTurnId || revision < session.latestRevision) return;
      session.latestTurnId = turnId;
      session.latestRevision = revision;
      const text = String(message.text || '').trim();
      if (message.final) this._finish(session, text, !text);
      else if (text) {
        try { session.onPartial?.(text); } catch (_) {}
      }
      return;
    }
    if (message.type === 'no_speech') {
      this._finish(session, '', true);
      return;
    }
    if (message.type === 'error') {
      // A prior voice session can become invalid when its registry process is
      // replaced or the server rejects a stale chat/user association. Start one
      // fresh server-issued session rather than exposing that transient state.
      if (message.code === 'voice_session_mismatch' && !session.voiceSessionRecoveryAttempted) {
        session.voiceSessionRecoveryAttempted = true;
        session.voiceSessionId = '';
        setVoiceSessionId(session.userId, session.sessionId, '');
        try { session.socket?.close(4000, 'refreshing voice session'); } catch (_) {}
        this._connect(session);
        return;
      }
      this._fail(session, message.code || 'stream_failed', message.message || '');
    }
  }

  _scheduleReconnect(session) {
    if (session.reconnectTimer || session.reconnects >= MAX_RECONNECTS) {
      if (session.reconnects >= MAX_RECONNECTS) this._fail(session, 'connection_lost');
      return;
    }
    const delay = 350 * Math.pow(2, session.reconnects++);
    this._state(session, 'reconnecting');
    session.reconnectTimer = setTimeout(() => {
      session.reconnectTimer = null;
      this._connect(session);
    }, delay);
  }

  _stopCapture(session) {
    if (session.processor) session.processor.onaudioprocess = null;
    try { session.source?.disconnect(); } catch (_) {}
    try { session.processor?.disconnect(); } catch (_) {}
    try { session.sink?.disconnect(); } catch (_) {}
    for (const track of session.stream?.getTracks?.() || []) {
      try { track.stop(); } catch (_) {}
    }
    session.stream = null;
    if (session.context && session.context.state !== 'closed') {
      try { session.context.close(); } catch (_) {}
    }
    session.context = null;
  }

  _finish(session, text, cancelled = false) {
    if (this.session !== session || session.finished) return;
    session.finished = true;
    this._stopCapture(session);
    if (session.reconnectTimer) clearTimeout(session.reconnectTimer);
    session.reconnectTimer = null;
    try { session.socket?.close(1000, 'voice turn complete'); } catch (_) {}
    this.session = null;
    if (text && !cancelled) {
      try { session.onComplete?.(text); } catch (_) {}
    } else {
      try { session.onCancel?.(); } catch (_) {}
    }
  }

  _fail(session, code, message = '') {
    if (this.session !== session || session.finished) return;
    try { session.onError?.(code, message); } catch (_) {}
    this._finish(session, '', true);
  }

  _state(session, state) {
    try { session.onState?.(state); } catch (_) {}
  }
}

const transport = new StreamingVoiceTransport();

export const isStreamingVoiceActive = () => transport.active;
export const startStreamingVoice = options => transport.start(options);
export const stopStreamingVoice = () => transport.stop();
export const cancelStreamingVoice = () => transport.cancel();
export { StreamingVoiceTransport };
