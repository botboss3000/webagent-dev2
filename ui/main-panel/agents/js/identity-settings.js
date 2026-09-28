'use strict';

/** Shared Identity section used by every persisted agent's Config tab. */

import { icon } from '../../../shared/js/icons.js';
import { openIconPicker } from '../../../shared/js/icon-picker.js';
import { _markSaving, _flashSaveCheck } from '../../../shared/js/dom-utils.js';
import { _debounced, _putAgentField, _renderAgentIcon } from './utils.js';
import { isAnonGuest, authHeaders } from '../../../shared/js/left-login.js';
import { app } from '../../../shared/js/state.js';

function _field(host, agent, { label, field, value, multiline = false, placeholder = '' }) {
  const wrap = document.createElement('div');
  wrap.className = 'ac-cfg-field';
  const labelEl = document.createElement('label');
  labelEl.className = 'ac-label';
  labelEl.textContent = label;
  const indicator = document.createElement('span');
  indicator.className = 'ac-cfg-ind';
  labelEl.appendChild(indicator);
  const input = document.createElement(multiline ? 'textarea' : 'input');
  if (!multiline) input.type = 'text';
  else input.rows = 3;
  input.className = 'ac-input';
  input.dataset.field = field;
  input.value = value || '';
  input.placeholder = placeholder;
  input.readOnly = agent.source !== 'custom';
  wrap.appendChild(labelEl);
  wrap.appendChild(input);
  host.appendChild(wrap);

  if (!input.readOnly) {
    const save = _debounced(async () => {
      let next = input.value;
      if (field === 'name') {
        next = next.trim();
        if (!next) {
          if (agent.is_mock) {
            await _putAgentField(agent, { name: '' }, null, { silent: true });
            return;
          }
          input.value = agent.name || agent.id;
          _flashSaveCheck(indicator, false, 'Name cannot be blank');
          return;
        }
      }
      _markSaving(indicator);
      const ok = await _putAgentField(agent, { [field]: next }, null, { silent: true });
      _flashSaveCheck(indicator, ok, ok ? '' : 'Save failed');
    });
    input.addEventListener('input', save);
    input.addEventListener('blur', () => save.flush());
  }
}

