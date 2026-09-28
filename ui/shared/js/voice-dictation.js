'use strict';

/**
 * SpeechRecognitionEvent.results is authoritative. An interim result may be
 * revised repeatedly at the same index before it becomes final, so rebuild the
 * transcript from the whole list on every event.
 */
export function transcriptFromResults(results) {
  let joined = '';
  for (let i = 0; i < (results?.length || 0); i++) {
    const transcript = results[i]?.[0]?.transcript;
    if (typeof transcript === 'string' && transcript.trim()) {
      const segment = transcript.trim();
      if (sameOrWordPrefix(joined, segment)) {
        // Chrome Android sometimes returns every cumulative hypothesis as a
        // separate "final" result: "test", "test 1", "test 1 2". Keep only
        // the newest/longest hypothesis instead of concatenating the history.
        joined = segment;
      } else if (!sameOrWordPrefix(segment, joined)) {
        joined += (needsSpace(joined, segment) ? ' ' : '') + segment;
      }
    }
  }
  return joined;
}

function sameOrWordPrefix(prefix, value) {
  if (!prefix || !value) return false;
  if (prefix === value) return true;
  if (!value.startsWith(prefix)) return false;
  const boundary = value.charAt(prefix.length);
  return !boundary || /[\s,.;:!?)}\]]/.test(boundary);
}

function needsSpace(left, right) {
  if (!left || !right || /\s$/.test(left) || /^\s/.test(right)) return false;
  return !/^[,.;:!?)}\]]/.test(right);
}

/** Insert a transcript at the selection that existed when dictation started. */
export function composeDictationValue(before, transcript, after) {
  if (!transcript) return `${before}${after}`;
  const leading = needsSpace(before, transcript) ? ' ' : '';
  const trailing = needsSpace(transcript, after) ? ' ' : '';
  return `${before}${leading}${transcript}${trailing}${after}`;
}

function appendTranscript(committed, next) {
  if (!committed) return next;
  if (!next) return committed;
  if (sameOrWordPrefix(committed, next)) return next;
  if (sameOrWordPrefix(next, committed)) return committed;
  return committed + (needsSpace(committed, next) ? ' ' : '') + next;
}

function dispatchInput(input) {
  const EventCtor = input?.ownerDocument?.defaultView?.Event || globalThis.Event;
  if (input && EventCtor) {
    input.dispatchEvent(new EventCtor('input', { bubbles: true }));
  }
}

function selectionSnapshot(input) {
  const selectionStart = Number.isInteger(input.selectionStart)
    ? input.selectionStart
    : input.value.length;
  const selectionEnd = Number.isInteger(input.selectionEnd)
    ? input.selectionEnd
    : selectionStart;
  return {
    before: input.value.slice(0, selectionStart),
    after: input.value.slice(selectionEnd),
  };
}

function applyTranscript(input, before, transcript, after) {
  const value = composeDictationValue(before, transcript, after);
  input.value = value;
  const caret = value.length - after.length;
  try { input.setSelectionRange(caret, caret); } catch {}
  dispatchInput(input);
}

/**
 * Owns one fresh SpeechRecognition object per dictation session. Session-local
 * handlers prevent late events from a stopped recognizer corrupting a new one.
 */
export class VoiceDictationController {
  constructor({
    createRecognition,
    language = () => 'en-US',
    renderButton = () => {},
    onError = () => {},
  }) {
    this.createRecognition = createRecognition;
    this.language = language;
    this.renderButton = renderButton;
    this.onError = onError;
    this.session = null;
  }

  get active() {
    return this.session !== null;
  }

  toggle(button, input, options = {}) {
    if (this.session) {
      this.stop();
      return false;
    }
    return this.start(button, input, options);
  }

