'use strict';

// Shared visual/runtime behavior for every non-panel chat surface. The panel
// owns the reference markup; widget and embed use these same classes and this
// helper so grid placement, sizing, icons, mic/send state and footer expansion
// cannot drift between their authenticated and anonymous transports.
import { icon } from './icons.js';
import { applyChatPillLayout, configuredRowControls } from './chat-pill-config.js';
import { keepPillFocusOnFooterTap } from './dom-utils.js';

export function installChatSurfaceIcons(els, headIcon = 'message-circle') {
  if (els.headIcon) els.headIcon.innerHTML = icon(headIcon, { size: '16px' });
  if (els.close) els.close.innerHTML = icon('x', { size: '15px' });
  if (els.voice) els.voice.innerHTML = icon('mic', { size: '32px' });
  if (els.send) els.send.innerHTML = icon('send', { size: '32px' });
  if (els.attach) els.attach.innerHTML = icon('plus', { size: '22px' });
  if (els.stopIcon) els.stopIcon.innerHTML = icon('square', { size: '12px' });
  if (els.continueIcon) els.continueIcon.innerHTML = icon('play', { size: '12px' });
  if (els.toggleIcon) els.toggleIcon.innerHTML = icon('chevron-up', { size: '14px' });
  if (els.abilitiesIcon) els.abilitiesIcon.innerHTML = icon('blocks', { size: '16px' });
  if (els.targetIcon) els.targetIcon.innerHTML = icon('monitor', { size: '16px' });
}

export function chatSurfaceStatsHtml() {
  return '<button type="button" class="chat-stats-chev left" aria-label="Scroll left">&#10094;</button>'
    + '<div class="chat-pill-stats-strip">'
    + '<div class="chat-token-bar">'
    + '<span class="chat-token-label">in</span>'
    + '<span class="chat-token-value" data-chat-tokens-in>0</span>'
    + '<span class="chat-token-spinner" aria-hidden="true"></span>'
    + '<span class="chat-token-value" data-chat-tokens-out>0</span>'
    + '<span class="chat-token-label">out</span>'
    + '</div>'
    + '<div class="chat-model-ctx"><span class="chat-token-label">ctx</span> '
    + '<span class="chat-ctx-value" data-chat-context>0</span></div>'
    + '<div class="chat-cost" data-chat-cost>$0</div>'
    + '</div>'
    + '<button type="button" class="chat-stats-chev right" aria-label="Scroll right">&#10095;</button>';
}

// Wire chevron scroll + visibility on a stats carousel created by chatSurfaceStatsHtml().
// Call this after injecting the HTML into a .chat-pill-stats container.
export function wireStatsCarousel(statsContainer) {
  const strip = statsContainer?.querySelector('.chat-pill-stats-strip');
  const chevLeft = statsContainer?.querySelector('.chat-stats-chev.left');
  const chevRight = statsContainer?.querySelector('.chat-stats-chev.right');
  if (!strip || !chevLeft || !chevRight) return;

  const _update = () => {
    const overflow = strip.scrollWidth - strip.clientWidth > 1;
    chevLeft.classList.toggle('visible', overflow && strip.scrollLeft > 1);
    chevRight.classList.toggle('visible', overflow && strip.scrollLeft < strip.scrollWidth - strip.clientWidth - 1);
  };

  const scrollStep = () => Math.max(60, Math.floor(strip.clientWidth * 0.5));
  chevLeft.addEventListener('click', () => { strip.scrollBy({ left: -scrollStep(), behavior: 'smooth' }); });
  chevRight.addEventListener('click', () => { strip.scrollBy({ left: scrollStep(), behavior: 'smooth' }); });
  strip.addEventListener('scroll', _update, { passive: true });

  requestAnimationFrame(_update);
  if (typeof ResizeObserver !== 'undefined') {
    let rp = false;
    const ro = new ResizeObserver(() => { if (!rp) { rp = true; requestAnimationFrame(() => { rp = false; _update(); }); } });
    ro.observe(strip);
    window.addEventListener('resize', _update);
  }
}

