'use strict';

// ── Collapsible JSON tree renderer ──
// Shared between stream view and database viewer

import { _esc } from './dom-utils.js';

// Keys whose string values are candidates for markdown rendering
// (still gated by looksLikeMarkdown() so plain strings stay quoted).
const CONTENT_KEYS = new Set(['content', 'description', 'text', 'message']);

// Detect whether a string contains markdown formatting worth rendering.
// Trips on: headings (`# `), bold (`**x**`), fenced code (```), bullet/ordered
// list items at line start, or markdown links. Plain prose returns false.
function looksLikeMarkdown(s) {
  if (!s || typeof s !== 'string') return false;
  // Heading at line start
  if (/(^|\n)#{1,6} \S/.test(s)) return true;
  // Bold (**x**) — non-greedy, requires non-asterisk inside
  if (/\*\*[^\s*][^*]*\*\*/.test(s)) return true;
  // Fenced code block
  if (/```/.test(s)) return true;
  // Bullet list item at line start
  if (/(^|\n)- \S/.test(s)) return true;
  // Ordered list item at line start
  if (/(^|\n)\d+\.\s+\S/.test(s)) return true;
  // Markdown link [text](url)
  if (/\[[^\]]+\]\([^)]+\)/.test(s)) return true;
  return false;
}

/**
 * Render a string containing markdown into styled HTML.
 * Handles: headers, bold, italic, inline code, code blocks, links, lists.
 */
function renderContentMarkdown(text) {
  if (!text) return '<span class="json-str">""</span>';

  // Escape HTML entities first
  let html = _esc(text);

  // Code blocks (```...```) — before other transformations
  html = html.replace(/```([\s\S]*?)```/g, function(match, code) {
    return '<pre><code>' + code + '</code></pre>';
  });

  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Headers
  html = html.replace(/^###### (.+)$/gm, '<h6>$1</h6>');
  html = html.replace(/^##### (.+)$/gm, '<h5>$1</h5>');
  html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // Bold
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

  // Italic
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');

  // Links [text](url)
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  // Unordered list items
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
  // Wrap consecutive <li> in <ul>
  html = html.replace(/((?:<li>.*?<\/li>(?:\n|$))+)/g, '<ul>$1</ul>');

  // Ordered list items
  html = html.replace(/^\d+\.\s+(.+)$/gm, '<li>$1</li>');
  // Wrap consecutive <li> in <ol>
  html = html.replace(/((?:<li>.*?<\/li>(?:\n|$))+)/g, '<ol>$1</ol>');

  // Double newlines → paragraph break
  html = html.replace(/\n\n+/g, '</p><p>');

  // Single newlines → <br>
  html = html.replace(/\n/g, '<br>');

  // Wrap in <p> if there's paragraph-worthy content and no block wrapper yet
  // but avoid wrapping elements that are already block-level (pre, h1-h6, ul, ol)
  const startsWithBlock = /^\s*(<(pre|h[1-6]|ul|ol|div))/i.test(html);
  if (!startsWithBlock) {
    html = '<p>' + html + '</p>';
  }

  return '<div class="json-content-md">' + html + '</div>';
}

const MAX_JSON_DEPTH = 20;

function renderJsonTree(data, indent, keyName, opts) {
  if (indent === undefined) indent = 0;
  if (!opts) opts = { renderMarkdown: true };
  if (indent > MAX_JSON_DEPTH) return '<span class="json-str">"... (truncated)"</span>';
  const padInner = '  '.repeat(indent + 1);
  if (data === null) return '<span class="json-null">null</span>';
  if (data === undefined) return '<span class="json-null">undefined</span>';
  if (typeof data === 'boolean') return `<span class="json-bool">${data}</span>`;
  if (typeof data === 'number') return `<span class="json-num">${data}</span>`;
  if (typeof data === 'string') {
    // Render as markdown if the key matches, markdown is enabled, and the
    // string actually contains markdown markers. Plain prose stays quoted.
    if (opts.renderMarkdown && keyName && CONTENT_KEYS.has(keyName) && data.length > 0 && looksLikeMarkdown(data)) {
      return renderContentMarkdown(data);
    }
    return `<span class="json-str">"${_esc(data)}"</span>`;
  }

  if (Array.isArray(data)) {
    if (data.length === 0) return '<span class="json-punc">[</span><span class="json-punc">]</span>';
    const items = data.map((v, i) => {
      // Arrays don't pass a keyName for their values
      const val = renderJsonTree(v, indent + 1, undefined, opts);
      const comma = i < data.length - 1 ? '<span class="json-punc">,</span>' : '';
      return `<div class="json-line">${padInner}${val}${comma}</div>`;
    }).join('');
    return `<span class="json-block">\n<span class="json-toggle">▼</span><span class="json-punc">[</span><span class="json-children">\n${items}\n${'  '.repeat(indent)}</span><span class="json-punc">]</span>\n</span>`;
  }

  if (typeof data === 'object') {
    const keys = Object.keys(data);
    if (keys.length === 0) return '<span class="json-punc">{</span><span class="json-punc">}</span>';
    const items = keys.map((k, i) => {
      const key = `<span class="json-key">"${_esc(k)}"</span>`;
      // Pass the key name so string values under known content keys render as markdown
      const val = renderJsonTree(data[k], indent + 1, k, opts);
      const comma = i < keys.length - 1 ? '<span class="json-punc">,</span>' : '';
      return `<div class="json-line">${padInner}${key}<span class="json-punc">:</span> ${val}${comma}</div>`;
    }).join('');
    return `<span class="json-block">\n<span class="json-toggle">▼</span><span class="json-punc">{</span><span class="json-children">\n${items}\n${'  '.repeat(indent)}</span><span class="json-punc">}</span>\n</span>`;
  }

  return _esc(String(data));
}

/**
 * Parse raw string as JSON and return collapsible HTML tree.
 * Returns null if not valid JSON.
 * opts.renderMarkdown (default true): when false, string values under
 * content/description/text/message keys are shown as quoted JSON strings
 * instead of being rendered as markdown.
 */
export function formatJsonAsHtml(raw, opts) {
  if (!opts) opts = { renderMarkdown: true };
  try {
    const parsed = JSON.parse(raw);
    return `<div class="json-root">${renderJsonTree(parsed, 0, undefined, opts)}</div>`;
  } catch (e) {
    return null;
  }
}

/**
 * Attach a single click listener for .json-toggle elements.
 * Safe to call multiple times — uses event delegation on document.
 */
export function initJsonToggle() {
  // Only attach once
  if (document._jsonToggleAttached) return;
  document._jsonToggleAttached = true;

  document.addEventListener('click', (e) => {
    const toggle = e.target.closest('.json-toggle');
    if (!toggle) return;
    const block = toggle.closest('.json-block');
    if (!block) return;
    const wasCollapsed = block.classList.toggle('json-collapsed');
    toggle.textContent = wasCollapsed ? '▶' : '▼';
  });
}
