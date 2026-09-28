'use strict';

/**
 * Shared sticky section navigator for the Admin → App Config pages.
 * ──────────────────────────────────────────────────────────────────────────
 * Keeps EVERY section heading on screen the whole time you scroll a config
 * page: a heading whose table you've scrolled past pins to the TOP of the
 * list, a heading whose table is still coming up pins to the BOTTOM, and the
 * one you're reading sits between them with its table. Every heading is always
 * visible and is click-to-jump to its table.
 *
 * One page opts in by:
 *   1. adding the `ac-stickynav` class to its `<section class="ac-section">`
 *      (the CSS in app3.css flattens the groups with display:contents and
 *      paints the sticky banner look — see "ADVANCED SCROLL" there), and
 *   2. calling `initStickyNav('<section-suffix>')` from its module's init(),
 *      plus registering it as a section hook so it re-measures every time the
 *      page is shown (at init() the section may be hidden → everything
 *      measures 0).
 *
 * WHY display:contents — a `position:sticky` element can only travel inside
 * its own containing block. Each heading normally lives in its own
 * `.ac-category-group`, so it could only stay stuck while THAT one group was on
 * screen. Collapsing the group box with display:contents makes the heading a
 * direct child of the whole section, so its containing block is the ENTIRE
 * page — which is what lets it stay stuck across every other section. The CSS
 * handles the flatten + banner; THIS module measures the headings and writes
 * their stacked top/bottom offsets inline, wires the jump clicks, and
 * highlights the current one.
 *
 * COLLAPSIBLE PAGES — display:contents breaks a `<details>`'s native collapse
 * AND its flow layout, so a collapsible group is authored as a plain
 * `<div class="ac-category-group ac-collapsible">` and opened/closed via an
 * `.ac-open` CLASS (CSS hides its body unless `.ac-open`). A group WITHOUT
 * `.ac-collapsible` is always-open (App Settings' page lists). This module tells
 * them apart by that class: collapsible = click toggles open and jumps when
 * opening; always-open = click only jumps.
 *
 * The scroller is the shared `#app-config-content` for every page.
 *
 * MOBILE SUB-HEADER (opt-in) — a section can pass `{ mobileCarousel: true }` to
 * initStickyNav (Data Settings, App Settings, and Agent Settings all do). On a
 * mobile-width viewport that section drops the vertical sticky stacking and
 * instead renders its headings as a
 * horizontal, swipeable strip of underline tabs in #app-config-subnav — a genuine
 * SUB-HEADER that sits directly beneath the app-config tab strip and OUTSIDE the
 * content scroller, so it spans the full width and mirrors the tab strip's look
 * (see the `.ac-mnav` block in app3.css). Tapping a tab jumps the scroll to that
 * section (landing its heading flush under the sub-header), and the tab for the
 * section you're reading highlights. Desktop is unchanged. See the `_syncMode` /
 * `_buildCarousel` helpers below and the `_navHost` / `_navFor` accessors.
 */

import { isMobileLayout } from '../../../shared/js/layout.js';
import { applyRubberBand } from '../../../shared/js/rubber-band.js';

const _SCROLLER_ID = 'app-config-content';
const _registered = new Set();   // section suffixes participating (for scroll updates)
// Sections that opted into the MOBILE heading carousel (initStickyNav(id,{mobileCarousel})).
const _carousel = new Set();
// Current active heading index per section — the single source of truth the DOM
// highlight (sticky heading + mobile chip) is painted from by _setActive.
const _active = {};
// Set while a click-driven smooth-scroll is animating: { idx, target, deadline }.
// The programmatic scroll fires scroll events that would otherwise re-derive a
// different section mid-flight, so while a lock is present _updateActive HOLDS
// the clicked index until the scroll reaches `target` (or the deadline lapses).
// This is what makes "clicking a tab activates that tab" hold for a short
// trailing section the page can't scroll to the engagement line.
const _lock = {};
const _wiredScrollers = new WeakSet();
let _resizeWired = false;
let _raf = 0;