function _renderMessageVisibilitySettings(content, agent) {
  const descriptor = (agent.gates?.types || []).find((type) => type.id === 'message_visibility');
  if (!descriptor) return;
  const persisted = (agent.gates?.instances || []).find((row) => row.type === 'message_visibility');
  const editable = agent.source === 'custom' || agent.id === 'shared_default';
  const settings = {
    enabled: persisted ? persisted.enabled !== false : descriptor.default_enabled !== false,
    guidance: String(persisted?.message || ''),
  };
  const section = document.createElement('section');
  section.className = 'agent-message-visibility-settings agent-closing-message-settings';
  section.innerHTML = `
    <div class="agent-closing-message-settings-copy">
      <strong>Message visibility</strong>
      <p>Choose whether working-agent messages stay in activity groups or can appear as ordinary assistant bubbles while work continues.</p>
    </div>`;
  const list = document.createElement('div');
  list.className = 'ac-list agent-closing-message-list';
  section.appendChild(list);
  const persist = async (indicator) => _putAgentField(agent, {
    gates: {
      schema_version: 1,
      gates: [{
        type: 'message_visibility', enabled: settings.enabled,
        message: settings.guidance,
      }],
    },
  }, indicator, { silent: true });

  const row = document.createElement('div');
  row.className = 'ac-ability-row agent-closing-message-row';
  const glyph = document.createElement('span'); glyph.className = 'ac-ability-icon'; glyph.innerHTML = icon('message-square-text', { size: '16px' });
  const label = document.createElement('div'); label.className = 'ac-ability-label';
  const name = document.createElement('div'); name.className = 'ac-ability-name'; name.textContent = 'Route working messages';
  const desc = document.createElement('div'); desc.className = 'ac-ability-desc'; desc.textContent = 'Enabled by default. The agent chooses internal activity updates or plain standalone messages without pausing the run.';
  label.append(name, desc);
  const toggleLabel = document.createElement('label'); toggleLabel.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
  const toggle = document.createElement('input'); toggle.type = 'checkbox'; toggle.className = 'conn-toggle'; toggle.checked = settings.enabled; toggle.disabled = !editable;
  toggle.setAttribute('aria-label', 'Route working messages');
  const track = document.createElement('span'); track.className = 'conn-toggle-track'; toggleLabel.append(toggle, track);
  const saveIndicator = document.createElement('span'); saveIndicator.className = 'agent-closing-message-save-status';
  row.append(glyph, label, toggleLabel, saveIndicator);
  const routeCard = document.createElement('div');
  routeCard.className = 'ac-row agent-message-visibility-route';
  routeCard.appendChild(row);
  const routeBody = document.createElement('div');
  routeBody.className = 'ac-ability-body';
  routeCard.appendChild(routeBody);
  list.appendChild(routeCard);
  row.addEventListener('click', (event) => {
    if (!event.target.closest('.conn-toggle-wrap')) routeCard.classList.toggle('expanded');
  });
  toggle.addEventListener('change', async () => {
    const previous = settings.enabled; settings.enabled = toggle.checked; toggle.disabled = true;
    const ok = await persist(saveIndicator); toggle.disabled = !editable;
    if (!ok) { settings.enabled = previous; toggle.checked = previous; }
  });

  const floor = document.createElement('label');
  floor.className = 'agent-closing-message-guidance';
  floor.innerHTML = '<span>Fixed protocol</span>';
  const floorText = document.createElement('textarea');
  floorText.className = 'ac-input'; floorText.rows = 5; floorText.readOnly = true;
  floorText.value = descriptor.prompt_instruction || '';
  floor.appendChild(floorText);

  const guidance = document.createElement('label');
  guidance.className = 'agent-closing-message-guidance';
  guidance.innerHTML = '<span>Additional agent guidance</span>';
  const guidanceInput = document.createElement('textarea');
  guidanceInput.className = 'ac-input'; guidanceInput.rows = 2; guidanceInput.value = settings.guidance;
  guidanceInput.placeholder = 'Optional guidance for when this agent should use a standalone working message.';
  guidanceInput.disabled = !editable;
  guidance.appendChild(guidanceInput);
  const guidanceSave = document.createElement('button');
  guidanceSave.type = 'button'; guidanceSave.className = 'ac-btn ac-btn-ghost'; guidanceSave.textContent = 'Save';
  guidanceSave.disabled = !editable;
  guidanceSave.addEventListener('click', async () => {
    const previous = settings.guidance; settings.guidance = guidanceInput.value;
    guidanceInput.disabled = true; guidanceSave.disabled = true;
    const ok = await persist(null);
    guidanceInput.disabled = !editable; guidanceSave.disabled = !editable;
    if (!ok) { settings.guidance = previous; guidanceInput.value = previous; return; }
    guidanceSave.textContent = 'Saved';
    setTimeout(() => { guidanceSave.textContent = 'Save'; }, 1500);
  });
  routeBody.append(floor, guidance, guidanceSave);
  content.appendChild(section);
}