// Small per-surface usage renderer. It consumes the same pipeline events as the
// panel and deliberately reuses its token spinner/count classes and animations.
export function createChatSurfaceUsage(stats) {
  const tokenBar = stats?.querySelector('.chat-token-bar');
  const spinner = stats?.querySelector('.chat-token-spinner');
  const input = stats?.querySelector('[data-chat-tokens-in]');
  const output = stats?.querySelector('[data-chat-tokens-out]');
  const context = stats?.querySelector('[data-chat-context]');
  const ctxLabel = stats?.querySelector('.chat-model-ctx .chat-token-label');
  const cost = stats?.querySelector('[data-chat-cost]');
  let tokensIn = 0;
  let tokensOut = 0;
  let streamChars = 0;
  let costUsd = 0;
  let exactCtx = 0;   // last provider-exact input_tokens (llm_call_end) — the
                      // ACTUAL context sent, so a compaction drop shows here too

  // Config from applyStatsConfig (chat_ui.json → controls.stats.visible):
  // entries [{ type, decimals }]. Falls back to compact ctx + cost when not
  // applied yet (or on the legacy path).
  const cfgOf = () => (Array.isArray(stats?._statsConfig) ? stats._statsConfig : null);
  const ctxEntry = () => { const c = cfgOf(); return (c && (c.find(e => e.type === 'ctx') || c.find(e => e.type === 'ctx-max'))) || null; };
  const ctxFull = () => { const c = cfgOf(); return !!(c && c.find(e => e.type === 'ctx-max')); };
  const costEntry = () => { const c = cfgOf(); return (c && c.find(e => e.type === 'cost')) || null; };

  const paint = () => {
    if (input) input.textContent = String(tokensIn);
    if (output) output.textContent = String(tokensOut + Math.round(streamChars / 4));
    if (cost) {
      const entry = costEntry();
      const text = formatCost(costUsd, entry ? entry.decimals : null);
      cost.textContent = text;
      cost.style.display = text ? '' : 'none';   // hidden away while zero
    }
    tokenBar?.classList.toggle('active', tokensIn > 0 || tokensOut > 0 || streamChars > 0);
  };
  const setDirection = (direction) => {
    spinner?.classList.remove('spinning-in', 'spinning-out');
    if (direction) spinner?.classList.add(`spinning-${direction}`);
  };
  // Start hidden: ctx and cost reveal themselves only once they have a real
  // value (config may also gate them entirely).
  if (context) { context.textContent = ''; context.style.display = 'none'; }
  if (ctxLabel) ctxLabel.style.display = 'none';
  paint();
  return {
    begin() { tokenBar?.classList.add('active', 'spinning'); setDirection('out'); },
    stream(text) { streamChars += (text || '').length; setDirection('out'); paint(); },
    event(event) {
      if (event?.type !== 'pipeline') return;
      if (event.step === 'context_status' && typeof event.tokens === 'number' && context) {
        // chars/4 ESTIMATE of the assembled context — only a stand-in until the
        // first provider-exact llm_call_end count lands (see below). Once an
        // exact count exists it owns the readout, so a post-compaction drop is
        // shown from the provider's own number, not the estimate.
        if (exactCtx > 0) return;
        const limit = Number(event.context_window || event.context_limit || event.limit) || 0;
        const entry = ctxEntry();
        const decimals = entry ? entry.decimals : null;
        const num = formatCount(event.tokens, decimals);
        if (!num || num.startsWith('0.0')) {
          context.textContent = '';
          context.style.display = 'none';
        } else {
          const full = ctxFull();
          if (ctxLabel) ctxLabel.style.display = full ? '' : 'none';
          context.textContent = full ? `${num} / ${formatCount(limit, decimals)}` : num;
          context.style.display = '';
        }
      }
      if (event.step === 'llm_call_end') {
        tokensIn += Number(event.input_tokens) || 0;
        tokensOut += Number(event.output_tokens) || 0;
        costUsd += Number(event.cost_usd) || 0;
        streamChars = 0;
        setDirection((Number(event.output_tokens) || 0) > 0 ? 'out' : 'in');
        paint();
        // The provider's exact prompt-token count for the call that just
        // completed — adopt it directly as the ctx readout (mirrors the panel's
        // chat-activity.js llm_call_end handling). This is the actual context
        // sent to the provider, so after compaction folds older turns the next
        // call reports the reduced prompt and this drops to it.
        if (context && typeof event.input_tokens === 'number' && event.input_tokens > 0) {
          exactCtx = Math.max(0, event.input_tokens);
          const limit = Number(event.context_window || event.context_limit || event.limit) || 0;
          const entry = ctxEntry();
          const decimals = entry ? entry.decimals : null;
          const num = formatCount(exactCtx, decimals);
          if (!num || num.startsWith('0.0')) {
            context.textContent = '';
            context.style.display = 'none';
          } else {
            const full = ctxFull();
            if (ctxLabel) ctxLabel.style.display = full ? '' : 'none';
            context.textContent = full ? `${num} / ${formatCount(limit, decimals)}` : num;
            context.style.display = '';
          }
        }
      }
    },
    finish() { streamChars = 0; tokenBar?.classList.remove('spinning'); setDirection(null); paint(); },
  };
}