function _contentRoot() { return document.getElementById(_SCROLLER_ID); }
function _scroller() {
  const content = _contentRoot();
  return content?.closest('.inst-grid.carousel') || content;
}
function _section(id) { return document.getElementById('ac-section-' + id); }
// The mobile sub-header host — a sibling directly under #app-config-tabs-wrap and
// OUTSIDE the scroller (markup in app-config.html). The carousel `.ac-mnav` for a
// section is parked here (tagged with data-section) so it renders as a real full-
// width sub-header, not a strip clipped inside the content scroller.
function _navHost() { return document.getElementById('app-config-subnav'); }
function _navFor(id) { const h = _navHost(); return h ? h.querySelector(':scope > .ac-mnav[data-section="' + id + '"]') : null; }
function _subnavOffset(scroller) {
  const content = _contentRoot();
  const host = _navHost();
  return scroller && scroller !== content && host?.offsetParent !== null
    ? Math.round(host.getBoundingClientRect().height)
    : 0;
}
// Unified carousel that combines headings from all sections
let _unifiedCarousel = null;
const _ALL_SECTIONS = ['data-settings', 'app-settings', 'agent-settings'];

function _getAllHeads() {
  const allHeads = [];
  for (const id of _ALL_SECTIONS) {
    const heads = _heads(id);
    heads.forEach((h, i) => {
      allHeads.push({ section: id, index: i, heading: h, label: _headLabel(h) });
    });
  }
  return allHeads;
}

function _buildUnifiedCarousel() {
  const host = _navHost();
  if (!host) return null;
  
  const allHeads = _getAllHeads();
  if (!allHeads.length) return null;
  
  // Check if we need to rebuild
  const sig = allHeads.map(h => h.section + ':' + h.label).join('|');
  if (_unifiedCarousel && _unifiedCarousel.dataset.sig === sig) return _unifiedCarousel;
  
  // Remove old carousels — both per-section and any previous unified carousel
  host.querySelectorAll('.ac-mnav[data-section], .ac-mnav.ac-unified-mnav').forEach(el => el.remove());
  
  // Create unified carousel
  _unifiedCarousel = document.createElement('div');
  _unifiedCarousel.className = 'ac-mnav ac-unified-mnav';
  _unifiedCarousel.dataset.sig = sig;
  _unifiedCarousel.innerHTML = '<div class="ac-mnav-strip" role="tablist"></div>';
  
  const strip = _unifiedCarousel.querySelector('.ac-mnav-strip');
  
  allHeads.forEach((h, i) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'ac-mnav-chip';
    chip.dataset.idx = String(i);
    chip.dataset.section = h.section;
    chip.dataset.sectionIdx = String(h.index);
    
    // Add section label as prefix (Data, App, Agent)
    const sectionLabel = document.createElement('span');
    sectionLabel.className = 'ac-mnav-section-label';
    const sectionNames = { 'data-settings': 'Data', 'app-settings': 'App', 'agent-settings': 'Agent' };
    sectionLabel.textContent = sectionNames[h.section] || '';
    chip.appendChild(sectionLabel);
    
    const lbl = document.createElement('span');
    lbl.className = 'ac-mnav-label';
    lbl.textContent = h.label;
    chip.appendChild(lbl);
    
    chip.addEventListener('click', () => _scrollToHeading(h.section, h.index));
    strip.appendChild(chip);
  });
  
  host.appendChild(_unifiedCarousel);
  strip.addEventListener('scroll', () => _updateMnavEdges(_unifiedCarousel), { passive: true });
  applyRubberBand(strip);
  
  if (window.lucide) {
    try { window.lucide.createIcons({ nodes: Array.from(strip.querySelectorAll('[data-lucide]:not(.lucide)')) }); } catch (_) {}
  }
  
  _updateUnifiedActive();
  return _unifiedCarousel;
}

function _scrollToHeading(sectionId, headingIndex) {
  const groups = _groups(sectionId);
  const scroller = _scroller();
  const g = groups[headingIndex];
  if (!g || !scroller) return;
  
  const head = g.querySelector(':scope > .ac-category-summary');
  if (!head) return;
  
  const sRect = scroller.getBoundingClientRect();
  const hRect = head.getBoundingClientRect();
  const padTop = parseFloat(getComputedStyle(scroller).paddingTop) || 0;
  const target = scroller.scrollTop + (hRect.top - sRect.top)
    - padTop - _subnavOffset(scroller);
  
  _selectAndScroll(sectionId, headingIndex, target);
}

