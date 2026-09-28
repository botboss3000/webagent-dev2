'use strict';

/**
 * Live, client-side search for the Settings area (Instances → Settings).
 *
 * Search is PAGE-GRANULAR: typing a query OPENS every section page that
 * matches — results appear as opened pages, stacked one after another — it
 * does not merely filter a list to click. A page matches when its title,
 * description, keywords (from app-pages.json) OR its live content
 * (headings, rows, injected data) match the query.
 *
 * The matcher is ported from the old app-config-search.js (same stemming,
 * thesaurus families, phrase aliases and typo tolerance), so search behaviour
 * is unchanged from the old single-page App Config — only the DOM target
 * changed from rows to pages.
 */

const _STOP_WORDS = new Set([
  'a', 'an', 'and', 'are', 'at', 'for', 'from', 'how', 'in', 'is', 'it',
  'my', 'of', 'on', 'or', 'the', 'to', 'with',
]);

const _RELATED_FAMILIES = [
  ['agent', 'assistant', 'bot', 'webagent'],
  ['app', 'application', 'workspace'],
  ['attachment', 'file', 'document', 'upload'],
  ['auth', 'authentication', 'login', 'signin', 'oauth'],
  ['automation', 'job', 'schedule', 'scheduler', 'task', 'timer'],
  ['boot', 'launch', 'startup', 'start'],
  ['chat', 'conversation', 'message', 'messaging'],
  ['connector', 'integration', 'provider', 'service'],
  ['credential', 'key', 'password', 'secret', 'token'],
  ['database', 'data', 'storage'],
  ['delete', 'erase', 'purge', 'remove'],
  ['design', 'appearance', 'color', 'colour', 'style', 'theme'],
  ['extension', 'addon', 'plugin'],
  ['guard', 'protection', 'safety', 'security'],
  ['llm', 'model'],
  ['market', 'marketplace', 'store'],
  ['memory', 'context', 'recall'],
  ['network', 'remote', 'domain', 'dns'],
  ['person', 'account', 'member', 'user'],
  ['social', 'community', 'media'],
  ['speech', 'dictation', 'microphone', 'voice'],
];

const _RELATED = new Map();
for (const family of _RELATED_FAMILIES) {
  const words = new Set(family);
  for (const word of family) _RELATED.set(word, words);
}

const _PHRASE_ALIASES = [
  [/\bsign[\s-]+in\b/g, 'signin'],
  [/\blog[\s-]+in\b/g, 'login'],
  [/\bdata[\s-]+base\b/g, 'database'],
  [/\bstart[\s-]+up\b/g, 'startup'],
  [/\bweb[\s-]+agent\b/g, 'webagent'],
  [/\bsocial[\s-]+media\b/g, 'social'],
];

function _normalize(value) {
  let text = String(value || '')
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase();
  for (const [pattern, replacement] of _PHRASE_ALIASES) {
    text = text.replace(pattern, replacement);
  }
  return text.replace(/[^a-z0-9]+/g, ' ').trim();
}

function _stem(word) {
  if (word.length > 5 && word.endsWith('ies')) return word.slice(0, -3) + 'y';
  if (word.length > 5 && word.endsWith('ing')) {
    let base = word.slice(0, -3);
    if (base.length > 3 && base.at(-1) === base.at(-2)) base = base.slice(0, -1);
    return base;
  }
  if (word.length > 4 && word.endsWith('ed')) {
    let base = word.slice(0, -2);
    if (base.length > 3 && base.at(-1) === base.at(-2)) base = base.slice(0, -1);
    return base;
  }
  if (word.length > 3 && word.endsWith('s') && !word.endsWith('ss')) {
    return word.slice(0, -1);
  }
  return word;
}

function _tokens(value, { query = false } = {}) {
  const words = _normalize(value).split(' ').filter(Boolean);
  return query ? words.filter(word => !_STOP_WORDS.has(word)) : words;
}

function _distanceWithin(left, right, limit) {
  if (Math.abs(left.length - right.length) > limit) return false;
  let previous = Array.from({ length: right.length + 1 }, (_, i) => i);
  for (let i = 1; i <= left.length; i++) {
    const current = [i];
    let rowMin = current[0];
    for (let j = 1; j <= right.length; j++) {
      current[j] = Math.min(
        current[j - 1] + 1,
        previous[j] + 1,
        previous[j - 1] + (left[i - 1] === right[j - 1] ? 0 : 1),
      );
      rowMin = Math.min(rowMin, current[j]);
    }
    if (rowMin > limit) return false;
    previous = current;
  }
  return previous[right.length] <= limit;
}

function _wordMatches(queryWord, candidateWord) {
  const queryStem = _stem(queryWord);
  const candidateStem = _stem(candidateWord);
  if (queryWord === candidateWord || queryStem === candidateStem) return true;

  const related = _RELATED.get(queryWord) || _RELATED.get(queryStem);
  if (related && (related.has(candidateWord) || related.has(candidateStem))) return true;

  // Avoid prefix/typo matching for tiny words so "cat" never matches "chat".
  if (Math.min(queryStem.length, candidateStem.length) >= 4
      && (queryStem.startsWith(candidateStem) || candidateStem.startsWith(queryStem))) {
    return true;
  }

  const typoLimit = queryStem.length >= 8 ? 2 : (queryStem.length >= 4 ? 1 : 0);
  return typoLimit > 0 && _distanceWithin(queryStem, candidateStem, typoLimit);
}