/** Format a USD amount. Rounds UP to the configured decimals (default 2 =
 *  cents) so the very first charge reads "$0.01" — an indicator that billing
 *  has started. Returns '' (hidden) while the value is zero. */
function formatCost(value, decimals) {
  if (!(value > 0)) return '';
  const dp = (decimals == null) ? 2 : decimals;
  const scale = Math.pow(10, dp);
  return `$${(Math.ceil(value * scale) / scale).toFixed(dp)}`;
}

/** Format a count for the pill: x.xk below 1M (1 decimal), x.xxM at/above 1M
 *  (2 decimals). An explicit `decimals` overrides the default for both ranges. */
function formatCount(value, decimals) {
  if (!(value > 0)) return '';
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(decimals == null ? 2 : decimals)}M`;
  return `${(value / 1000).toFixed(decimals == null ? 1 : decimals)}k`;
}

// Shared by panel, widget and embed; reset absent overrides on every profile switch.
export function applyMessageBubbleConfig(root, profile) {
  if (!root) return;
  // Additional transcript edge space; omitted fields preserve existing layout.
  const transcriptPadding = profile?.transcript_padding || {};
  for (const edge of ['top', 'bottom']) {
    const value = transcriptPadding[`${edge}_px`];
    root.style.setProperty(`--chat-transcript-padding-${edge}`,
      `${typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0}px`);
  }
  const cfg = profile?.message_bubbles || {};
  const settings = [
    ['font_size_px', '--chat-bubble-font-size', 17, 1],
    ['padding_y_px', '--chat-bubble-padding-y', 10, 0],
    ['padding_x_px', '--chat-bubble-padding-x', 14, 0],
    ['agent_padding_left_px', '--chat-agent-padding-left', 4, 0],
  ];
  for (const [key, token, fallback, min] of settings) {
    const value = cfg[key];
    const valid = typeof value === 'number' && Number.isFinite(value) && value >= min;
    root.style.setProperty(token, `${valid ? value : fallback}px`);
  }
  // Role surfaces come from the profile, reset on switches, and resolve theme
  // variables at the bubble rather than freezing palette values on the root.
  for (const role of ['user', 'agent']) {
    const surface = cfg[role] || {};
    for (const [key, suffix] of [['bg_dark', 'background'], ['bg_light', 'background-light'], ['border', 'border']]) {
      const token = `--chat-${role}-${suffix}`;
      const value = surface[key];
      const property = key === 'border' ? 'border' : 'background-color';
      if (typeof value === 'string' && CSS.supports(property, value)) {
        root.style.setProperty(token, value);
      } else {
        root.style.removeProperty(token);
      }
    }
  }
}

export function applyChatSurfaceProfile(els, profile) {
  applyMessageBubbleConfig(els.root, profile);
  if (!profile) return;
  // Support both new active_footer.chat_pill and legacy top-level chat_pill
  const activeFooter = profile.active_footer || {};
  const pill = activeFooter.chat_pill || profile.chat_pill || {};
  if (profile.content_max_width) els.root?.style.setProperty('--chat-surface-max-width', profile.content_max_width);
  if (pill.pill_width) {
    els.root?.style.setProperty('--chat-pill-width', pill.pill_width);
    els.root?.style.setProperty('--chat-pill-current-width', pill.pill_width);
  }
  if (pill.pill_radius) {
    els.root?.style.setProperty('--chat-pill-radius', pill.pill_radius);
  }
  const headerCfg = profile.chat_header || {};
  if (headerCfg.max_width) els.root?.style.setProperty('--chat-header-max-width', headerCfg.max_width);

  // ── Fade mask ──
  const fade = profile.fade || {};
  const fadeTop = fade.top != null ? String(fade.top) + 'px' : '0px';
  const fadeBot = fade.bottom != null ? String(fade.bottom) + 'px' : '0px';
  // A zero-length transparent-to-opaque mask transition anti-aliases into a
  // full-width seam. Keep a top fade configurable, but use one opaque stop
  // when it is disabled.
  const fadeTopStops = Number(fade.top) > 0 ? `transparent 0, #000 ${fadeTop}` : '#000 0';
  const msgEl = els.messages || els.body;
  if (msgEl) {
    msgEl.style.setProperty('--chat-fade-top', fadeTop);
    msgEl.style.setProperty('--chat-fade-top-stops', fadeTopStops);
    msgEl.style.setProperty('--chat-fade-bottom', fadeBot);
  }

  const resolvedPill = applyChatPillLayout({
    pill: els.pill,
    input: els.input,
    stats: els.stats,
    pillButtons: els.pillButtons,
    mic: els.voice,
    send: els.send,
    attach: els.attach,
    stop: els.stop,
    continue: els.continue,
  }, pill);
  els.pillConfiguredControls = new Set(configuredRowControls(resolvedPill));

  // Support both new active_footer.above_pill/below_pill and legacy top-level
  const activeFooter2 = profile.active_footer || {};
  const abovePill = activeFooter2.above_pill || profile.above_pill || {};
  const belowPill = activeFooter2.below_pill || profile.below_pill || {};
  if (els.above) els.above.hidden = abovePill.enabled === false;
  if (els.below) els.below.hidden = belowPill.enabled === false;
  const applyControls = (container, config, revealWanted = false) => {
    if (!container) return;
    const rows = Array.isArray(config?.rows)
      ? config.rows
      : [{ left: config?.left || [], center: config?.center || [], right: config?.right || [] }];
    const wanted = new Set(rows.flatMap(row => [
      ...(row?.left || []),
      ...(row?.center || []),
      ...(row?.right || []),
    ]));
    container.querySelectorAll('[data-footer-control]').forEach(el => {
      const isWanted = wanted.has(el.dataset.footerControl);
      if (!isWanted) el.hidden = true;
      else if (revealWanted) el.hidden = false;
    });
  };
  applyControls(els.above, abovePill);
  // Above-pill Stop/Continue visibility is live run state; below-pill controls
  // are static profile choices and may safely be restored when configured.
  applyControls(els.below, belowPill, true);

  // ── Header ──────────────────────────────────────────────────────────
  applyChatSurfaceHeader(els, profile);
}