function _updateUnifiedActive() {
  if (!_unifiedCarousel) return;
  
  const scroller = _scroller();
  if (!scroller) return;
  
  const sRect = scroller.getBoundingClientRect();
  const strip = _unifiedCarousel.querySelector('.ac-mnav-strip');
  const chips = _unifiedCarousel.querySelectorAll('.ac-mnav-chip');
  
  // Find which heading is most visible
  let activeIdx = 0;
  let maxVisible = 0;
  
  const allHeads = _getAllHeads();
  allHeads.forEach((h, i) => {
    const rect = h.heading.getBoundingClientRect();
    const visible = Math.min(rect.bottom, sRect.bottom) - Math.max(rect.top, sRect.top);
    if (visible > maxVisible) {
      maxVisible = visible;
      activeIdx = i;
    }
  });
  
  _unifiedCarousel.dataset.activeIdx = String(activeIdx);
  chips.forEach((c, i) => c.classList.toggle('active', i === activeIdx));
  _scrollChipIntoView(strip, chips[activeIdx]);
}
// Every top-level `.ac-category-group` in the section, at any depth — so a page
// that renders its groups inside a wrapper (e.g. Features' #ac-features-list)
// still works. Nested groups (a group inside another group) are excluded so
// only the real section headings are picked up.
function _groups(id) {
  const sec = _section(id);
  if (!sec) return [];
  return Array.from(sec.querySelectorAll('.ac-category-group'))
    .filter((g) => !g.parentElement || !g.parentElement.closest('.ac-category-group'));
}
// Only VISIBLE headings take part in the stack. Some pages hide groups at
// runtime (e.g. Agent Settings empties + hides its legacy integration
// categories once their cards are relocated into the unified abilities table) —
// a hidden heading measures 0 and would otherwise pollute the stacked offsets.
// `offsetParent === null` is null exactly when the element (or an ancestor) is
// display:none, which also covers the whole section being inactive.
function _heads(id) {
  return _groups(id)
    .map((g) => g.querySelector(':scope > .ac-category-summary'))
    .filter((h) => h && h.offsetParent !== null);
}

// Measure each heading's rendered height and write the stacked offsets: a
// heading's TOP offset is the combined height of every heading above it (so
// when it pins to the top it lands flush under them); its BOTTOM offset is the
// combined height of every heading below it. A sticky element pins at the
// scroller's PADDING edge, so we subtract the scroller's own padding so the top
// stack sits flush just under the tabs and the bottom stack flush at the very
// bottom. No-op while the section is hidden (everything measures 0) — the
// section hook re-runs it once shown.
function _recalc(id) {
  const heads = _heads(id);
  if (!heads.length) return;
  const hts = heads.map((h) => Math.round(h.getBoundingClientRect().height));
  if (hts.every((h) => h === 0)) return;            // hidden → nothing to measure yet
  const scroller = _scroller();
  const cs = scroller ? getComputedStyle(scroller) : null;
  const padTop = cs ? (parseFloat(cs.paddingTop) || 0) : 0;
  const padBottom = cs ? (parseFloat(cs.paddingBottom) || 0) : 0;
  // Pull each stacked heading 1px CLOSER to the one before it. Heights are
  // Math.round-ed, so the cumulative offset can land ~1px below the previous
  // heading's true (fractional) bottom edge, leaving a 1px sliver of panel
  // showing between two pinned headings. Overlapping the stack by 1px per level
  // closes that seam; the overlap itself is invisible because every heading is
  // the same glass. (Only between headings — the first/last stay flush via the 0
  // start, so we never pull the top stack above the tabs or the bottom past the
  // floor.)
  const OVERLAP = 1;
  let topAcc = 0;
  const tops = hts.map((ht) => { const v = topAcc; topAcc += ht - OVERLAP; return v; });
  const bots = new Array(heads.length);
  let botAcc = 0;
  for (let i = heads.length - 1; i >= 0; i--) { bots[i] = botAcc; botAcc += hts[i] - OVERLAP; }
  // FLOOR PILL — if a section marks a floating element pinned INSIDE the
  // scroller at its bottom (class `.ac-stack-floor`), the bottom-stacking
  // headings must pile up RIGHT ON that element's top edge rather than behind it
  // / at the very floor — and with NO gap, so page content scrolling up vanishes
  // flush under the lowest heading instead of peeking through a gap between the
  // heading and the (opaque) element. We measure its live top edge (so it tracks
  // its real height/position) and re-base the bottom offsets to it: a sticky
  // `bottom: B` holds a heading's bottom edge at (contentBoxBottom − B), so to
  // land the lowest heading's bottom GAP px below the element top (GAP negative =
  // a small overlap that seals any sub-pixel sliver) we set
  // B = bots + (contentBoxBottom − pillTop) + GAP. With no such element, keep the
  // flush-to-floor base (bots − padBottom); padBottom cancels out of the pill
  // branch, so it's not subtracted there.
  //
  // NOTE: no current section uses `.ac-stack-floor`. App Settings' page-assistant
  // pill USED to sit inside the scroller and used this path; it now lives OUTSIDE
  // the scroller (a sibling below #app-config-content — see #ac-pa-bar in
  // app3.css), so the scroll viewport simply ends above it and the flush-to-floor
  // base is used. This branch is kept for any future in-scroller floor element.
  const sec = _section(id);
  let floorBase = null;   // null → no floor pill, use the flush-floor base
  if (sec && scroller) {
    const pill = sec.querySelector('.ac-stack-floor');
    if (pill && pill.offsetParent !== null) {
      const GAP = -2;   // overlap the pill top by 2px → no content seam
      const contentBoxBottom = scroller.getBoundingClientRect().bottom - padBottom;
      floorBase = Math.round(contentBoxBottom - pill.getBoundingClientRect().top + GAP);
    }
  }
  heads.forEach((h, i) => {
    h.style.top = (tops[i] - padTop) + 'px';
    h.style.bottom = ((floorBase === null ? (bots[i] - padBottom) : (bots[i] + floorBase))) + 'px';
  });
  _updateActive(id);
}