/** Exported for focused tests and reuse by other config surfaces. */
export function matchesRelatedText(query, candidate) {
  const queryText = _normalize(query);
  if (!queryText) return true;
  const queryWords = _tokens(queryText, { query: true });
  if (!queryWords.length) return true;

  const candidateText = _normalize(candidate);
  if (queryWords.length > 1 && candidateText.includes(queryWords.join(' '))) return true;
  const candidateWords = _tokens(candidateText);
  return queryWords.every(queryWord =>
    candidateWords.some(candidateWord => _wordMatches(queryWord, candidateWord)));
}

function _setHidden(element, hidden) {
  if (!element) return;
  if (hidden) element.setAttribute('data-ac-search-hidden', 'true');
  else element.removeAttribute('data-ac-search-hidden');
}

/**
 * @param {object} opts
 * @param {HTMLElement} opts.root        — the scroller (#app-config-content)
 * @param {HTMLElement} [opts.empty]     — "No match" element
 * @param {HTMLElement} [opts.list]      — landing list element (hidden while searching)
 * @param {HTMLElement} [opts.topbar]    — page chrome (hidden while searching)
 * @param {function}    [opts.getPages]  — () => [.settings-page elements]
 * @param {Map}         [opts.pageMeta]  — section id -> {title, description, keywords}
 * @param {function}    [opts.onDeactivate] — called when search clears (restore surface)
 * @returns {{filter:(query:string)=>number, clear:()=>void, destroy:()=>void}}
 */
export function createSettingsSearch({ root, empty = null, list = null, topbar = null,
                                       getPages = null, pageMeta = new Map(), onDeactivate = null } = {}) {
  let currentQuery = '';
  let frame = null;
  // Page text cache: element -> { text, dirty }. A plain Map (not WeakMap —
  // WeakMap has no .values(), and the page roots live for the whole session).
  const textCache = new Map();

  function pageText(page) {
    const id = (page.id || '').replace('settings-page-', '');
    const meta = pageMeta.get(id);
    const cached = textCache.get(page);
    if (cached && !cached.dirty) return cached.text;
    const metaText = [meta?.title, meta?.description, ...(meta?.keywords || [])]
      .filter(Boolean).join(' ');
    const text = metaText + ' ' + (page.textContent || '');
    textCache.set(page, { text, dirty: false });
    return text;
  }

  function markDirty() {
    for (const value of textCache.values()) value.dirty = true;
  }

  function clear() {
    currentQuery = '';
    root?.classList.remove('ac-searching');
    root?.querySelectorAll('[data-ac-search-hidden]')
      .forEach(element => element.removeAttribute('data-ac-search-hidden'));
    // Pages are hidden by default (list view); restore that state.
    const pages = typeof getPages === 'function'
      ? getPages()
      : [...(root ? root.querySelectorAll('.settings-page') : [])];
    for (const page of pages) page.hidden = true;
    if (empty) {
      empty.hidden = true;
      empty.textContent = '';
    }
    if (typeof onDeactivate === 'function') onDeactivate();
  }

  function filter(rawQuery) {
    if (!root) return 0;
    const query = String(rawQuery || '').trim();
    // Keep the page completely unchanged until the user has entered four
    // meaningful characters. Short fragments are too noisy to filter well.
    if (_normalize(query).replace(/\s/g, '').length < 4) {
      clear();
      return 0;
    }
    currentQuery = query;

    root.classList.add('ac-searching');
    if (list) list.hidden = true;
    if (topbar) topbar.hidden = true;

    const pages = typeof getPages === 'function'
      ? getPages()
      : [...root.querySelectorAll('.settings-page')];
    let count = 0;
    for (const page of pages) {
      const hit = matchesRelatedText(query, pageText(page));
      // Matched pages OPEN (results appear as opened pages, stacked); the
      // hidden attribute is the source of truth for page visibility.
      page.hidden = !hit;
      _setHidden(page, !hit);
      if (hit) count++;
    }

    if (empty) {
      empty.hidden = count > 0;
      const message = `No match for "${query}"`;
      if (empty.textContent !== message) empty.textContent = message;
    }
    return count;
  }

  const observer = root && typeof MutationObserver !== 'undefined'
    ? new MutationObserver(() => {
        if (!currentQuery || frame) return;
        frame = requestAnimationFrame(() => {
          frame = null;
          markDirty();
          filter(currentQuery);
        });
      })
    : null;
  observer?.observe(root, { childList: true, subtree: true });

  return {
    filter,
    clear,
    destroy() {
      observer?.disconnect();
      if (frame) cancelAnimationFrame(frame);
      clear();
    },
  };
}
