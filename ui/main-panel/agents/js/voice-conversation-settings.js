'use strict';

import { _putAgentField, _debounced } from './utils.js';
import {
  _wrapNumberStepper,
  _markSaving,
  _flashSaveCheck,
} from '../../../shared/js/dom-utils.js';

const DEFAULTS = {
  continuous_context: true,
  include_durable_chat: true,
  include_agent_events: true,
  session_timeout_minutes: 15,
  max_voice_turns: 12,
  max_durable_messages: 12,
  max_agent_events: 8,
  response_length: 'brief',
  response_instructions: '',
};

function group(body, iconName, title, info) {
  const section = document.createElement('div');
  section.className = 'ac-category-group';
  const summary = document.createElement('div');
  summary.className = 'ac-category-summary';
  summary.style.cursor = 'default';
  summary.innerHTML =
    `<i data-lucide="${iconName}" class="lucide-icon" style="width:16px;height:16px;color:var(--accent);"></i>`;
  const heading = document.createElement('span');
  heading.className = 'ac-category-title';
  heading.textContent = title;
  summary.appendChild(heading);
  const content = document.createElement('div');
  content.className = 'ac-category-body';
  if (info) {
    const hint = document.createElement('div');
    hint.className = 'ac-hint';
    hint.textContent = info;
    content.appendChild(hint);
  }
  section.append(summary, content);
  body.appendChild(section);
  return content;
}

function list(body) {
  const element = document.createElement('div');
  element.className = 'ac-list ac-config-list';
  body.appendChild(element);
  return element;
}

function row(host, name, description) {
  const element = document.createElement('div');
  element.className = 'ac-ability-row';
  const label = document.createElement('span');
  label.className = 'ac-ability-label';
  const title = document.createElement('span');
  title.className = 'ac-ability-name';
  title.textContent = name;
  const detail = document.createElement('span');
  detail.className = 'ac-ability-desc';
  detail.textContent = description;
  label.append(title, detail);
  const control = document.createElement('span');
  control.className = 'ac-config-control';
  element.append(label, control);
  host.appendChild(element);
  return control;
}

export function renderVoiceConversationSettings(body, agent) {
  const config = { ...DEFAULTS, ...(agent.voice_conversation || {}) };

  const save = async (control) => {
    if (control) _markSaving(control);
    const next = { ...config };
    const ok = await _putAgentField(
      agent, { voice_conversation: next }, null, { silent: true },
    );
    if (control) _flashSaveCheck(control, ok, ok ? '' : 'Save failed');
    if (ok) agent.voice_conversation = next;
    return ok;
  };

  const addToggle = (host, name, description, key) => {
    const control = row(host, name, description);
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.className = 'conn-toggle';
    input.checked = !!config[key];
    input.addEventListener('change', async () => {
      const previous = config[key];
      config[key] = input.checked;
      if (!await save(control)) {
        config[key] = previous;
        input.checked = previous;
      }
    });
    control.appendChild(input);
  };

  const addNumber = (host, name, description, key, min, max) => {
    const control = row(host, name, description);
    const input = document.createElement('input');
    input.type = 'number';
    input.className = 'ac-input ac-input-sm ac-config-num';
    input.min = min;
    input.max = max;
    input.step = 1;
    input.value = config[key];
    control.appendChild(_wrapNumberStepper(input));
    input.addEventListener('change', async () => {
      const previous = config[key];
      const parsed = parseInt(input.value, 10);
      config[key] = Math.max(min, Math.min(max, Number.isFinite(parsed) ? parsed : previous));
      input.value = config[key];
      if (!await save(control)) {
        config[key] = previous;
        input.value = previous;
      }
    });
  };

  const context = group(body, 'messages-square', 'Conversation context',
    'Voice remains ephemeral. These settings control the bounded context sent with each new Voice message.');
  const contextList = list(context);
  addToggle(contextList, 'Remember this voice conversation',
    'Use recent Voice exchanges as rolling context so follow-up questions are connected.', 'continuous_context');
  addToggle(contextList, 'Include recent text chat',
    'Use recent durable user and assistant messages from this session.', 'include_durable_chat');
  addToggle(contextList, 'Include live agent events',
    'Use a small sanitized tail of current run and tool activity.', 'include_agent_events');
  addNumber(contextList, 'Voice turns retained',
    'Maximum user and Voice reply messages in the ephemeral rolling context.', 'max_voice_turns', 2, 40);
  addNumber(contextList, 'Text-chat messages',
    'Maximum recent durable messages available to Voice. 0 disables this context.', 'max_durable_messages', 0, 40);
  addNumber(contextList, 'Agent events',
    'Maximum recent run events available to Voice. 0 disables this context.', 'max_agent_events', 0, 30);

  const behavior = group(body, 'audio-lines', 'Response behavior',
    'The model marked “Use for Voice” in Model handles intent and answers. Safety confirmations remain fixed.');
  const behaviorList = list(behavior);
  addNumber(behaviorList, 'Session timeout',
    'Minutes of inactivity before the ephemeral Voice conversation expires.', 'session_timeout_minutes', 1, 60);

  const lengthControl = row(behaviorList, 'Response length',
    'Preferred maximum size of spoken conversational answers.');
  const length = document.createElement('select');
  length.className = 'ac-input ac-input-sm ac-config-sel';
  [['brief', 'Brief — 1–2 sentences'], ['normal', 'Normal — up to 4 sentences'], ['detailed', 'Detailed — up to 2 short paragraphs']]
    .forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      option.selected = config.response_length === value;
      length.appendChild(option);
    });
  length.addEventListener('change', async () => {
    const previous = config.response_length;
    config.response_length = length.value;
    if (!await save(lengthControl)) {
      config.response_length = previous;
      length.value = previous;
    }
  });
  lengthControl.appendChild(length);

  const field = document.createElement('div');
  field.className = 'ac-cfg-field';
  const label = document.createElement('label');
  label.className = 'ac-label';
  label.textContent = 'Response guidance';
  const indicator = document.createElement('span');
  indicator.className = 'ac-cfg-ind';
  label.appendChild(indicator);
  const textarea = document.createElement('textarea');
  textarea.className = 'ac-input';
  textarea.rows = 3;
  textarea.value = config.response_instructions;
  textarea.placeholder = 'Optional tone, vocabulary, or audience guidance for spoken answers.';
  const hint = document.createElement('div');
  hint.className = 'ac-hint';
  hint.textContent = 'Conversational answers only. This cannot change routing, confirmations, or execute work.';
  field.append(label, textarea, hint);
  behavior.appendChild(field);
  const saveGuidance = _debounced(async () => {
    config.response_instructions = textarea.value.trim();
    _markSaving(indicator);
    const ok = await save(null);
    _flashSaveCheck(indicator, ok, ok ? '' : 'Save failed');
  });
  textarea.addEventListener('input', saveGuidance);
  textarea.addEventListener('blur', () => saveGuidance.flush());
}