// Paint the active heading index onto the DOM: the desktop sticky heading gets
// `.ac-navh-active`, and (mobile) the matching carousel chip gets `.active` and
// is nudged into view. The single writer of the highlight — every rule below
// (scroll position, tab click, in-section click) funnels through here.
function _setActive(id, active) {
  const heads = _heads(id);
  if (!heads.length) return;
  active = Math.max(0, Math.min(active, heads.length - 1));
  _active[id] = active;
  heads.forEach((h, i) => h.classList.toggle('ac-navh-active', i === active));
  const sec = _section(id);
  const mnav = sec && sec.classList.contains('ac-mnav-on') ? _navFor(id) : null;
  // Reflect the current section in the carousel chips — only when it CHANGES, so
  // we don't fight the user's horizontal scroll on every tick — and keep the
  // active chip in view.
  if (mnav && mnav.dataset.activeIdx !== String(active)) {
    mnav.dataset.activeIdx = String(active);
    const chips = mnav.querySelectorAll('.ac-mnav-chip');
    chips.forEach((c, i) => c.classList.toggle('active', i === active));
    _scrollChipIntoView(mnav.querySelector('.ac-mnav-strip'), chips[active]);
  }
}

// Derive and paint the active heading from the current SCROLL position. Three
// rules, simplest first:
//   • scrolled to the very top  → first tab (top edge = start of section 0)
//   • scrolled to the very bottom → last tab (a short trailing section can't
//     push its heading up to the engagement line, so pure top-engagement could
//     never light it — this is the case the old >50%-coverage test botched)
//   • otherwise → the DEEPEST heading whose top edge has reached its engagement
//     line (desktop: its pinned offset; mobile: the scroller's top edge). No
//     coverage threshold, so a small section still activates the moment you
//     scroll its heading to the top.
// While a click-driven scroll is animating (a lock is set) we HOLD the clicked
// index instead — see `_lock`.
function _updateActive(id) {
  const sec = _section(id);
  const scroller = _scroller();
  const heads = _heads(id);
  if (!sec || !sec.classList.contains('active') || !scroller || !heads.length) return;

  // A click-driven smooth-scroll is animating: keep the tab the user picked
  // until the scroll settles at its target (or the deadline lapses), so the
  // scroll events the animation emits can't steal the highlight mid-flight.
  const lock = _lock[id];
  if (lock) {
    const reached = lock.target == null || Math.abs(scroller.scrollTop - lock.target) <= 2;
    if (!reached && Date.now() < lock.deadline) { _setActive(id, lock.idx); return; }
    _lock[id] = null;                 // settled (or timed out): keep the picked tab, let scrolling take over next event
    _setActive(id, lock.idx);
    return;
  }

  const sTop = scroller.getBoundingClientRect().top;
  // A heading pins at the scroller's top PADDING edge, so its pinned position is
  // padTop + its own top offset — account for the padding or the test mis-reads
  // which heading is engaged.
  const padTop = parseFloat(getComputedStyle(scroller).paddingTop) || 0;
  // MOBILE sub-header: the headings are un-stuck and the section-tab strip lives
  // OUTSIDE the scroller (in #app-config-subnav, above it), so nothing pins over
  // the content — a section is "current" once its heading reaches the scroller's
  // top edge. (Desktop stacks sticky headings, so a section engages once its
  // heading reaches its own pinned offset.)
  const mnav = sec.classList.contains('ac-mnav-on') ? _navFor(id) : null;
  let active = 0;
  heads.forEach((h, i) => {
    // Mobile: the section's `-28px` top margin cancels the scroller's 28px top
    // padding, so a heading rests flush at the scroller's border-box top — the
    // "current" line is 0 (heading reached the top edge, just under the sub-header).
    // Desktop: a heading engages at its own pinned offset (padTop + its inline top,
    // which for the first heading is padTop + (−padTop) = 0).
    const line = mnav ? 0 : (padTop + (parseFloat(h.style.top) || 0));
    const rTop = h.getBoundingClientRect().top - sTop;
    if (rTop <= line + 1) active = i;               // scrolled into/past this section
  });
  // Scroll extremes win over top-engagement: pinned at the very top → first tab;
  // scrolled to the very bottom → last tab.
  if (scroller.scrollTop <= 1) active = 0;
  else if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 1) active = heads.length - 1;

  _setActive(id, active);
}