export function wireChatSurfaceComposer(els, { isBusy = () => false } = {}) {
  const syncInputState = () => {
    const hasText = !!els.input?.value.trim();
    els.pill?.classList.toggle('has-text', hasText);
    if (els.send) els.send.disabled = !hasText || isBusy();
    return hasText;
  };
  els.input?.addEventListener('input', syncInputState);
  // Mobile: taps on footer controls (Continue/Stop above the pill, mode /
  // abilities / target below it) must not blur the pill and dismiss the
  // keyboard — they are typing-adjacent interactions. Keep focus on the input.
  keepPillFocusOnFooterTap(els.footer || els.composer || els.root, els.input);
  els.footerToggle?.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    const expanded = !els.below?.classList.contains('expanded');
    if (els.below) els.below.hidden = false;
    els.below?.classList.toggle('expanded', expanded);
    els.footerToggle.classList.toggle('expanded', expanded);
    els.footerToggle.setAttribute('aria-expanded', String(expanded));
  });
  syncInputState();
  return syncInputState;
}

export function appendChatSurfaceBubble(container, role, text = '') {
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble ${role}`;
  bubble.textContent = text;
  container.appendChild(bubble);
  container.scrollTop = container.scrollHeight;
  return bubble;
}

// ── Header config for widget / embed surfaces ───────────────────────────
// Control-name → els-key mapping. Each surface passes its `els` object and
// the profile; this shows/hides header children accordingly.
const SURFACE_HEADER_ELS_KEYS = {
  icon:      'headIcon',
  title:     'title',
  subtitle:  'subtitle',
  status:    'statusEl',
  dot:       'dot',
  minimize:  'minBtn',
  close:     'closeBtn',
};

export function applyChatSurfaceHeader(els, profile) {
  const headerCfg = profile?.chat_header;
  if (!headerCfg || !els.header) return;

  // Disable entire header.
  if (headerCfg.enabled === false) {
    els.header.style.display = 'none';
    return;
  }
  els.header.style.display = '';

  const rows = headerCfg.rows;
  if (!Array.isArray(rows) || rows.length === 0) return;

  // Single-row surfaces (widget, embed) — just handle the first row.
  const rowCfg = rows[0];
  const wanted = new Set([
    ...(rowCfg.left || []),
    ...(rowCfg.center || []),
    ...(rowCfg.right || []),
  ]);

  for (const [name, elsKey] of Object.entries(SURFACE_HEADER_ELS_KEYS)) {
    const el = els[elsKey];
    if (!el) continue;
    el.hidden = !wanted.has(name);
  }
}
