import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';

class ClassList {
  constructor(el) { this.el = el; this.values = new Set(); }
  add(...names) { names.forEach(name => this.values.add(name)); this._sync(); }
  remove(...names) { names.forEach(name => this.values.delete(name)); this._sync(); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.contains(name) : !!force;
    if (on) this.values.add(name); else this.values.delete(name);
    this._sync(); return on;
  }
  _sync() { this.el._className = [...this.values].join(' '); }
}

class Element {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.listeners = {};
    this.attributes = {};
    this.style = {};
    this.dataset = {};
    this.classList = new ClassList(this);
    this._className = '';
    this._textContent = '';
    this._innerHTML = '';
    this.checked = false;
    this.disabled = false;
  }
  set className(value) {
    this._className = String(value || '');
    this.classList.values = new Set(this._className.split(/\s+/).filter(Boolean));
  }
  get className() { return this._className; }
  set textContent(value) { this._textContent = String(value ?? ''); }
  get textContent() { return this._textContent; }
  set innerHTML(value) { this._innerHTML = String(value ?? ''); }
  get innerHTML() { return this._innerHTML; }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const found = [];
    const matches = node => selector.startsWith('.')
      ? node.classList.contains(selector.slice(1))
      : node.tagName.toLowerCase() === selector.toLowerCase();
    const walk = node => {
      for (const child of node.children) {
        if (matches(child)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
}

globalThis.window = { location: { pathname: '/', protocol: 'http:', host: 'localhost' } };
globalThis.location = globalThis.window.location;
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
globalThis.document = {
  createElement: tag => new Element(tag),
  createTextNode: text => ({ textContent: String(text), parentElement: null }),
};
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: { language: 'en-US' },
});

// The browser source tree intentionally has no package.json. Load the production
// module through Node's VM API and stub unrelated card dependencies; the tested
// control function itself is evaluated directly from codex-agent.js.
const resolved = path.resolve('ui/main-panel/agents/js/codex-agent.js');
const source = await fs.readFile(resolved, 'utf8');
const codexModule = new vm.SourceTextModule(source, { identifier: pathToFileURL(resolved).href });
const noop = () => {};
const toggleRow = (list, { label, checked, onSave, onChange }) => {
  const row = new Element('div');
  const name = new Element('span'); name.className = 'ac-ability-name'; name.textContent = label;
  const input = new Element('input'); input.checked = !!checked;
  input.addEventListener('change', async () => {
    if (onChange) return onChange(input.checked);
    if (onSave) return onSave(input.checked);
  });
  row.append(name, input); list.appendChild(row);
};
const modeRow = (list, { value, onSave, onChange }) => {
  const row = new Element('div');
  const select = new Element('select'); select.value = value;
  select.addEventListener('change', async () => {
    if (onChange) return onChange(select.value);
    if (onSave) return onSave(select.value);
  });
  row.appendChild(select); list.appendChild(row);
};
const dependencyExports = {
  '../../../shared/js/state.js': { app: {} },
  '../../../shared/js/left-login.js': { authHeaders: () => ({}) },
  '../../../shared/js/config.js': { apiPath: value => value },
  './state.js': { _agents: [], _clearExpanded: noop },
  './utils.js': { _debounced: fn => Object.assign(fn, { flush: fn }), _putAgentField: async () => true },
  '../../../shared/js/dom-utils.js': { _markSaving: noop, _flashSaveCheck: noop },
  '../sessions/js/sessions-page.js': { setSessionsAgentContext: noop },
  './identity-settings.js': { renderAgentIdentitySettings: noop },
  './claude-agent.js': {
    _field: noop, _modeRow: modeRow, _deviceRow: noop, _toggleRow: toggleRow,
  },
};
await codexModule.link(specifier => {
  const values = dependencyExports[specifier];
  if (!values) throw new Error(`Unexpected browser import: ${specifier}`);
  return new vm.SyntheticModule(Object.keys(values), function initialise() {
    for (const [name, value] of Object.entries(values)) this.setExport(name, value);
  });
});
await codexModule.evaluate();
const { _mountCodexSessionControls } = codexModule.namespace;

function setup(cfg = {}, onSave = null) {
  const parent = new Element('div');
  const list = new Element('div');
  parent.appendChild(list);
  const mounted = _mountCodexSessionControls(list, parent, cfg, { onSave });
  const labels = list.querySelectorAll('.ac-ability-name');
  const toggles = list.querySelectorAll('input');
  const modes = list.querySelectorAll('select');
  return { parent, list, labels, toggles, modes, effective: mounted.effective };
}

test('Codex Config controls render legacy defaults and update independently', async () => {
  const payloads = [];
  const ui = setup({}, async patch => { payloads.push(patch); return true; });

  assert.deepEqual(ui.labels.map(label => label.textContent), [
    'Run WebAgent Closer',
  ]);
  assert.equal(ui.modes[0].value, 'native_codex', 'legacy context defaults to native Codex');
  assert.equal(ui.toggles[0].checked, true, 'legacy Closer defaults to enabled');
  assert.match(ui.effective.textContent, /Legacy mirror/);
  assert.match(ui.effective.textContent, /Closer enabled/);

  ui.modes[0].value = 'webagent_wrapper';
  await ui.modes[0].listeners.change[0]();
  assert.deepEqual(payloads[0], { context_mode: 'webagent_wrapper' });
  assert.match(ui.effective.textContent, /WebAgent owns context/);
  assert.match(ui.effective.textContent, /fresh ephemeral Codex run per message/);

  ui.toggles[0].checked = false;
  await ui.toggles[0].listeners.change[0]();
  assert.deepEqual(payloads[1], { closer_enabled: false });
  assert.match(ui.effective.textContent, /WebAgent owns context/);
  assert.match(ui.effective.textContent, /Closer disabled/);
});

test('Codex Config serializes concurrent toggle saves and preserves both states', async () => {
  let releaseFirst;
  const firstSave = new Promise(resolve => { releaseFirst = resolve; });
  const payloads = [];
  let activeSaves = 0;
  let maxActiveSaves = 0;
  const cfg = {};
  const ui = setup(cfg, async patch => {
    payloads.push(patch);
    activeSaves += 1;
    maxActiveSaves = Math.max(maxActiveSaves, activeSaves);
    if (payloads.length === 1) await firstSave;
    activeSaves -= 1;
    return true;
  });

  ui.modes[0].value = 'webagent_wrapper';
  const wrapperSave = ui.modes[0].listeners.change[0]();
  await Promise.resolve();
  assert.deepEqual(payloads, [{ context_mode: 'webagent_wrapper' }]);

  ui.toggles[0].checked = false;
  const closerSave = ui.toggles[0].listeners.change[0]();
  await Promise.resolve();
  assert.equal(payloads.length, 1, 'the Closer PUT waits for the wrapper PUT');

  releaseFirst();
  await Promise.all([wrapperSave, closerSave]);

  assert.deepEqual(payloads, [
    { context_mode: 'webagent_wrapper' },
    { closer_enabled: false },
  ]);
  assert.equal(maxActiveSaves, 1, 'only one metadata merge is in flight');
  assert.equal(cfg.context_mode, 'webagent_wrapper');
  assert.equal(cfg.closer_enabled, false);
  assert.match(ui.effective.textContent, /WebAgent owns context/);
  assert.match(ui.effective.textContent, /Closer disabled/);
});