// One shared scroll handler updates whichever participating section is active
// (the others bail in _updateActive on the `.active` guard).
function _onScroll() {
  if (_raf) return;                                  // active-highlight only; rAF pause is harmless
  _raf = requestAnimationFrame(() => {
    _raf = 0;
    _registered.forEach((id) => _updateActive(id));
    // Also update unified carousel active state
    if (_unifiedCarousel) {
      _updateUnifiedActive();
    }
  });
}

// Clicking anywhere INSIDE a section's content activates that section's tab too
// (not just scrolling into it). We map the click's Y to the deepest heading that
// starts above it — the section the pointer landed in. Clicks on a heading itself
// are left to _onHeadClick (which scrolls + selects). All sections are always visible
// on the single scrollable page, so no .active check needed.
function _onScrollerClick(e) {
  if (e.target.closest && e.target.closest('.ac-category-summary')) return;
  _registered.forEach((id) => {
    const sec = _section(id);
    if (!sec) return;
    const heads = _heads(id);
    if (heads.length < 2) return;
    const y = e.clientY;
    let idx = 0;
    heads.forEach((h, i) => { if (h.getBoundingClientRect().top <= y) idx = i; });
    _lock[id] = null;                 // an explicit pick supersedes any in-flight scroll lock
    _setActive(id, idx);
  });
}

function _onResize() {
  // _syncMode picks carousel (mobile, opted-in) vs sticky-stack (everything else)
  // and re-measures — so crossing the mobile/desktop breakpoint flips the mode.
  _registered.forEach((id) => _syncMode(id));
}

// ─────────────────────────────────────────────────────────────────────────
// ── MOBILE heading carousel (opt-in via initStickyNav(id,{mobileCarousel})) ─
// ─────────────────────────────────────────────────────────────────────────

function _headLabel(h) {
  const t = h.querySelector('.ac-category-title');
  return ((t ? t.textContent : h.textContent) || '').trim();
}