  start(button, input, options = {}) {
    if (!input || this.session) return false;

    const selection = selectionSnapshot(input);
    const session = {
      recognition: null,
      button,
      input,
      before: selection.before,
      after: selection.after,
      committedTranscript: '',
      currentTranscript: '',
      stopping: false,
      stopTimer: null,
      restartTimer: null,
      onComplete: typeof options.onComplete === 'function' ? options.onComplete : null,
      onCancel: typeof options.onCancel === 'function' ? options.onCancel : null,
    };

    this.session = session;
    if (!this._beginRecognitionCycle(session)) {
      return false;
    }

    this.renderButton(button, 'recording');
    input.focus();
    return true;
  }

  _beginRecognitionCycle(session) {
    if (this.session !== session || session.stopping) return false;
    let recognition;
    try {
      recognition = this.createRecognition();
    } catch (error) {
      this.onError('start-failed', error);
      this._finish(session);
      return false;
    }
    if (!recognition) {
      this.onError('unsupported');
      this._finish(session);
      return false;
    }

    session.recognition = recognition;
    session.currentTranscript = '';
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = this.language() || 'en-US';

    recognition.onresult = (event) => {
      if (this.session !== session || session.recognition !== recognition) return;
      session.currentTranscript = transcriptFromResults(event.results);
      const transcript = appendTranscript(
        session.committedTranscript,
        session.currentTranscript,
      );
      applyTranscript(session.input, session.before, transcript, session.after);
    };
    recognition.onerror = (event) => {
      if (this.session !== session || session.recognition !== recognition) return;
      const error = event?.error || 'recognition-error';
      this.onError(error, event);
      // Mobile recognizers commonly report no-speech/aborted immediately before
      // onend. Those are cycle boundaries, not user-requested session endings.
      if (error !== 'no-speech' && error !== 'aborted') this._finish(session);
    };
    recognition.onend = () => {
      if (this.session !== session || session.recognition !== recognition) return;
      if (session.stopping) {
        this._finish(session);
        return;
      }

      // Chrome Android and other engines may end after a short silence despite
      // continuous=true. Commit this cycle and restart invisibly; the Stop
      // button remains visible throughout the logical dictation session.
      session.committedTranscript = appendTranscript(
        session.committedTranscript,
        session.currentTranscript,
      );
      session.currentTranscript = '';
      session.recognition = null;
      session.restartTimer = globalThis.setTimeout(() => {
        session.restartTimer = null;
        this._beginRecognitionCycle(session);
      }, 150);
    };

    try {
      recognition.start();
      return true;
    } catch (error) {
      this.onError('start-failed', error);
      this._finish(session);
      return false;
    }
  }

  stop() {
    const session = this.session;
    if (!session || session.stopping) return;
    session.stopping = true;
    if (session.restartTimer !== null) {
      globalThis.clearTimeout(session.restartTimer);
      session.restartTimer = null;
    }
    this.renderButton(session.button, 'stopping');
    try {
      // stop(), unlike abort(), lets the engine deliver its last final result.
      if (!session.recognition) {
        this._finish(session);
        return;
      }
      session.recognition.stop();
      session.stopTimer = globalThis.setTimeout(
        () => this._finish(session),
        2000,
      );
    } catch {
      this._finish(session);
    }
  }

  _finish(session) {
    if (this.session !== session) return;
    const completedTranscript = session.stopping
      ? appendTranscript(session.committedTranscript, session.currentTranscript).trim()
      : '';
    this.session = null;
    if (session.stopTimer !== null) globalThis.clearTimeout(session.stopTimer);
    if (session.restartTimer !== null) globalThis.clearTimeout(session.restartTimer);
    if (session.recognition) {
      session.recognition.onresult = null;
      session.recognition.onerror = null;
      session.recognition.onend = null;
    }
    this.renderButton(session.button, 'idle');
    if (completedTranscript && session.onComplete) {
      try { session.onComplete(completedTranscript); } catch {}
    } else if (!completedTranscript && session.onCancel) {
      try { session.onCancel(); } catch {}
    }
  }
}

/**
 * Cross-browser fallback for browsers without SpeechRecognition. Records one
 * short clip locally, then asks the WebAgent server to transcribe it.
 */
