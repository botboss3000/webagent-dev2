'use strict';

// ── SHARED-FILE-TEXT-VIEWER ────────────────────────────────────────────────
// Styles → ui/shared/css/app1.css (".wa-file-viewer-*" block).
//
// Every agent file card (save_file / attach_file) offers Download. Files that
// can be rendered as TEXT — Markdown, plain text, JSON, CSV, YAML, source code —
// additionally offer "View file", which opens this in-place read-only editor:
// a line-number gutter, syntax highlighting from the page-global Prism build,
// and Copy / Download actions.
//
// Why a fetch instead of a plain <a href>: the card's URL is an opaque
// workspace content grant, and grants are only honoured with the app's Bearer
// token (see app/content_access.py). Opening the URL directly would 404 and
// would also leak the grant into browser history.
//
// Read-only on purpose. The file is a workspace artifact the agent owns; edits
// belong to the agent (or the Files tool), not to a transcript popup, so this
// viewer never writes back.

import { icon } from './icons.js';
import { authHeaders } from './left-login.js';
import { copyText } from './clipboard.js';

// ── What counts as viewable text ───────────────────────────────────────────
// An ALLOWLIST, deliberately: offering "View file" for something we can't
// decode as text would just produce mojibake.
const TEXT_EXTENSIONS = new Set([
  'md', 'markdown', 'mdx', 'txt', 'text', 'log', 'rst',
  'json', 'jsonl', 'ndjson', 'json5', 'geojson',
  'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf', 'env', 'properties',
  'csv', 'tsv', 'xml', 'html', 'htm',
  'css', 'scss', 'sass', 'less',
  'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx', 'vue', 'svelte',
  'py', 'rb', 'go', 'rs', 'java', 'kt', 'swift', 'php', 'cs',
  'c', 'h', 'cc', 'cpp', 'hpp', 'm', 'mm', 'lua', 'pl', 'r', 'scala',
  'sh', 'bash', 'zsh', 'fish', 'ps1', 'bat', 'cmd',
  'sql', 'graphql', 'gql', 'proto', 'diff', 'patch',
  'srt', 'vtt', 'tex', 'bib', 'gitignore', 'dockerfile', 'makefile',
]);

const TEXT_MIME_TYPES = new Set([
  'application/json', 'application/ld+json', 'application/x-ndjson', 'application/x-jsonlines',
  'application/x-yaml', 'application/yaml', 'application/toml', 'application/xml',
  'application/javascript', 'application/x-javascript', 'application/ecmascript',
  'application/x-sh', 'application/sql', 'application/graphql', 'application/x-httpd-php',
]);

// Container/binary formats that must never be offered as text even when the
// extension looks textual (e.g. image/svg+xml keeps its image-card treatment).
const NON_TEXT_MIME_PREFIXES = ['image/', 'audio/', 'video/', 'font/', 'model/'];
const NON_TEXT_MIME_TYPES = new Set([
  'application/pdf', 'application/zip', 'application/gzip', 'application/x-tar',
  'application/x-7z-compressed', 'application/x-rar-compressed',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/msword', 'application/vnd.ms-excel', 'application/vnd.ms-powerpoint',
  'application/octet-stream-binary',
]);

// Prism language id per extension. Unknown extensions fall back to plain
// monospace (no highlighting) rather than a wrong grammar.
const PRISM_LANGUAGE_BY_EXTENSION = {
  md: 'markdown', markdown: 'markdown', mdx: 'markdown',
  json: 'json', jsonl: 'json', ndjson: 'json', json5: 'json', geojson: 'json',
  yaml: 'yaml', yml: 'yaml', toml: 'toml', ini: 'ini', cfg: 'ini', conf: 'ini',
  properties: 'properties', env: 'bash',
  csv: 'csv', tsv: 'csv',
  xml: 'markup', html: 'markup', htm: 'markup',
  css: 'css', scss: 'scss', sass: 'sass', less: 'less',
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'jsx',
  ts: 'typescript', tsx: 'tsx', vue: 'markup', svelte: 'markup',
  py: 'python', rb: 'ruby', go: 'go', rs: 'rust', java: 'java', kt: 'kotlin',
  swift: 'swift', php: 'php', cs: 'csharp', c: 'c', h: 'c', cc: 'cpp',
  cpp: 'cpp', hpp: 'cpp', lua: 'lua', pl: 'perl', r: 'r', scala: 'scala',
  sh: 'bash', bash: 'bash', zsh: 'bash', fish: 'bash', ps1: 'powershell',
  bat: 'batch', cmd: 'batch',
  sql: 'sql', graphql: 'graphql', gql: 'graphql', diff: 'diff', patch: 'diff',
  dockerfile: 'docker', makefile: 'makefile',
};