// Build (or refresh) the chip strip for a section and insert it at the top of the
// scroll block. Idempotent: skips the rebuild when the heading labels are
// unchanged so a resize storm / re-show doesn't thrash the DOM.
function _buildCarousel(id) {
  const sec = _section(id);
  const host = _navHost();
  const heads = _heads(id);
  if (!sec || !host || !heads.length) return null;
  const sig = heads.map(_headLabel).join('|');
  let nav = _navFor(id);
  if (nav && nav.dataset.sig === sig) return nav;
  if (!nav) {
    nav = document.createElement('div');
    nav.className = 'ac-mnav';
    nav.dataset.section = id;         // ties this sub-header strip to its section
    nav.innerHTML = '<div class="ac-mnav-strip" role="tablist"></div>';
    // Park it in the sub-header host (outside the scroller), NOT inside the section.
    host.appendChild(nav);
    const strip0 = nav.querySelector('.ac-mnav-strip');
    strip0.addEventListener('scroll', () => _updateMnavEdges(nav), { passive: true });
  }
  const strip = nav.querySelector('.ac-mnav-strip');
  strip.innerHTML = '';
  heads.forEach((h, i) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'ac-mnav-chip';
    chip.dataset.idx = String(i);
    // The section's leading Lucide icon, if it has one (always-open headings do;
    // the chevron-prefixed collapsible categories don't → label-only chip). Only
    // match real Lucide icons, never a bare chevron svg.
    const icon = h.querySelector(':scope > .lucide-icon, :scope > [data-lucide]');
    if (icon) { const c = icon.cloneNode(true); c.removeAttribute('style'); c.classList.add('ac-mnav-ico'); chip.appendChild(c); }
    const lbl = document.createElement('span');
    lbl.className = 'ac-mnav-label';
    lbl.textContent = _headLabel(h);
    chip.appendChild(lbl);
    chip.addEventListener('click', () => _scrollToMobile(id, i));
    strip.appendChild(chip);
  });
  nav.dataset.sig = sig;
  nav.dataset.activeIdx = '';
  if (window.lucide) { try { window.lucide.createIcons({ nodes: Array.from(strip.querySelectorAll('[data-lucide]:not(.lucide)')) }); } catch (_) {} }
  _updateMnavEdges(nav);
  return nav;
}

function _removeCarousel(id) {
  const nav = _navFor(id);
  if (nav) nav.remove();
}

// Toggle the edge-fade mask classes from the strip's live scroll position (mask,
// not a coloured gradient — see the `.ac-mnav` block in app3.css).
function _updateMnavEdges(nav) {
  const strip = nav.querySelector('.ac-mnav-strip');
  if (!strip) return;
  const overflow = strip.scrollWidth - strip.clientWidth > 1;
  nav.classList.toggle('can-left', overflow && strip.scrollLeft > 1);
  nav.classList.toggle('can-right', overflow && strip.scrollLeft < strip.scrollWidth - strip.clientWidth - 1);
}

// Nudge a chip into view inside the strip WITHOUT moving the page scroll.
function _scrollChipIntoView(strip, chip) {
  if (!strip || !chip) return;
  const s = strip.getBoundingClientRect(), c = chip.getBoundingClientRect();
  if (c.left < s.left + 8) strip.scrollBy({ left: c.left - s.left - 14, behavior: 'smooth' });
  else if (c.right > s.right - 8) strip.scrollBy({ left: c.right - s.right + 14, behavior: 'smooth' });
}

// Select tab i immediately (so clicking a tab activates that tab — even a short
// trailing section the page can't scroll to the engagement line) and smooth-
// scroll to `target`, holding the selection until the scroll settles there.
function _selectAndScroll(id, i, target) {
  const scroller = _scroller();
  if (!scroller) return;
  const max = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  const t = Math.max(0, Math.min(Math.round(target), max));
  _lock[id] = { idx: i, target: t, deadline: Date.now() + 800 };
  _setActive(id, i);
  scroller.scrollTo({ top: t, behavior: 'smooth' });
}

// Jump the page scroll so section i's heading rests flush at the scroller's top
// edge — i.e. directly under the sub-header (which sits OUTSIDE the scroller, so
// it doesn't overlap the content and needs no height offset). The mobile analogue
// of _scrollTo, which lands under the pinned heading stack.
function _scrollToMobile(id, i) {
  const groups = _groups(id);
  const scroller = _scroller();
  const g = groups[i];
  if (!g || !scroller) return;
  const head = g.querySelector(':scope > .ac-category-summary');
  const ref = head || g.querySelector(':scope > .ac-category-body') || g;
  const sRect = scroller.getBoundingClientRect();
  const rRect = ref.getBoundingClientRect();
  // flowTop = the heading's offset from the scroller's border-box top. Scrolling
  // there lands it at rTop 0 (the section's -28px margin cancels the scroller's
  // top padding, so the top edge is the visible content top).
  const flowTop = scroller.scrollTop + (rRect.top - sRect.top);
  _selectAndScroll(id, i, flowTop);
}