export class RecordedAudioDictationController {
  constructor({
    getStream,
    createRecorder,
    transcribe,
    renderButton = () => {},
    onError = () => {},
    maxDurationMs = 60_000,
  }) {
    this.getStream = getStream;
    this.createRecorder = createRecorder;
    this.transcribe = transcribe;
    this.renderButton = renderButton;
    this.onError = onError;
    this.maxDurationMs = maxDurationMs;
    this.session = null;
  }

  get active() {
    return this.session !== null;
  }

  async toggle(button, input, options = {}) {
    if (this.session) {
      this.stop();
      return false;
    }
    return this.start(button, input, options);
  }

  async start(button, input, options = {}) {
    if (!input || this.session) return false;
    const selection = selectionSnapshot(input);
    const session = {
      button,
      input,
      before: selection.before,
      after: selection.after,
      stream: null,
      recorder: null,
      chunks: [],
      timer: null,
      stopping: false,
      onComplete: typeof options.onComplete === 'function' ? options.onComplete : null,
      onCancel: typeof options.onCancel === 'function' ? options.onCancel : null,
      completed: false,
    };
    this.session = session;
    this.renderButton(button, 'starting');

    try {
      session.stream = await this.getStream();
      if (this.session !== session) {
        this._stopTracks(session);
        return false;
      }
      session.recorder = this.createRecorder(session.stream);
      session.recorder.ondataavailable = (event) => {
        if (event.data?.size) session.chunks.push(event.data);
      };
      session.recorder.onerror = (event) => {
        if (this.session !== session) return;
        this.onError('recording-failed', event);
        this._finish(session);
      };
      session.recorder.onstop = () => this._transcribe(session);
      session.recorder.start();
      session.timer = globalThis.setTimeout(() => this.stop(), this.maxDurationMs);
      this.renderButton(button, 'recording');
      input.focus();
      return true;
    } catch (error) {
      if (this.session === session) {
        this.onError(error?.name === 'NotAllowedError' ? 'not-allowed' : 'recording-failed', error);
        this._finish(session);
      }
      return false;
    }
  }

  stop() {
    const session = this.session;
    if (!session || session.stopping) return;
    session.stopping = true;
    if (session.timer !== null) globalThis.clearTimeout(session.timer);
    this.renderButton(session.button, 'stopping');
    try {
      session.recorder?.stop();
    } catch {
      this.onError('recording-failed');
      this._finish(session);
    }
  }

  async _transcribe(session) {
    if (this.session !== session) return;
    this._stopTracks(session);
    if (!session.chunks.length) {
      this.onError('empty-recording');
      this._finish(session);
      return;
    }

    this.renderButton(session.button, 'transcribing');
    const mimeType = session.recorder?.mimeType || session.chunks[0]?.type || 'audio/webm';
    try {
      const transcript = await this.transcribe(new Blob(session.chunks, { type: mimeType }));
      if (this.session !== session) return;
      if (transcript?.trim()) {
        applyTranscript(session.input, session.before, transcript.trim(), session.after);
        session.completed = true;
        if (session.onComplete) {
          try { session.onComplete(transcript.trim()); } catch {}
        }
      }
    } catch (error) {
      if (this.session === session) this.onError('transcription-failed', error);
    } finally {
      if (this.session === session) this._finish(session);
    }
  }

  _stopTracks(session) {
    for (const track of session.stream?.getTracks?.() || []) {
      try { track.stop(); } catch {}
    }
  }

  _finish(session) {
    if (this.session !== session) return;
    this.session = null;
    if (session.timer !== null) globalThis.clearTimeout(session.timer);
    this._stopTracks(session);
    if (session.recorder) {
      session.recorder.ondataavailable = null;
      session.recorder.onerror = null;
      session.recorder.onstop = null;
    }
    this.renderButton(session.button, 'idle');
    if (!session.completed && session.onCancel) {
      try { session.onCancel(); } catch {}
    }
  }
}
