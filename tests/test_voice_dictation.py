"""Behavioral tests for the browser voice-dictation controller."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "ui" / "shared" / "js" / "voice-dictation.js"


def test_voice_dictation_reconciles_revised_results_and_session_races() -> None:
    script = r"""
const fs = require('node:fs');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');

(async () => {
  const {
    RecordedAudioDictationController,
    VoiceDictationController,
    composeDictationValue,
    transcriptFromResults,
  } = await import(moduleUrl);

  const result = (text, isFinal = false) =>
    Object.assign([{ transcript: text }], { isFinal });
  const event = (...items) => ({ results: items });

  assert.equal(
    transcriptFromResults([result('hello'), result(', world', true)]),
    'hello, world',
  );
  assert.equal(
    transcriptFromResults([
      result('test', true),
      result('test 1', true),
      result('test 1 2', true),
      result('test 1 2 3', true),
    ]),
    'test 1 2 3',
  );
  assert.equal(
    composeDictationValue('Ask ', 'how are you', '?'),
    'Ask how are you?',
  );

  class Recognition {
    start() { this.started = true; }
    stop() { this.stopped = true; }
  }

  const recognitions = [];
  const buttonStates = [];
  const completed = [];
  const controller = new VoiceDictationController({
    createRecognition: () => {
      const recognition = new Recognition();
      recognitions.push(recognition);
      return recognition;
    },
    language: () => 'en-US',
    renderButton: (_button, state) => buttonStates.push(state),
  });

  const input = {
    value: 'Say: please.',
    selectionStart: 5,
    selectionEnd: 5,
    inputEvents: 0,
    focus() { this.focused = true; },
    setSelectionRange(start, end) {
      this.selectionStart = start;
      this.selectionEnd = end;
    },
    dispatchEvent() { this.inputEvents += 1; },
  };
  const button = {};

  assert.equal(controller.start(button, input, {
    onComplete: (text) => completed.push(text),
  }), true);
  const first = recognitions[0];
  assert.equal(first.started, true);

  // Web Speech revises interim result index 0. The previous implementation
  // skipped the revision and later skipped that same result becoming final.
  first.onresult(event(result('hello')));
  assert.equal(input.value, 'Say: hello please.');
  first.onresult(event(result('hello there')));
  assert.equal(input.value, 'Say: hello there please.');
  first.onresult(event(result('hello there', true), result('world')));
  assert.equal(input.value, 'Say: hello there world please.');

  controller.stop();
  assert.equal(first.stopped, true);
  assert.equal(controller.active, true);

  // stop() still accepts the engine's final result before onend.
  first.onresult(event(result('hello there', true), result('world today', true)));
  assert.equal(input.value, 'Say: hello there world today please.');
  const staleResultHandler = first.onresult;
  first.onend();
  assert.equal(controller.active, false);
  assert.deepEqual(completed, ['hello there world today']);

  const secondInput = {
    value: '',
    selectionStart: 0,
    selectionEnd: 0,
    focus() {},
    setSelectionRange() {},
    dispatchEvent() {},
  };
  controller.start(button, secondInput);
  staleResultHandler(event(result('stale words', true)));
  assert.equal(secondInput.value, '');

  // An engine ending on silence must start a new recognition cycle without
  // ending the logical dictation session or losing the accumulated transcript.
  const second = recognitions[1];
  second.onresult(event(result('before pause', true)));
  second.onend();
  assert.equal(controller.active, true);
  assert.equal(secondInput.value, 'before pause');
  await new Promise(resolve => setTimeout(resolve, 180));
  const third = recognitions[2];
  assert.equal(third.started, true);
  third.onresult(event(result('after pause', true)));
  assert.equal(secondInput.value, 'before pause after pause');
  controller.stop();
  third.onend();
  assert.equal(controller.active, false);

  assert.deepEqual(
    buttonStates,
    ['recording', 'stopping', 'idle', 'recording', 'stopping', 'idle'],
  );
  assert.equal(input.inputEvents, 4);

  const fallbackStates = [];
  const fallbackCompleted = [];
  const stoppedTracks = [];
  let fallbackRecorder;
  const fallback = new RecordedAudioDictationController({
    getStream: async () => ({
      getTracks: () => [{ stop: () => stoppedTracks.push(true) }],
    }),
    createRecorder: () => {
      fallbackRecorder = {
        mimeType: 'audio/webm',
        start() { this.started = true; },
        stop() {
          this.ondataavailable({ data: new Blob(['audio'], { type: this.mimeType }) });
          this.onstop();
        },
      };
      return fallbackRecorder;
    },
    transcribe: async () => 'fallback words',
    renderButton: (_button, state) => fallbackStates.push(state),
  });
  const fallbackInput = {
    value: 'Before after',
    selectionStart: 7,
    selectionEnd: 7,
    focus() {},
    setSelectionRange() {},
    dispatchEvent() {},
  };
  assert.equal(await fallback.start(button, fallbackInput, {
    onComplete: (text) => fallbackCompleted.push(text),
  }), true);
  assert.equal(fallbackRecorder.started, true);
  fallback.stop();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(fallbackInput.value, 'Before fallback words after');
  assert.equal(fallback.active, false);
  assert.deepEqual(fallbackCompleted, ['fallback words']);
  assert.deepEqual(
    fallbackStates,
    ['starting', 'recording', 'stopping', 'transcribing', 'idle'],
  );
  assert.equal(stoppedTracks.length > 0, true);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", script, str(MODULE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