function _renderClosingMessageSettings(content, agent) {
  const descriptor = (agent.gates?.types || []).find((type) => type.id === 'closing_message');
  if (!descriptor) return;
  const persisted = (agent.gates?.instances || []).find((row) => row.type === 'closing_message');
  const rawParams = persisted?.params && typeof persisted.params === 'object' ? persisted.params : {};
  const legacyMasterOff = !!persisted && persisted.enabled !== true;
  const defaults = descriptor.params || {};
  const toggleValue = (key) => legacyMasterOff
    ? false
    : (rawParams[key] ?? defaults[key]?.default ?? false) === true;
  const settings = {
    require_flag: toggleValue('require_flag'),
    require_notification: toggleValue('require_notification'),
    output_closer_enabled: toggleValue('output_closer_enabled'),
    notification: String(rawParams.notification || ''),
    format: String(rawParams.format || ''),
    max_tokens: Number(rawParams.max_tokens ?? defaults.max_tokens?.default ?? 0) || 0,
    format_enforcement_min_tokens: Number(
      rawParams.format_enforcement_min_tokens ?? defaults.format_enforcement_min_tokens?.default ?? 0,
    ) || 0,
    rejection_message: String(rawParams.rejection_message || defaults.rejection_message?.default || ''),
  };
  const editable = agent.source === 'custom' || agent.id === 'shared_default';
  const section = document.createElement('section');
  section.className = 'agent-closing-message-settings';
  section.innerHTML = `
    <div class="agent-closing-message-settings-copy">
      <strong>Final response</strong>
      <p>Choose the completion requirements independently. Active requirements are combined into one final instruction and one validation pass.</p>
    </div>`;
  const list = document.createElement('div');
  list.className = 'ac-list agent-closing-message-list';
  section.appendChild(list);

  const persist = async (indicator) => _putAgentField(agent, {
    gates: {
      schema_version: 1,
      gates: [{ type: 'closing_message', enabled: true, params: { ...settings } }],
    },
  }, indicator, { silent: true });
  const makeToggle = (key, iconName, title, description) => {
    const row = document.createElement('div');
    row.className = 'ac-ability-row agent-closing-message-row';
    const glyph = document.createElement('span'); glyph.className = 'ac-ability-icon'; glyph.innerHTML = icon(iconName, { size: '16px' });
    const label = document.createElement('div'); label.className = 'ac-ability-label';
    const name = document.createElement('div'); name.className = 'ac-ability-name'; name.textContent = title;
    const desc = document.createElement('div'); desc.className = 'ac-ability-desc'; desc.textContent = description;
    label.append(name, desc);
    const toggleLabel = document.createElement('label'); toggleLabel.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
    const toggle = document.createElement('input'); toggle.type = 'checkbox'; toggle.className = 'conn-toggle'; toggle.checked = settings[key]; toggle.disabled = !editable;
    toggle.setAttribute('aria-label', title);
    const track = document.createElement('span'); track.className = 'conn-toggle-track'; toggleLabel.append(toggle, track);
    const saveIndicator = document.createElement('span'); saveIndicator.className = 'agent-closing-message-save-status';
    row.append(glyph, label, toggleLabel, saveIndicator);
    toggle.addEventListener('change', async () => {
      const previous = settings[key]; settings[key] = toggle.checked; toggle.disabled = true;
      const ok = await persist(saveIndicator); toggle.disabled = !editable;
      if (!ok) { settings[key] = previous; toggle.checked = previous; }
      section.dispatchEvent(new CustomEvent('closing-message-change'));
    });
    return { row, toggle };
  };

  list.appendChild(makeToggle(
    'require_flag', 'flag', 'Require completion flag',
    'Repair the private final-status flag without rewriting the natural terminal message; validate it against the app-wide flag vocabulary.',
  ).row);
  const notification = makeToggle(
    'require_notification', 'bell', 'Require notification / next step',
    'Repair a short private notification or next-step summary without rewriting the natural terminal message.',
  );
  const notificationCard = document.createElement('div');
  notificationCard.className = 'ac-row agent-closing-message-notification';
  notificationCard.appendChild(notification.row);
  const notificationBody = document.createElement('div');
  notificationBody.className = 'ac-ability-body';
  notificationCard.appendChild(notificationBody);
  list.appendChild(notificationCard);
  notification.row.addEventListener('click', (event) => {
    if (!event.target.closest('.conn-toggle-wrap')) notificationCard.classList.toggle('expanded');
  });

  const output = makeToggle(
    'output_closer_enabled', 'message-square-text', 'Enforce output message',
    'The only control allowed to revise visible terminal prose: enable final-message guidance, limits, format rejection, and rewrite behavior.',
  );
  const outputCard = document.createElement('div');
  outputCard.className = 'ac-row agent-closing-message-output';
  outputCard.appendChild(output.row);
  const outputBody = document.createElement('div'); outputBody.className = 'ac-ability-body';
  const outputFields = document.createElement('div'); outputFields.className = 'agent-closing-message-fields';
  const field = (label, key, type, hint) => {
    const wrap = document.createElement('label'); wrap.className = 'agent-closing-message-field';
    const name = document.createElement('span'); name.textContent = label;
    const input = document.createElement(type === 'textarea' ? 'textarea' : 'input');
    input.className = 'ac-input'; input.dataset.closingMessageKey = key;
    if (type === 'number') { input.type = 'number'; input.min = '0'; input.max = '100000'; input.step = '1'; input.value = String(settings[key]); }
    else { input.rows = 3; input.value = settings[key]; }
    input.placeholder = hint; input.disabled = !editable || !settings.output_closer_enabled;
    wrap.append(name, input); return wrap;
  };
  outputFields.append(
    field('Maximum output tokens', 'max_tokens', 'number', '0 disables the limit'),
    field('Enforce bullet format above tokens', 'format_enforcement_min_tokens', 'number', '0 disables format enforcement'),
    field('Rejection / rewrite guidance', 'rejection_message', 'textarea', 'Instructions for an over-limit or rejected output message'),
    field('Output format guidance', 'format', 'textarea', 'Optional final-message guidance'),
  );
  const outputSave = document.createElement('button');
  outputSave.type = 'button'; outputSave.className = 'ac-btn ac-btn-ghost'; outputSave.textContent = 'Save';
  outputSave.disabled = !editable || !settings.output_closer_enabled;
  outputSave.addEventListener('click', async () => {
    const previous = {
      max_tokens: settings.max_tokens,
      format_enforcement_min_tokens: settings.format_enforcement_min_tokens,
      rejection_message: settings.rejection_message,
      format: settings.format,
    };
    outputFields.querySelectorAll('[data-closing-message-key]').forEach((input) => {
      const key = input.dataset.closingMessageKey;
      settings[key] = input.type === 'number'
        ? Math.max(0, Math.min(100000, Number(input.value) || 0))
        : input.value;
      input.disabled = true;
    });
    outputSave.disabled = true;
    const ok = await persist(null);
    outputFields.querySelectorAll('[data-closing-message-key]').forEach((input) => {
      input.disabled = !editable || !settings.output_closer_enabled;
    });
    outputSave.disabled = !editable || !settings.output_closer_enabled;
    if (!ok) {
      Object.assign(settings, previous);
      outputFields.querySelectorAll('[data-closing-message-key]').forEach((input) => {
        input.value = String(settings[input.dataset.closingMessageKey]);
      });
      return;
    }
    outputSave.textContent = 'Saved';
    setTimeout(() => { outputSave.textContent = 'Save'; }, 1500);
  });
  outputBody.append(outputFields, outputSave); outputCard.appendChild(outputBody); list.appendChild(outputCard);
  output.row.addEventListener('click', (event) => {
    if (!event.target.closest('.conn-toggle-wrap')) outputCard.classList.toggle('expanded');
  });
  section.addEventListener('closing-message-change', () => {
    const disabled = !editable || !settings.output_closer_enabled;
    outputFields.querySelectorAll('input, textarea').forEach((input) => { input.disabled = disabled; });
    outputSave.disabled = disabled;
  });

  const notificationGuidance = document.createElement('label');
  notificationGuidance.className = 'agent-closing-message-guidance';
  notificationGuidance.innerHTML = '<span>Notification / next-step guidance</span>';
  const guidanceInput = document.createElement('textarea');
  guidanceInput.className = 'ac-input'; guidanceInput.rows = 2; guidanceInput.value = settings.notification;
  guidanceInput.placeholder = 'Optional guidance for the notification or next-step summary.';
  guidanceInput.disabled = !editable || !settings.require_notification;
  notificationGuidance.appendChild(guidanceInput);
  const notificationSave = document.createElement('button');
  notificationSave.type = 'button'; notificationSave.className = 'ac-btn ac-btn-ghost'; notificationSave.textContent = 'Save';
  notificationSave.disabled = !editable || !settings.require_notification;
  notificationSave.addEventListener('click', async () => {
    const previous = settings.notification; settings.notification = guidanceInput.value;
    guidanceInput.disabled = true; notificationSave.disabled = true;
    const ok = await persist(null);
    guidanceInput.disabled = !editable || !settings.require_notification;
    notificationSave.disabled = !editable || !settings.require_notification;
    if (!ok) { settings.notification = previous; guidanceInput.value = previous; return; }
    notificationSave.textContent = 'Saved';
    setTimeout(() => { notificationSave.textContent = 'Save'; }, 1500);
  });
  section.addEventListener('closing-message-change', () => {
    const disabled = !editable || !settings.require_notification;
    guidanceInput.disabled = disabled; notificationSave.disabled = disabled;
  });
  notificationBody.append(notificationGuidance, notificationSave);
  content.appendChild(section);
}