const MAX_VIEW_BYTES = 4 * 1024 * 1024;          // refuse to render beyond 4 MB of text
const MAX_HIGHLIGHT_CHARACTERS = 200 * 1024;     // above this, show plain text (no Prism pass)

/** Lowercased extension of a filename ('' when there is none). */
function _extensionOf(filename) {
  const name = String(filename || '').toLowerCase();
  const dot = name.lastIndexOf('.');
  if (dot < 0 || dot === name.length - 1) return '';
  return name.slice(dot + 1);
}

/** Normalised MIME type without parameters. */
function _normalizeMime(mimeType) {
  return String(mimeType || '').toLowerCase().split(';')[0].trim();
}

/**
 * Can this file be shown in the text viewer?
 * Text MIME types and known text-ish extensions qualify; images, audio, video,
 * PDFs and other containers never do.
 * @param {string} filename
 * @param {string} [mimeType]
 * @returns {boolean}
 */
export function isViewableTextFile(filename, mimeType) {
  const mime = _normalizeMime(mimeType);
  if (NON_TEXT_MIME_TYPES.has(mime)) return false;
  if (NON_TEXT_MIME_PREFIXES.some((prefix) => mime.startsWith(prefix))) return false;
  if (mime.startsWith('text/') || TEXT_MIME_TYPES.has(mime)) return true;
  return TEXT_EXTENSIONS.has(_extensionOf(filename));
}

// ── Decoding ───────────────────────────────────────────────────────────────

/**
 * Decode bytes as UTF-8 text, or return null when the payload is really binary
 * (NUL byte, or a high ratio of undecodable sequences — i.e. not this encoding).
 */
function _decodeUtf8(bytes) {
  for (let i = 0; i < bytes.length; i += 1) {
    if (bytes[i] === 0) return null;
  }
  let text;
  try {
    text = new TextDecoder('utf-8', { fatal: false }).decode(bytes);
  } catch (_) {
    return null;
  }
  const undecodable = (text.match(/\uFFFD/g) || []).length;
  if (undecodable > 0 && undecodable / Math.max(1, text.length) > 0.02) return null;
  return text;
}

/** Gutter text: 1..lineCount, right-padded to a common width and newline-joined. */
function _lineNumbers(lineCount) {
  const width = String(lineCount).length;
  const out = new Array(lineCount);
  for (let i = 0; i < lineCount; i += 1) out[i] = String(i + 1).padStart(width, ' ');
  return out.join('\n');
}

function _countLines(text) {
  if (!text) return 1;
  let lines = 1;
  for (let i = 0; i < text.length; i += 1) {
    if (text.charCodeAt(i) === 10) lines += 1;
  }
  // A trailing newline closes the last line instead of opening an empty one.
  if (text.charCodeAt(text.length - 1) === 10) lines -= 1;
  return Math.max(1, lines);
}

// ── Overlay ────────────────────────────────────────────────────────────────

let _viewer = null;        // single reused instance
let _loadToken = 0;        // guards against a slow fetch from a previous file
let _openText = '';        // decoded text of the file on screen (for Copy / Download)
let _openFilename = '';
let _lastFocus = null;     // element to hand focus back to on close
let _saveHandler = null;    // optional async callback for editable repository text

