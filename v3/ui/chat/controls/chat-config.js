import { applyChatPillLayout } from './chat-pill-config.js';
import { initFooterHandle } from './footer-handle.js';
// V3 reads the v3/data/config/chat.json schema. Unknown controls are
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
  if (window.__CHAT_PORTAL__) return 'chat_widget';
  return (byId('chat-panel')?.clientWidth || window.innerWidth) <= 768 ? 'chat_mobile' : 'chat_desktop';
}

export function currentChatProfile() {
  if (!documentConfig?.chat_common) return null;
  return mergeProfile(documentConfig.chat_common, documentConfig[surfaceName()]);
}

// SISTER-PANEL: v2 chat-controls-config sizing; row defaults precede overrides.
function sizeControl(node, config) {
  if (!node || !config) return;
  const size = config.container_size || config.box_size;
  const icon = node.querySelector('svg') || node.querySelector('i');
  const iconSize = config.element_size || config.size || (size && size !== 'auto' ? Math.floor(parseFloat(size) * .7) + 'px' : null);
  if (icon && iconSize) for (const key of ['width','height']) icon.style.setProperty(key,iconSize,'important');
  if (size && size !== 'auto') {
    for (const key of ['height','min-height']) node.style.setProperty(key,size,'important');
    if (config.square !== false) { node.style.setProperty('width',size,'important'); node.style.padding='0'; node.style.justifyContent='center'; }
  }
  const trigger = node.querySelector('.session-dropdown-trigger');
  if (trigger) { trigger.style.height='100%'; trigger.style.minHeight='0'; trigger.style.boxSizing='border-box'; }
  if (config.font_size) { node.style.fontSize=config.font_size; if(trigger) trigger.style.fontSize=config.font_size; }
  if (config.border_radius) node.style.borderRadius=config.border_radius === 'pill' ? `calc(${size || '40px'} / 2)` : config.border_radius;
  if (config.min_width) node.style.minWidth=config.min_width;
  if (config.max_width) node.style.maxWidth=config.max_width;
}