export function renderAgentIdentitySettings(body, agent, {
  embedded = body.classList.contains('agent-config-topic-content'),
} = {}) {
  const anonymousDraft = !!agent.is_mock && isAnonGuest();
  const group = document.createElement('div');
  group.className = embedded
    ? 'agent-identity-settings agent-identity-settings--embedded'
    : 'ac-category-group agent-identity-settings';
  if (!embedded) group.innerHTML = `
    <div class="ac-category-summary" style="cursor:default">
      ${icon('badge', { size: '16px' })}
      <span class="ac-category-title">Identity and design</span>
    </div>`;
  const content = document.createElement('div');
  content.className = 'ac-category-body';
  group.appendChild(content);
  body.appendChild(group);

  const iconRow = document.createElement('div');
  iconRow.className = 'agent-identity-icon-row';
  const iconButton = document.createElement('button');
  iconButton.type = 'button';
  iconButton.className = 'agent-identity-icon-button';
  iconButton.innerHTML = _renderAgentIcon(agent, '26px');
  iconButton.disabled = agent.source !== 'custom';
  iconButton.title = iconButton.disabled ? 'This agent icon is managed by the system' : 'Change agent icon';
  iconButton.setAttribute('aria-label', iconButton.title);
  const iconText = document.createElement('span');
  iconText.innerHTML = '<strong>Agent icon</strong><small>Shown in the agent carousel and chats.</small>';
  iconRow.appendChild(iconText);
  iconRow.appendChild(iconButton);
  content.appendChild(iconRow);

  if (!iconButton.disabled) {
    iconButton.addEventListener('click', async () => {
      const chosen = await openIconPicker({ current: agent.icon || '', title: 'Choose an icon' });
      if (!chosen) return;
      const previous = agent.icon || '';
      agent.icon = chosen;
      iconButton.innerHTML = _renderAgentIcon(agent, '26px');
      const ok = await _putAgentField(agent, { icon: chosen }, null, { silent: true });
      if (!ok) {
        agent.icon = previous;
        iconButton.innerHTML = _renderAgentIcon(agent, '26px');
      }
    });
  }

  _field(content, agent, {
    label: 'Name', field: 'name', value: agent.is_mock ? (agent.name || '') : (agent.name || agent.id),
    placeholder: anonymousDraft
      ? 'Register an account to name and create an agent…'
      : 'Name this agent…',
  });
  _field(content, agent, {
    label: 'Description', field: 'description', value: agent.description || '', multiline: true,
    placeholder: 'Describe what this agent is for…',
  });
  _renderMessageVisibilitySettings(content, agent);
  _renderClosingMessageSettings(content, agent);

  const design = document.createElement('section');
  design.className = 'agent-design-settings';
  const designCopy = document.createElement('div');
  designCopy.className = 'agent-design-settings-copy';
  designCopy.innerHTML = `<strong>Chat design</strong><span class="agent-config-info-tip" data-tooltip="Chat design parameters can be changed by talking to this agent when it has Agent Management enabled. Changes are limited to administrators of this agent." tabindex="0" aria-label="Chat design parameters can be changed by talking to this agent when it has Agent Management enabled. Changes are limited to administrators of this agent.">${icon('info', { size: '14px' })}</span>`;
  const designForm = document.createElement('form');
  designForm.className = 'agent-design-settings-form';
  const designInput = document.createElement('textarea');
  designInput.className = 'ac-input agent-design-settings-input';
  designInput.rows = 2;
  designInput.placeholder = 'Describe the chat design change you want…';
  designInput.setAttribute('aria-label', 'Chat design request');
  const designSubmit = document.createElement('button');
  designSubmit.type = 'submit';
  designSubmit.className = 'ac-btn ac-btn-primary agent-design-settings-submit';
  designSubmit.innerHTML = `${icon('send', { size: '14px' })} Start design chat`;
  designSubmit.title = 'Start a new session and send this chat-design request';
  designForm.appendChild(designInput);
  designForm.appendChild(designSubmit);
  designForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const request = designInput.value.trim();
    if (!request) {
      designInput.focus();
      return;
    }
    const prompt = [
      `Agent configuration task for the specific agent “${agent.name || agent.id}” (ID: ${agent.id}).`,
      'If you have Agent Management permission and the caller is an administrator of this agent, update only this agent\'s metadata.chat_ui database field using the supported agent-update mechanism.',
      'Do not edit the global data/config/chat_ui.json file and do not modify any other agent.',
      '',
      `Requested design change: ${request}`,
    ].join('\n');
    const launchId = (crypto.randomUUID
      ? crypto.randomUUID()
      : Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join(''));
    sessionStorage.setItem(`agent-design-launch:${launchId}`, JSON.stringify({
      version: 1,
      agentId: agent.id,
      prompt,
      mode: 'auto',
      createdAt: Date.now(),
    }));
    const target = new URL(window.location.href);
    target.search = '';
    target.hash = '';
    target.searchParams.set('agent', agent.id);
    target.searchParams.set('new', '1');
    target.searchParams.set('design_launch', launchId);
    window.location.assign(target.href);
  });
  design.appendChild(designCopy);
  design.appendChild(designForm);
  content.appendChild(design);

  // Member access is intentionally agent-scoped: an app account proves identity,
  // while these profiles control the access granted inside this particular agent.
  if (!agent.is_mock && agent.source === 'custom') {
    const access = document.createElement('section');
    access.className = 'agent-member-access';
    access.innerHTML = `
      <div class="agent-member-access-copy">
        <strong>Member access</strong>
        <p>Choose how people join this agent, create agent-specific member levels, and assign existing members. App roles are never copied into this agent automatically.</p>
      </div>
      <div class="agent-member-access-status" role="status">Loading access configuration…</div>
    `;
    content.appendChild(access);

    let status = access.querySelector('.agent-member-access-status');
    const endpoint = (path) => `/api/v1/agents/${encodeURIComponent(agent.id)}${path}`;
    const request = async (path, options = {}) => {
      const response = await fetch(endpoint(path), {
        ...options,
        headers: { ...authHeaders(), ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) },
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || 'Unable to update member access.');
      return data;
    };
    const labelFor = (profile) => profile?.name || profile?.slug || 'Member';
    const render = ({ profiles = [], members = [], auth_policy: policy = {} }) => {
      const memberProfile = profiles.find((profile) => profile.slug === 'member')?.policy || {
        abilities: ['*'], tools: ['*'], features: ['*'], limits: {}, funding: { mode: 'member' }, chat_ui: {}, prompt_overlay: '',
      };
      const assignable = profiles.filter((profile) => profile.slug !== 'agent-administrator');
      access.replaceChildren();
      const copy = document.createElement('div');
      copy.className = 'agent-member-access-copy';
      copy.innerHTML = '<strong>Member access</strong><p>App accounts prove identity; this agent decides what each person can do. New levels inherit the Member permission set, subject to this agent’s enabled abilities and tools.</p>';
      const registration = document.createElement('fieldset');
      registration.className = 'agent-member-access-registration';
      const legend = document.createElement('legend'); legend.textContent = 'Member registration'; registration.appendChild(legend);
      const registrationChoices = [['disabled', 'Disabled'], ['invite', 'Invite only'], ['open', 'Open registration']];
      const currentMode = policy.local_signup_mode || 'open';
      registrationChoices.forEach(([value, text]) => {
        const label = document.createElement('label'); label.className = 'agent-member-access-radio';
        const input = document.createElement('input'); input.type = 'radio'; input.name = `agent-registration-${agent.id}`; input.value = value; input.checked = currentMode === value;
        label.append(input, document.createTextNode(text)); registration.appendChild(label);
        input.addEventListener('change', async () => {
          if (!input.checked) return;
          try {
            await request('/auth/policy', { method: 'PUT', body: JSON.stringify({ app_login_enabled: !!policy.app_login_enabled, local_signup_mode: value, transcript_review: !!policy.transcript_review }) });
            await load();
          } catch (error) { status.textContent = error.message; }
        });
      });
      access.append(copy, registration);

      {
        const levels = document.createElement('div'); levels.className = 'agent-member-access-levels';
        const visitor = document.createElement('div'); visitor.className = 'agent-member-access-level'; visitor.textContent = 'Visitor';
        levels.appendChild(visitor);

        const anonymousAccess = document.createElement('input'); anonymousAccess.type = 'checkbox';
        anonymousAccess.checked = (agent.user_mode || 'anonymous') === 'anonymous';
        const anonymousAccessLabel = document.createElement('label'); anonymousAccessLabel.className = 'agent-member-access-toggle';
        anonymousAccessLabel.append(anonymousAccess, document.createTextNode('Allow anonymous users to access this agent'));
        anonymousAccess.addEventListener('change', async () => {
          const requested = anonymousAccess.checked;
          anonymousAccess.disabled = true;
          const ok = await _putAgentField(agent, { user_mode: requested ? 'anonymous' : 'register' }, null, { silent: true });
          anonymousAccess.disabled = false;
          if (ok) {
            agent.user_mode = requested ? 'anonymous' : 'register';
            status.textContent = requested ? 'Anonymous visitor access saved.' : 'Anonymous visitor access disabled.';
          } else {
            anonymousAccess.checked = !requested;
            status.textContent = 'Unable to update anonymous visitor access.';
          }
        });
        levels.appendChild(anonymousAccessLabel);

        const member = document.createElement('div'); member.className = 'agent-member-access-level'; member.textContent = 'Member';
        levels.appendChild(member);

        const appLogin = document.createElement('input'); appLogin.type = 'checkbox'; appLogin.checked = !!policy.app_login_enabled;
        const appLoginLabel = document.createElement('label'); appLoginLabel.className = 'agent-member-access-toggle'; appLoginLabel.append(appLogin, document.createTextNode('Accept app registered users as members'));
        appLogin.addEventListener('change', async () => {
          try {
            await request('/auth/policy', { method: 'PUT', body: JSON.stringify({ app_login_enabled: appLogin.checked, local_signup_mode: currentMode, transcript_review: !!policy.transcript_review }) });
            status.textContent = 'App account access saved.';
          } catch (error) { appLogin.checked = !appLogin.checked; status.textContent = error.message; }
        });
        levels.appendChild(appLoginLabel);

        profiles.filter((profile) => !['visitor', 'member', 'agent-administrator'].includes(profile.slug)).forEach((profile) => {
          const level = document.createElement('div'); level.className = 'agent-member-access-level agent-member-access-level--custom'; level.textContent = labelFor(profile); levels.appendChild(level);
        });
        const levelForm = document.createElement('form'); levelForm.className = 'agent-member-access-new-level';
        const levelName = document.createElement('input'); levelName.className = 'ac-input'; levelName.placeholder = 'New level name, e.g. Client'; levelName.required = true;
        const addLevel = document.createElement('button'); addLevel.type = 'submit'; addLevel.className = 'ac-btn ac-btn-secondary'; addLevel.textContent = 'Add new member level';
        levelForm.append(levelName, addLevel); levels.appendChild(levelForm);
        levelForm.addEventListener('submit', async (event) => {
          event.preventDefault(); const name = levelName.value.trim(); const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
          if (!slug) { status.textContent = 'Enter a level name containing letters or numbers.'; return; }
          addLevel.disabled = true;
          try { await request(`/profiles/${encodeURIComponent(slug)}`, { method: 'PUT', body: JSON.stringify({ slug, name, description: `Custom ${name} member level`, policy: memberProfile }) }); await load(); }
          catch (error) { status.textContent = error.message; } finally { addLevel.disabled = false; }
        });

        const agentAdmin = document.createElement('div'); agentAdmin.className = 'agent-member-access-level agent-member-access-level--admin'; agentAdmin.textContent = 'Agent administrator';
        const transcriptReview = document.createElement('input'); transcriptReview.type = 'checkbox'; transcriptReview.checked = !!policy.transcript_review;
        const transcriptReviewLabel = document.createElement('label'); transcriptReviewLabel.className = 'agent-member-access-toggle'; transcriptReviewLabel.append(transcriptReview, document.createTextNode('Allow agent administrators to review conversations'));
        transcriptReview.addEventListener('change', async () => {
          try {
            await request('/auth/policy', { method: 'PUT', body: JSON.stringify({ app_login_enabled: !!policy.app_login_enabled, local_signup_mode: currentMode, transcript_review: transcriptReview.checked }) });
            status.textContent = 'Conversation review setting saved.';
          } catch (error) { transcriptReview.checked = !transcriptReview.checked; status.textContent = error.message; }
        });
        levels.append(agentAdmin, transcriptReviewLabel);
        access.appendChild(levels);
      }
      status = document.createElement('div'); status.className = 'agent-member-access-status'; status.setAttribute('role', 'status'); status.textContent = currentMode === 'disabled' ? 'Registration is disabled.' : 'Access configuration ready.';
      access.appendChild(status);
    };
    const load = async () => {
      try {
        const [profileData, memberData] = await Promise.all([request('/profiles'), request('/profile-members')]);
        render({ ...profileData, members: memberData.members || [] });
      } catch (error) { status.textContent = error.message; }
    };
    load();
  }
}