function _ensureViewer() {
  if (_viewer) return _viewer;

  const overlay = document.createElement('div');
  overlay.className = 'wa-file-viewer-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', 'File viewer');
  overlay.innerHTML = `
    <div class="wa-file-viewer">
      <header class="wa-file-viewer-head">
        <span class="wa-file-viewer-icon"></span>
        <div class="wa-file-viewer-titles">
          <span class="wa-file-viewer-name"></span>
          <span class="wa-file-viewer-meta"></span>
        </div>
        <button type="button" class="wa-file-viewer-close" aria-label="Close file viewer"></button>
      </header>
      <div class="wa-file-viewer-body">
        <div class="wa-file-viewer-gutter" aria-hidden="true"></div>
        <pre class="wa-file-viewer-code"><code></code></pre>
        <textarea class="wa-file-viewer-editor" aria-label="File editor" spellcheck="false"></textarea>
        <div class="wa-file-viewer-state" role="status"></div>
      </div>
      <footer class="wa-file-viewer-foot">
        <button type="button" class="wa-file-viewer-copy"></button>
        <button type="button" class="wa-file-viewer-download"></button>
      </footer>
    </div>`;

  const refs = {
    overlay,
    panel: overlay.querySelector('.wa-file-viewer'),
    iconSlot: overlay.querySelector('.wa-file-viewer-icon'),
    name: overlay.querySelector('.wa-file-viewer-name'),
    meta: overlay.querySelector('.wa-file-viewer-meta'),
    gutter: overlay.querySelector('.wa-file-viewer-gutter'),
    code: overlay.querySelector('.wa-file-viewer-code code'),
    editor: overlay.querySelector('.wa-file-viewer-editor'),
    state: overlay.querySelector('.wa-file-viewer-state'),
    copyBtn: overlay.querySelector('.wa-file-viewer-copy'),
    saveBtn: document.createElement('button'),
    downloadBtn: overlay.querySelector('.wa-file-viewer-download'),
    closeBtn: overlay.querySelector('.wa-file-viewer-close'),
  };

  refs.iconSlot.innerHTML = icon('file-text', { size: '16px' });
  refs.closeBtn.innerHTML = icon('x', { size: '16px' });
  refs.copyBtn.innerHTML = icon('copy', { size: '14px' }) + '<span>Copy</span>';
  refs.saveBtn.type = 'button';
  refs.saveBtn.className = 'wa-file-viewer-save';
  refs.saveBtn.hidden = true;
  refs.saveBtn.innerHTML = icon('save', { size: '14px' }) + '<span>Save</span>';
  refs.downloadBtn.innerHTML = icon('download', { size: '14px' }) + '<span>Download</span>';
  refs.downloadBtn.before(refs.saveBtn);

  refs.closeBtn.addEventListener('click', () => closeFileTextViewer());
  // Backdrop click closes; clicks inside the panel do not.
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) closeFileTextViewer();
  });

  refs.copyBtn.addEventListener('click', async () => {
    if (!_openText) return;
    const label = refs.copyBtn.querySelector('span');
    try {
      await copyText(_openText);
      refs.copyBtn.classList.add('is-copied');
      if (label) label.textContent = 'Copied';
    } catch (_) {
      if (label) label.textContent = 'Copy failed';
    }
    setTimeout(() => {
      refs.copyBtn.classList.remove('is-copied');
      if (label) label.textContent = 'Copy';
    }, 1400);
  });

  refs.saveBtn.addEventListener('click', async () => {
    if (!_saveHandler) return;
    const label = refs.saveBtn.querySelector('span');
    refs.saveBtn.disabled = true;
    if (label) label.textContent = 'Saving…';
    try {
      const next = String(refs.editor.value || '');
      await _saveHandler(next);
      _openText = next;
      if (label) label.textContent = 'Saved';
      setTimeout(() => { if (label) label.textContent = 'Save'; }, 1400);
    } catch (error) {
      if (label) label.textContent = 'Save failed';
      refs.state.textContent = error && error.message ? error.message : 'Could not save this file.';
    } finally {
      refs.saveBtn.disabled = false;
    }
  });

  refs.editor.addEventListener('input', () => {
    _openText = refs.editor.value;
    refs.gutter.textContent = _lineNumbers(_countLines(_openText));
  });

  refs.downloadBtn.addEventListener('click', () => {
    if (!_openText) return;
    const blobUrl = URL.createObjectURL(new Blob([_openText], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = blobUrl;
    link.download = _openFilename || 'file.txt';
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
  });

  // Escape closes the viewer and never reaches the chat underneath it.
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !overlay.classList.contains('open')) return;
    event.preventDefault();
    event.stopPropagation();
    closeFileTextViewer();
  }, true);

  document.body.appendChild(overlay);
  _viewer = refs;
  return refs;
}

function _setState(refs, message) {
  refs.panel.classList.add('is-notice');
  refs.state.textContent = message;
  refs.gutter.textContent = '';
  refs.code.textContent = '';
  refs.code.className = '';
}

/**
 * Close the viewer (no-op when it isn't open) and return focus to the control
 * that opened it.
 */