function headerControl(name) {
  if (!headerNodes) {
    const select = byId('sessions');
    headerNodes = {
      agent_name: byId('chat-header-agent-name').closest('.chat-header-name-row'),
      session_dropdown: byId('session-dropdown'),
      new_session: byId('new'),
    };
    for (const [name, delta, label, symbol] of [
      ['session_prev', -1, 'Previous conversation', 'arrow-big-left'],
      ['session_next', 1, 'Next conversation', 'arrow-big-right'],
    ]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'session-nav-btn';
      button.setAttribute('aria-label', label);
      button.title = label;
      button.innerHTML = '<i data-lucide="' + symbol + '"></i>';
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
    minimize.className = 'chat-header-minimize';
    minimize.setAttribute('aria-label', 'Minimize chat');
    minimize.title = 'Minimize chat';
    minimize.innerHTML = '<i data-lucide="minus"></i>';
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
  headerNodes.session_dropdown.configure?.(config.controls?.session_dropdown);
  headerNodes.session_dropdown._sessionDropdownController?.close({immediate:true});
  header.replaceChildren();
  for (const row of rows) {
    const line = document.createElement('div');
    line.className = 'chat-header-row v3-config-header-row';
    for (const zoneName of ['left', 'center', 'right']) {
      const zone = document.createElement('span');
      zone.className = 'chat-header-zone chat-header-zone-' + zoneName;
      for (const name of row[zoneName] || []) {
        const node = headerControl(name);
        const controlConfig = { ...config.row_sizes?.primary, ...config.controls?.[name] };
        if (name === 'agent_name') controlConfig.square = false;
        if (!node || controlConfig.enabled === false) continue;
        node.hidden = false;
        node.dataset.headerControl = name;
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
      for (const [node, feature] of [[footerNodes.attach,'Attachments'],[footerNodes.mic,'Voice']]) {
        node.type = 'button';
        node.disabled = byId('chat-input-row').dataset.composerAvailable !== 'true';
        node.title = feature === 'Attachments' ? 'Attach files (or paste / drop onto the chat pill)' : 'Voice conversation';
      }
      footerNodes.send.type = 'submit';
      footerNodes.stop.type = 'button';
      footerNodes.continue.type = 'button';
      footerNodes.continue.disabled = false;
      // Provider usage and measured context are supplied by the v3 stats adapter.
      byId('chat-token-bar').hidden = false;
      footerControls = Object.fromEntries(Array.from(byId('chat-footer-row').querySelectorAll('[data-header-control]')).map(n => [n.dataset.headerControl,n]));
      for (const name of ['changes','browser_notifications']) {
        const node = document.createElement('button'); node.type = 'button';
        node.className = name === 'changes' ? 'chat-changes-btn' : 'chat-browser-notifications-btn';
        node.dataset.headerControl = name;
        node.innerHTML = '<i data-lucide="'+(name === 'changes' ? 'files' : 'bell')+'" style="width:16px;height:16px;"></i>';
        footerControls[name] = node;
      }
      for (const [name,node] of Object.entries(footerControls)) {
        node.type = 'button';
        if(['mode','model_changer'].includes(name))continue;
        node.disabled = true;node.classList.add('v3-backend-pending');
        node.title = name.replaceAll('_',' ') + ' — backend port pending';
      }
      footerNodes.stats.style.display = '';
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
    const contentProfile = mergeProfile(documentConfig.chat_common, documentConfig[window.innerWidth <= 768 ? 'chat_mobile' : 'chat_desktop']);
    surface.style.setProperty('--chat-surface-max-width',contentProfile.content_max_width || '1000px');
    if(profile.messages?.pill_placeholder)footerNodes.input.placeholder=profile.messages.pill_placeholder;
    applyChatPillLayout(footerNodes,pill);
    footerNodes.stop.dataset.configEnabled=String(pill.controls?.stop?.enabled !== false);
    footerNodes.stop.hidden=!footerNodes.stop.dataset.run || pill.controls?.stop?.enabled === false;
    footerNodes.stop.style.display=footerNodes.stop.hidden ? 'none' : '';
    footerNodes.continue.dataset.configEnabled=String(pill.controls?.continue?.enabled !== false);
    const continuing=!footerNodes.stop.dataset.run && !footerNodes.input.value.trim() && !Number(footerNodes.pill.dataset.attachmentCount) && pill.controls?.continue?.enabled !== false;
    footerNodes.continue.hidden=!continuing;
    footerNodes.continue.style.display=continuing ? '' : 'none';
    // V2 mic/send presentation includes staged attachments.
    const hasText=!!footerNodes.input.value.trim()||!!Number(footerNodes.pill.dataset.attachmentCount);
    footerNodes.pill.classList.toggle('has-text',hasText);
    footerNodes.send.style.display=hasText ? '' : 'none';
    footerNodes.mic.style.display=hasText ? 'none' : '';
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
  const focused = document.activeElement;
  const inComposer = byId('chat-input-area')?.contains(focused) || focused?.closest('.v3-context-panel:not([hidden]), .v3-chat-model-list:not([hidden])');
  const mode = byId('chat-input').value || focused === byId('chat-input') || (currentFooterMode === 'active' && inComposer) ? 'active' : 'idle';
  if (applyingFooter) return;
  if (surface === currentSurface && mode === currentFooterMode) {
    const text=!!byId('chat-input').value.trim()||!!Number(byId('chat-input-row').dataset.attachmentCount);
    footerNodes?.pill.classList.toggle('has-text',text);
    if(footerNodes){footerNodes.send.style.display=text ? '' : 'none';footerNodes.mic.style.display=text ? 'none' : '';
      const continuing=!footerNodes.stop.dataset.run&&!text&&footerNodes.continue.dataset.configEnabled!=='false';
      footerNodes.continue.hidden=!continuing;footerNodes.continue.style.display=continuing?'':'none';}
    return;
  }
  currentSurface = surface;
  currentFooterMode = mode;
  const panel = byId('chat-panel');
  const contentProfile = mergeProfile(documentConfig.chat_common, documentConfig[window.innerWidth <= 768 ? 'chat_mobile' : 'chat_desktop']);
  panel.style.setProperty('--v3-content-max-width', contentProfile.content_max_width || '100%');
  panel.style.setProperty('--v3-transcript-top',(profile.transcript_padding?.top_px ?? 25)+'px');
  panel.style.setProperty('--v3-transcript-bottom',(profile.transcript_padding?.bottom_px ?? 50)+'px');
  const tailClearance = profile.transcript_tail_clearance || {};
  const tailPixels = (value, fallback) => Number.isFinite(Number(value)) && Number(value) >= 0 ? Number(value) : fallback;
  panel.style.setProperty('--v3-tail-gap', tailPixels(tailClearance.gap_px, 24) + 'px');
  panel.style.setProperty('--v3-tail-min', tailPixels(tailClearance.min_px, 100) + 'px');
  // Match v2's stationary message-scroller mask; zero top fade avoids a seam.
  const messages = byId('chat-messages');
  const top = Math.max(0, Number(profile.fade?.top) || 0);
  const bottom = Math.max(0, Number(profile.fade?.bottom) || 0);
  messages?.style.setProperty('--chat-fade-top', top + 'px');
  messages?.style.setProperty('--chat-fade-top-stops', top > 0 ? `transparent 0, #000 ${top}px` : '#000 0');
  messages?.style.setProperty('--chat-fade-bottom', bottom + 'px');
  applyHeader(profile);
  applyFooter(profile, mode);
}

export async function initChatConfig() {
  const response = await fetch('/api/v3/auth/ui-config', { credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) return;
  const data = await response.json();
  const config = data.chat_ui || data;
  if (!config?.chat_common) return;
  // A remount replaces the partial; never reuse detached header/pill nodes.
  if (footerNodes && footerNodes.input !== byId('chat-input')) { headerNodes=null; footerNodes=null; footerControls=null; currentSurface=''; currentFooterMode=''; }
  documentConfig = config;
  currentSurface = '';
  currentFooterMode = '';
  applyChatConfig();
  if (!listenersBound) {
    listenersBound = true;
    window.addEventListener('resize', () => { currentSurface = ''; applyChatConfig(); });
    new ResizeObserver(applyChatConfig).observe(byId('chat-panel'));
    // Defer focusout until the destination has focus; a synchronous textarea
    // blur must not detach a stats/mode/model control before its click fires.
    // Delegation also survives chat-partial remounts.
    // Pointer controls belong to the composer interaction: keep its caret
    // and selection instead of letting a button blur an empty composer.
    // Keyboard focus and editable popup fields retain their normal behavior.
    document.addEventListener('pointerdown', event => {
      if (event.button !== 0 || document.activeElement !== byId('chat-input')) return;
      const control = event.target.closest?.('button, [role="button"], #chat-footer-handle, [data-header-control]');
      if (!control || !control.closest('#chat-input-area, #chat-header, .v3-context-panel, .v3-chat-model-list, .session-dropdown-menu')) return;
      event.preventDefault();
    }, true);
    document.addEventListener('focusin', applyChatConfig);
    document.addEventListener('focusout', () => setTimeout(applyChatConfig, 0));
    document.addEventListener('input', event => { if(event.target === byId('chat-input'))applyChatConfig(); });
  }
}