// Use unified carousel for all sections (replaces the main tab bar).
// Called on init, on every section show, and on resize.
function _syncMode(id) {
  // Always use unified carousel - it shows all headings from all sections
  _buildUnifiedCarousel();
  
  // Clear any inline sticky offsets since we're using the carousel for navigation
  _registered.forEach(sid => {
    const heads = _heads(sid);
    heads.forEach((h) => { 
      h.style.top = ''; 
      h.style.bottom = ''; 
    });
  });
}

// Click-to-jump: scroll so group i's heading rests right under the stack of
// headings pinned above it. We measure off the (never-sticky) BODY so the
// target is the heading's true flow position even when it's currently pinned.
function _scrollTo(id, i) {
  const groups = _groups(id);
  const scroller = _scroller();
  const g = groups[i];
  if (!g || !scroller) return;
  const head = g.querySelector(':scope > .ac-category-summary');
  const body = g.querySelector(':scope > .ac-category-body') || head;
  const hH = head ? Math.round(head.getBoundingClientRect().height) : 0;
  const top = head ? (parseFloat(head.style.top) || 0) : 0;
  const sRect = scroller.getBoundingClientRect();
  const bRect = body.getBoundingClientRect();
  const padTop = parseFloat(getComputedStyle(scroller).paddingTop) || 0;
  const bodyFlowTop = scroller.scrollTop + (bRect.top - sRect.top);
  // Land the heading flush UNDER the stack pinned above it: its pinned position
  // is padTop + its own top offset, so subtract both here.
  const target = bodyFlowTop - hH - top - padTop;
  _selectAndScroll(id, i, target);
}

// Handle a click on a heading. `.ac-collapsible` group: toggle its `.ac-open`
// class and jump to it when OPENING (per the chosen "expand + jump" behaviour).
// Always-open group: just jump.
function _onHeadClick(e, id, i, group) {
  // Never hijack a click on a control/link/badge that sits in the heading.
  if (e.target.closest('a, button, input, select')) return;
  if (group.classList.contains('ac-collapsible')) {
    const wasOpen = group.classList.contains('ac-open');
    if (wasOpen) {
      group.classList.remove('ac-open');
      _recalc(id);
    } else {
      group.classList.add('ac-open');
      // Let the body reflow before measuring its flow position, then jump.
      requestAnimationFrame(() => { _recalc(id); _scrollTo(id, i); });
    }
  } else {
    _scrollTo(id, i);
  }
}

/**
 * Install (or re-measure) the sticky navigator for one App Config section.
 * Safe to call repeatedly — head click wiring + the global scroll/resize
 * listeners are guarded so they attach once.
 *
 * @param {string} id  the section suffix, e.g. 'app-settings', 'database'.
 * @param {object} [opts]
 * @param {boolean} [opts.mobileCarousel] opt this section into the mobile heading
 *        carousel (horizontal chip strip instead of stacked sticky headings).
 */
export function initStickyNav(id, opts = {}) {
  const groups = _groups(id);
  const heads = _heads(id);
  if (!heads.length) return;
  _registered.add(id);
  if (opts.mobileCarousel) _carousel.add(id);
  groups.forEach((g, i) => {
    const h = g.querySelector(':scope > .ac-category-summary');
    if (!h) return;
    h.classList.add('ac-cursor-pointer');               // overrides any inline cursor:default
    h.setAttribute('title', g.classList.contains('ac-collapsible') ? 'Open / jump to this section' : 'Jump to this section');
    if (!h.__navhWired) {
      h.addEventListener('click', (e) => _onHeadClick(e, id, i, g));
      h.__navhWired = true;
    }
  });
  const scroller = _scroller();
  if (scroller && !_wiredScrollers.has(scroller)) {
    scroller.addEventListener('scroll', _onScroll, { passive: true });
    scroller.addEventListener('click', _onScrollerClick);
    _wiredScrollers.add(scroller);
  }
  if (!_resizeWired) {
    window.addEventListener('resize', _onResize);
    _resizeWired = true;
  }
  _syncMode(id);   // mobile carousel (opted-in) or desktop sticky stack, then measure
}
