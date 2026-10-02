import { applyChatPillLayout } from './chat-pill-config.js';
import { initFooterHandle } from './footer-handle.js';
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
let applyingFooter = false;
let footerControls = null;
function applyFooter(profile, mode) {
  if (applyingFooter) return;
  applyingFooter = true;
  try {
    const footer = profile[mode + '_footer'] || {};
    const pill = footer.chat_pill || {};
    const surface = byId('chat-panel');
    const area = byId('chat-input-area');
    if (!footerNodes) {
      footerNodes = { pill: byId('chat-input-row'), input: byId('chat-input'),
        stats: byId('chat-pill-stats-row'), pillButtons: document.querySelector('#chat-input-row .chat-pill-buttons'),
        mic: byId('chat-voice-btn'), send: byId('chat-send'), attach: byId('chat-attach-btn'),
        stop: byId('chat-stop-btn'), continue: byId('chat-continue-btn') };
      for (const [node, feature] of [[footerNodes.attach,'Attachments'],[footerNodes.mic,'Voice'],[footerNodes.continue,'Continue']]) {
        node.type = 'button';
        node.disabled = true;
        node.title = feature + ' — unavailable in v3';
      }
      footerNodes.send.type = 'submit';
      footerNodes.stop.type = 'button';
      // No invented usage: v3 does not currently provide these totals.
      byId('chat-token-bar').hidden = true;
      footerControls = Object.fromEntries(Array.from(byId('chat-footer-row').querySelectorAll('[data-header-control]')).map(n => [n.dataset.headerControl,n]));
      for (const name of ['changes','browser_notifications']) {
        const node = document.createElement('button'); node.type = 'button';
        node.className = name === 'changes' ? 'chat-changes-btn' : 'chat-browser-notifications-btn';
        node.dataset.headerControl = name;
        node.innerHTML = '<i data-lucide="'+(name === 'changes' ? 'files' : 'bell')+'" style="width:16px;height:16px;"></i>';
        footerControls[name] = node;
      }
      for (const [name,node] of Object.entries(footerControls)) {
        node.type = 'button'; node.disabled = true;
        node.title = name.replaceAll('_',' ') + ' — unavailable in v3';
      }
      // Until usage metadata is ported, omit zero stats just as v2 does.
      footerNodes.stats.style.display = 'none';
      initFooterHandle();
      const handle = byId('chat-footer-handle');
      handle.tabIndex = 0; handle.setAttribute('role','button');
      handle.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); handle.click(); } });
    }
    area.classList.toggle('footer-mode-idle', mode === 'idle');
    area.classList.toggle('footer-mode-active', mode === 'active');
    for (const [token,value] of [['--chat-pill-width',pill.pill_width],['--chat-pill-current-width',pill.pill_width],['--chat-pill-radius',pill.pill_radius]]) if(value)surface.style.setProperty(token,value);
    area.style.setProperty('--idle-pill-width',profile.idle_footer?.chat_pill?.pill_width || '500px');
    area.style.setProperty('--idle-margin-bottom',profile.idle_footer?.margin_bottom || '24px');
    area.style.setProperty('bottom',footer.margin_bottom || '0px','important');
    surface.style.setProperty('--chat-surface-max-width',profile.content_max_width || '1000px');
    if(profile.messages?.pill_placeholder)footerNodes.input.placeholder=profile.messages.pill_placeholder;
    applyChatPillLayout(footerNodes,pill);
    footerNodes.stop.dataset.configEnabled=String(pill.controls?.stop?.enabled !== false);
    footerNodes.stop.hidden=!footerNodes.stop.dataset.run || pill.controls?.stop?.enabled === false;
    footerNodes.stop.style.display=footerNodes.stop.hidden ? 'none' : '';
    footerNodes.continue.hidden=true;
    footerNodes.continue.style.display='none';
    // Canonical mic/send presentation toggles on content, without inventing voice support.
    const hasText=!!footerNodes.input.value.trim();
    footerNodes.pill.classList.toggle('has-text',hasText);
    footerNodes.send.style.display=hasText ? '' : 'none';
    footerNodes.mic.style.display=hasText ? 'none' : '';
    footerNodes.stats.style.display='none';
    const row=byId('chat-footer-row');
    const below=profile.active_footer?.below_pill || {};
    row.replaceChildren();
    for(const spec of below.rows || []) {
      const line=document.createElement('div'); line.className='chat-header-row';
      for(const zoneName of ['left','center','right']) {
        const zone=document.createElement('span'); zone.className='chat-header-zone chat-header-zone-'+zoneName;
        for(const name of spec[zoneName] || []) {
          const node=footerControls[name]; const cfg=below.controls?.[name] || {};
          if(!node || cfg.enabled === false)continue;
          node.hidden=false;sizeControl(node,cfg);zone.append(node);
        }
        line.append(zone);
      }
      row.append(line);
    }
    row.hidden=below.enabled === false;
    row.classList.toggle('idle-collapsed',mode === 'idle');
    byId('chat-footer-handle').style.display=mode === 'idle' || row.hidden ? 'none' : '';
    window.lucide?.createIcons();
  } finally { applyingFooter=false; }
}

export function applyChatConfig() {
  const profile = currentChatProfile();
  if (!profile) return;
  const surface = surfaceName();
  const mode = byId('chat-input').value || document.activeElement === byId('chat-input') ? 'active' : 'idle';
  if (applyingFooter) return;
  if (surface === currentSurface && mode === currentFooterMode) {
    const text=!!byId('chat-input').value.trim();
    footerNodes?.pill.classList.toggle('has-text',text);
    if(footerNodes){footerNodes.send.style.display=text ? '' : 'none';footerNodes.mic.style.display=text ? 'none' : '';}
    return;
  }
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
