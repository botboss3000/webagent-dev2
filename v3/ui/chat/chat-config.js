// V3 reads the data/config chat_ui.json schema. Unknown controls are
// skipped until their v3 features exist; the controls below are live in v3.
const byId = id => document.getElementById(id);
let documentConfig = null;
let headerNodes = null;
let currentSurface = '';
let currentFooterMode = '';
let listenersBound = false;

export function mergeProfile(base, override) {
  const merged = { ...(base || {}) };
  for (const [key, value] of Object.entries(override || {})) {
    merged[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? mergeProfile(base?.[key], value) : value;
  }
  return merged;
}

function surfaceName() {
  return window.matchMedia('(max-width:800px)').matches ? 'chat_mobile' : 'chat_desktop';
}

export function currentChatProfile() {
  if (!documentConfig?.chat_common) return null;
  return mergeProfile(documentConfig.chat_common, documentConfig[surfaceName()]);
}

function sizeControl(node, config) {
  if (!node || !config) return;
  const size = config.container_size || config.box_size;
  if (size && size !== 'auto') {
    node.style.height = size;
    if (config.square !== false) node.style.width = size;
  }
  if (config.min_width) node.style.minWidth = config.min_width;
  if (config.max_width) node.style.maxWidth = config.max_width;
  const icon = node.querySelector('svg');
  const iconSize = config.element_size || config.size;
  if (icon && iconSize) {
    icon.style.width = iconSize;
    icon.style.height = iconSize;
  }
}

function headerControl(name) {
  if (!headerNodes) {
    const select = byId('sessions');
    headerNodes = {
      agent_name: byId('chat-header-agent-name').closest('.chat-header-name-row'),
      session_dropdown: select.closest('.session-dropdown'),
      new_session: byId('new'),
    };
    for (const [name, delta, label, symbol] of [
      ['session_prev', -1, 'Previous conversation', '‹'],
      ['session_next', 1, 'Next conversation', '›'],
    ]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'v3-chat-nav-button';
      button.setAttribute('aria-label', label);
      button.title = label;
      button.textContent = symbol;
      button.onclick = () => {
        const next = select.selectedIndex + delta;
        if (next < 0 || next >= select.options.length) return;
        select.selectedIndex = next;
        select.dispatchEvent(new Event('change', { bubbles: true }));
      };
      headerNodes[name] = button;
    }
    const minimize = document.createElement('button');
    minimize.type = 'button';
    minimize.className = 'v3-chat-nav-button';
    minimize.setAttribute('aria-label', 'Minimize chat');
    minimize.title = 'Minimize chat';
    minimize.textContent = '−';
    minimize.onclick = () => byId('chat-toggle-btn').click();
    headerNodes.minimize = minimize;
  }
  return headerNodes[name];
}

function applyHeader(profile) {
  const header = byId('chat-header');
  const config = profile.chat_header || {};
  header.hidden = config.enabled === false;
  if (header.hidden) return;
  header.style.setProperty('--chat-header-max-width', config.max_width || profile.content_max_width || '100%');
  const rows = Array.isArray(config.rows) ? config.rows : [];
  headerControl('agent_name');
  header.replaceChildren();
  for (const row of rows) {
    const line = document.createElement('div');
    line.className = 'chat-header-row v3-config-header-row';
    for (const zoneName of ['left', 'center', 'right']) {
      const zone = document.createElement('span');
      zone.className = 'chat-header-zone chat-header-zone-' + zoneName;
      for (const name of row[zoneName] || []) {
        const node = headerControl(name);
        const controlConfig = config.controls?.[name] || {};
        if (!node || controlConfig.enabled === false) continue;
        node.hidden = false;
        sizeControl(node, controlConfig);
        zone.append(node);
      }
      line.append(zone);
    }
    if (line.querySelector('.chat-header-zone:not(:empty)')) header.append(line);
  }
}

let footerNodes = null;

function footerPlacement(rows, names) {
  for (let row = 0; row < rows.length; row++) {
    for (const [zone, column] of [['left', 1], ['center', 2], ['right', 3]]) {
      const spec = rows[row]?.[zone] || {};
      if ((spec.controls || []).some(name => names.includes(name))) return { row: row + 1, column, spec };
    }
  }
  return null;
}

function positionFooterNode(node, placement) {
  node.hidden = !placement;
  if (!placement) return;
  node.style.gridRow = String(placement.row);
  node.style.gridColumn = String(placement.column);
}

function applyFooter(profile, mode) {
  const footer = profile[mode + '_footer'] || {};
  const pill = footer.chat_pill || {};
  const surface = byId('chat-panel');
  if (pill.pill_width) surface.style.setProperty('--v3-pill-width', pill.pill_width);
  if (pill.pill_radius) surface.style.setProperty('--v3-pill-radius', pill.pill_radius);
  byId('chat-input-area').style.paddingBottom = footer.margin_bottom || '';
  if (profile.messages?.pill_placeholder) byId('chat-input').placeholder = profile.messages.pill_placeholder;
  if (!footerNodes) {
    const textarea = byId('chat-input');
    const stop = byId('stop');
    const send = byId('send');
    const status = byId('run-status');
    const layout = byId('chat-input-row').querySelector('.chat-pill-layout-rows');
    layout.replaceChildren(textarea, status);
    footerNodes = { textarea, stop, send, status, layout };
  }
  const { textarea, stop, send, status, layout } = footerNodes;
  const inputConfig = pill.controls?.textarea || {};
  for (const [property, value] of [
    ['min-height', inputConfig.min_height],
    ['max-height', inputConfig.max_height],
    ['font-size', inputConfig.font_size],
    ['padding', inputConfig.padding],
  ]) if (value) textarea.style.setProperty(property, value, 'important');
  const rows = Array.isArray(pill.rows) ? pill.rows : [];
  const inputPlace = footerPlacement(rows, ['textarea']);
  positionFooterNode(textarea, inputPlace);
  status.hidden = mode === 'idle';
  status.style.gridRow = String(Math.max(rows.length, 1));
  status.style.gridColumn = '2';
  layout.style.gridTemplateRows = 'repeat(' + Math.max(rows.length, 1) + ', auto)';
  for (const zone of layout.querySelectorAll('.v3-chat-actions')) zone.remove();
  const zones = new Map();
  for (const [node, names] of [[stop, ['stop']], [send, ['mic_send', 'send']]]) {
    const placement = footerPlacement(rows, names);
    if (!placement) { node.hidden = true; continue; }
    const key = placement.row + ':' + placement.column;
    let zone = zones.get(key);
    if (!zone) {
      zone = document.createElement('div');
      zone.className = 'v3-chat-actions';
      zone.style.gridRow = String(placement.row);
      zone.style.gridColumn = String(placement.column);
      zone.style.padding = placement.spec.padding || '';
      zone.style.justifyContent = placement.column === 1 ? 'flex-start' : placement.column === 2 ? 'center' : 'flex-end';
      layout.append(zone);
      zones.set(key, zone);
    }
    zone.append(node);
  }
  stop.dataset.configEnabled = String(pill.controls?.stop?.enabled !== false);
  stop.hidden = !footerPlacement(rows, ['stop']) || !stop.dataset.run || pill.controls?.stop?.enabled === false;
  send.hidden = !footerPlacement(rows, ['mic_send', 'send']) || (pill.controls?.mic_send?.enabled === false && pill.controls?.send?.enabled === false);
  sizeControl(stop, pill.controls?.stop);
  sizeControl(send, pill.controls?.mic_send || pill.controls?.send);
}

export function applyChatConfig() {
  const profile = currentChatProfile();
  if (!profile) return;
  const surface = surfaceName();
  const mode = byId('chat-input').value || document.activeElement === byId('chat-input') ? 'active' : 'idle';
  if (surface === currentSurface && mode === currentFooterMode) return;
  currentSurface = surface;
  currentFooterMode = mode;
  const panel = byId('chat-panel');
  panel.style.setProperty('--v3-content-max-width', profile.content_max_width || '100%');
  panel.style.setProperty('--v3-tail-gap', (profile.transcript_tail_clearance?.gap_px ?? 16) + 'px');
  applyHeader(profile);
  applyFooter(profile, mode);
}

export async function initChatConfig() {
  const response = await fetch('/api/v3/auth/ui-config', { credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) return;
  const data = await response.json();
  const config = data.chat_ui || data;
  if (!config?.chat_common) return;
  documentConfig = config;
  currentSurface = '';
  currentFooterMode = '';
  applyChatConfig();
  if (!listenersBound) {
    listenersBound = true;
    window.addEventListener('resize', applyChatConfig);
    const input = byId('chat-input');
    for (const eventName of ['focus', 'blur', 'input']) input.addEventListener(eventName, applyChatConfig);
  }
}