export function closeFileTextViewer() {
  const refs = _viewer;
  if (!refs) return;
  _loadToken += 1;                 // cancel any in-flight render
  _openText = '';
  _openFilename = '';
  _saveHandler = null;
  refs.overlay.classList.remove('open');
  refs.panel.classList.remove('is-notice', 'is-editable');
  refs.saveBtn.hidden = true;
  refs.editor.value = '';
  refs.gutter.textContent = '';
  refs.code.textContent = '';
  refs.code.className = '';
  refs.state.textContent = '';
  const focusBack = _lastFocus;
  _lastFocus = null;
  if (focusBack && typeof focusBack.focus === 'function' && document.contains(focusBack)) {
    try { focusBack.focus(); } catch (_) {}
  }
}

function _renderText(refs, text, filename) {
  const lines = _countLines(text);
  refs.panel.classList.remove('is-notice');
  refs.state.textContent = '';
  refs.gutter.textContent = _lineNumbers(lines);
  const extension = _extensionOf(filename);
  const language = PRISM_LANGUAGE_BY_EXTENSION[extension];
  // Prism owns the markup: set the raw text first, then let it highlight (the
  // page-global autoloader pulls any missing grammar from ./ui/vendor/prismjs).
  refs.code.className = language ? `language-${language}` : '';
  refs.code.textContent = text;
  refs.editor.value = text;
  if (language && window.Prism && typeof window.Prism.highlightElement === 'function'
      && text.length <= MAX_HIGHLIGHT_CHARACTERS) {
    try { window.Prism.highlightElement(refs.code); } catch (_) { /* plain text is fine */ }
  }
  refs.panel.querySelector('.wa-file-viewer-body').scrollTop = 0;
}

/**
 * Open the read-only text viewer for a workspace file.
 * @param {object} o
 * @param {string} o.url        authenticated content-grant URL
 * @param {string} o.filename   display name (also drives syntax highlighting)
 * @param {string} [o.metaLabel] pre-formatted meta line, e.g. "Markdown · 6 KB"
 */
export function openFileTextViewer({ url, filename, metaLabel = '' } = {}) {
  if (!url || !filename) return;
  const refs = _ensureViewer();

  _lastFocus = document.activeElement;
  const token = (_loadToken += 1);
  _openText = '';
  _openFilename = String(filename);

  refs.name.textContent = String(filename);
  refs.name.title = String(filename);
  refs.meta.textContent = metaLabel;
  _setState(refs, 'Loading file…');
  refs.overlay.classList.add('open');
  refs.closeBtn.focus();

  void (async () => {
    try {
      const response = await fetch(url, { headers: authHeaders() });
      if (!response.ok) throw new Error(`File unavailable (${response.status})`);
      const buffer = await response.arrayBuffer();
      if (token !== _loadToken) return;
      if (buffer.byteLength > MAX_VIEW_BYTES) {
        _setState(refs, 'This file is too large to display here. Use Download to open it locally.');
        return;
      }
      const text = _decodeUtf8(new Uint8Array(buffer));
      if (text === null) {
        _setState(refs, "This file isn't text, so it can't be shown here. Use Download instead.");
        return;
      }
      if (token !== _loadToken) return;
      _openText = text;
      if (!text.trim()) {
        _setState(refs, 'This file is empty.');
        return;
      }
      _renderText(refs, text, filename);
    } catch (error) {
      if (token !== _loadToken) return;
      _setState(refs, `Could not open this file. ${error && error.message ? error.message : ''}`.trim());
    }
  })();
}


/**
 * Open the same shared attachment text surface with an in-memory document. When
 * `onSave` is supplied it becomes an editor; ordinary file attachments continue
 * to use `openFileTextViewer` and remain read-only.
 */
export function openTextEditor({ filename, text = '', metaLabel = '', onSave = null } = {}) {
  if (!filename) return;
  const refs = _ensureViewer();
  _lastFocus = document.activeElement;
  _loadToken += 1;
  _openText = String(text);
  _openFilename = String(filename);
  _saveHandler = typeof onSave === 'function' ? onSave : null;

  refs.name.textContent = _openFilename;
  refs.name.title = _openFilename;
  refs.meta.textContent = metaLabel;
  refs.panel.classList.toggle('is-editable', !!_saveHandler);
  refs.saveBtn.hidden = !_saveHandler;
  refs.saveBtn.disabled = false;
  const saveLabel = refs.saveBtn.querySelector('span');
  if (saveLabel) saveLabel.textContent = 'Save';
  _renderText(refs, _openText, _openFilename);
  refs.overlay.classList.add('open');
  (_saveHandler ? refs.editor : refs.closeBtn).focus();
}
