'use strict';

/**
 * Attachments module — file picker, voice recording, drag & drop,
 * preview chips, and inline rendering in chat bubbles.
 *
 * Depends on the global `app` object from state.js.
 * Must be loaded after state.js.
 */

import { app } from './state.js';
import { apiPath } from './config.js';
import { icon } from './icons.js';
import { authHeaders } from './left-login.js';
import { isViewableTextFile, openFileTextViewer } from './file-text-viewer.js';
import { putAttachment, getObjectUrl, deleteAttachment as idbDelete } from './attachments-idb.js';
import * as mediaCache from './media-cache.js';
import {
  RecordedAudioDictationController,
  VoiceDictationController,
} from './voice-dictation.js';
import { beginVoiceTurn, initVoiceConversation } from './voice-conversation.js';
import { isStreamingVoiceSupported } from './voice-stream.js';

// Active server-side attachment backend. Synced opportunistically by the
// Chat Attachments card's bootstrap fetch (see ui/shared/js/data-management.js).
// We also do a best-effort fetch the first time an upload happens so this
// works even before the user opens App Configuration → Data Management.
let _backendCache = null;
let _backendFetched = false;

function _appendIconText(parent, iconName, text, size = '12px') {
  const iconWrapper = document.createElement('span');
  iconWrapper.innerHTML = icon(iconName, { size });
  parent.appendChild(iconWrapper);
  parent.appendChild(document.createTextNode(` ${String(text ?? '')}`));
}

async function _resolveBackend() {
  if (_backendCache) return _backendCache;
  try {
    if (window.__webagentAttachmentBackend) {
      _backendCache = window.__webagentAttachmentBackend;
      return _backendCache;
    }
  } catch {}
  if (_backendFetched) return 'local';
  _backendFetched = true;
  try {
    const uid = (typeof localStorage !== 'undefined' && localStorage.getItem('auth_user_id')) || '';
    const res = await fetch(apiPath('/admin/storage/attachments/status?requesting_user_id=' + encodeURIComponent(uid)));
    if (res.ok) {
      const data = await res.json();
      _backendCache = data.mode || 'local';
      try { window.__webagentAttachmentBackend = _backendCache; } catch {}
    }
  } catch {
    // Non-admin or unauthenticated — the endpoint will 403/401. Treat the
    // default as 'local' (the server still owns the actual decision).
  }
  return _backendCache || 'local';
}

// ── State ──────────────────────────────────────────────────────────────────

/** Attachments ready to send with next message */
const pendingAttachments = [];
/** Exposed so sendMessage can render attachments into the user bubble. */
export function getPendingAttachments() { return pendingAttachments; }

// ── Agent-generated file attachment cards ───────────────────────────────────
// save_file and attach_file are the only trusted sources for this compact card.
// The model cannot create one by emitting look-alike markdown/JSON: callers pass
// an actual persisted tool-call entry, and this function requires the explicit result kind.
const FILE_ATTACHMENT_TOOLS = new Set(['save_file', 'attach_file']);
function _formatFileSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return '';
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let scaled = value / 1024;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) { scaled /= 1024; unit += 1; }
  return `${scaled >= 10 || unit === 0 ? scaled.toFixed(0) : scaled.toFixed(1)} ${units[unit]}`;
}

function _fileTypeLabel(mimeType, filename) {
  const mime = String(mimeType || '').toLowerCase();
  if (mime === 'application/pdf') return 'PDF';
  if (mime.startsWith('image/')) return 'Image';
  if (mime.startsWith('audio/')) return 'Audio';
  if (mime.startsWith('video/')) return 'Video';
  if (mime.startsWith('text/')) return 'Text';
  const extension = String(filename || '').split('.').pop();
  return extension && extension !== filename ? extension.toUpperCase() : 'File';
}

// SVG and other document-like image formats remain download-only. Raster files
// can safely be decoded into a protected in-memory Blob for preview/new-tab use.
function _isPreviewableImage(mimeType) {
  return new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif'])
    .has(String(mimeType || '').toLowerCase());
}

export function renderSavedFileAttachmentCard(toolCall) {
  if (!toolCall || !FILE_ATTACHMENT_TOOLS.has(toolCall.tool) || !toolCall.result) return null;
  let data;
  try { data = typeof toolCall.result === 'string' ? JSON.parse(toolCall.result) : toolCall.result; }
  catch (_) { return null; }
  if (!data || data.kind !== 'file_attachment' || data.status !== 'ok'
      || typeof data.filename !== 'string' || typeof data.url !== 'string'
      || !data.url.startsWith('/api/v1/content/')) return null;

  const card = document.createElement('section');
  card.className = 'chat-file-attachment';
  if (toolCall.toolCallId) card.dataset.fileAttachmentCallId = String(toolCall.toolCallId);
  const previewableImage = _isPreviewableImage(data.mime_type);
  let leadingVisual;
  if (previewableImage) {
    card.classList.add('chat-image-attachment');
    const preview = document.createElement('a');
    preview.className = 'chat-image-attachment-preview is-loading';
    preview.target = '_blank';
    preview.rel = 'noopener';
    preview.setAttribute('aria-label', `Open full-size ${data.filename}`);
    preview.title = 'Loading image preview…';
    preview.addEventListener('click', (event) => {
      if (!preview.href) event.preventDefault();
    });
    const image = document.createElement('img');
    image.alt = `Preview of ${data.filename}`;
    image.decoding = 'async';
    preview.appendChild(image);
    leadingVisual = preview;

    // A protected content grant is not usable by a plain <img> tag because it
    // needs the app's Bearer token. Fetch it once, then use a local Blob URL for
    // both the inline thumbnail and the full-size new-tab link.
    void (async () => {
      try {
        const response = await fetch(data.url, { headers: authHeaders() });
        if (!response.ok) throw new Error(`Image preview failed (${response.status})`);
        const blob = await response.blob();
        if (!blob.type.startsWith('image/')) throw new Error('Unexpected preview content type');
        const blobUrl = URL.createObjectURL(blob);
        image.src = blobUrl;
        preview.href = blobUrl;
        preview.classList.remove('is-loading');
        preview.title = `Open full-size ${data.filename}`;
        window.addEventListener('pagehide', () => URL.revokeObjectURL(blobUrl), { once: true });
      } catch (_) {
        preview.classList.add('is-unavailable');
        preview.title = 'Image preview unavailable. Use Download instead.';
      }
    })();
  } else {
    const fileIcon = document.createElement('span');
    fileIcon.className = 'chat-file-attachment-icon';
    fileIcon.innerHTML = icon('file-down', { size: '18px' });
    leadingVisual = fileIcon;
  }
  const details = document.createElement('div'); details.className = 'chat-file-attachment-details';
  const name = document.createElement('span'); name.className = 'chat-file-attachment-name'; name.textContent = data.filename;
  const meta = document.createElement('span'); meta.className = 'chat-file-attachment-meta';
  meta.textContent = [_fileTypeLabel(data.mime_type, data.filename), _formatFileSize(data.bytes)].filter(Boolean).join(' · ');
  details.append(name, meta);
  const actions = document.createElement('div');
  actions.className = 'chat-file-attachment-actions';

  // Text-renderable files (md / txt / json / csv / source …) get a "View file"
  // action that opens the shared read-only text viewer in place — same
  // authenticated fetch path as Download (see ui/shared/js/file-text-viewer.js).
  if (isViewableTextFile(data.filename, data.mime_type)) {
    const viewButton = document.createElement('button');
    viewButton.type = 'button'; viewButton.className = 'chat-file-attachment-view';
    viewButton.innerHTML = icon('eye', { size: '15px' }) + '<span>View file</span>';
    viewButton.addEventListener('click', () => openFileTextViewer({
      url: data.url,
      filename: data.filename,
      metaLabel: meta.textContent,
    }));
    actions.appendChild(viewButton);
  }

  const button = document.createElement('button');
  button.type = 'button'; button.className = 'chat-file-attachment-download';
  button.innerHTML = icon('download', { size: '15px' }) + '<span>Download</span>';
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    try {
      const response = await fetch(data.url, { headers: authHeaders() });
      if (!response.ok) throw new Error(`Download failed (${response.status})`);
      const blobUrl = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = blobUrl;
      link.download = data.filename;
      link.style.display = 'none';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    } catch (_) {
      button.title = 'Download failed. Please try again.';
    } finally { button.disabled = false; }
  });
  actions.appendChild(button);
  card.append(leadingVisual, details, actions);
  return card;
}


// ── Init ───────────────────────────────────────────────────────────────────

export function initAttachments() {
  const attachBtn = document.getElementById('chat-attach-btn');
  const fileInput = document.getElementById('chat-file-input');
  const voiceBtn = document.getElementById('chat-voice-btn');
  const previewBar = document.getElementById('chat-preview-bar');
  const inputArea = document.getElementById('chat-input-area');

  if (!attachBtn || !fileInput) return;

  // Composer accepted-format policy. Resolved lazily from the hidden file
  // input's data-accepted-formats (published by attach.js from chat_ui.json) so
  // the picker, drop and paste paths all honour the same configured list.
  const uploadOpts = {
    get acceptedFormats() { return _acceptedFormatsForInput(fileInput); },
  };

  // File picker click. The config-driven composer may turn the plus button into
  // a small action menu; that menu's Attach files action owns the picker click.
  attachBtn.addEventListener('click', () => {
    if (attachBtn.dataset.popover === 'true') return;
    fileInput.click();
  });

  // File selected
  fileInput.addEventListener('change', async () => {
    const files = Array.from(fileInput.files);
    fileInput.value = ''; // reset so same file can be re-selected
    for (const file of files) {
      await uploadAndPreview(file, uploadOpts);
    }
  });

  // The main composer uses the speech capture stack as an ephemeral voice
  // conversation. Secondary pills still use plain dictation.
  if (voiceBtn) {
    initVoiceConversation();
    voiceBtn.title = 'Voice conversation';
    voiceBtn.setAttribute('aria-label', 'Voice conversation');
    voiceBtn.addEventListener('click', () => beginVoiceTurn({
      button: voiceBtn,
      input: document.getElementById('chat-input'),
      captureActive: isSpeechDictationActive,
      toggleCapture: (options) => startSpeechDictation(
        voiceBtn, document.getElementById('chat-input'), options,
      ),
    }));
    const pill = document.getElementById('chat-input-row');
    if (pill) {
      if (!isVoiceInputSupported()) {
        pill.classList.add('no-voice');
      } else {
        // Async refinement: if the admin disabled LLM dictation AND this
        // browser has no native SpeechRecognition, startSpeechDictation would
        // error on click — hide the mic in that case too.
        _loadVoicePolicy().then(policy => {
          const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
          if (policy.llm_enabled === false && !SR) pill.classList.add('no-voice');
        });
      }
    }
  }

  // Main chat pill: paste + drop scoped to the chat field (composer pill).
  // Other pills (Gen UI, Agents, admin) wire themselves via wireChatPillUploads.
  wireChatPillUploads(
    document.getElementById('chat-input-row'),
    document.getElementById('chat-input'),
    uploadOpts,
  );

  // Expose attachment_ids to chat send flow
  app.getPendingAttachmentIds = () => pendingAttachments.map(a => a.attachment_id);
  app.clearPendingAttachments = clearPendingAttachments;
}

// ── Upload ─────────────────────────────────────────────────────────────────

// Turn a server error body into readable text. FastAPI/Pydantic validation
// errors arrive as `detail: [{ msg, loc, ... }, ...]`; stringifying that list
// yields "[object Object],[object Object]", which is what the user used to see
// on a failed paste. Pull the human messages out instead.
function _serverErrText(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const msgs = detail.map(d => (d && d.msg) ? d.msg : '').filter(Boolean);
    return msgs.length ? msgs.join('; ') : fallback;
  }
  if (typeof detail === 'object' && detail.msg) return detail.msg;
  return fallback;
}

// Attachments are stored against the active user + session. A paste or drop can
// fire before the chat has identity behind it — a fresh page that hasn't
// finished booting, or a secondary pill whose chat hasn't started a session.
// Without a user + session the upload is rejected by the server and the failure
// is invisible. Mirror the send flow: if we have a user but no session, spin up
// the shared WebAgent session; with no user at all the upload can't be
// attributed and must be refused.
async function _ensureSessionForUpload() {
  if (app.currentUserId && app.currentSessionId) return true;
  if (!app.currentUserId) return false;
  if (typeof app.startWebagentSession === 'function') {
    try { await app.startWebagentSession(); } catch { /* fall through to recheck */ }
  }
  return !!(app.currentUserId && app.currentSessionId);
}

// ── Accepted-format policy ──────────────────────────────────────────────────
// The composer's allowed file formats are authored in data/config/chat_ui.json
// (active_footer/idle_footer → chat_pill → controls → attach → accepted_formats)
// and threaded here via opts.acceptedFormats. Tokens follow <input accept="…">
// syntax: a MIME wildcard ("image/*"), an exact MIME type ("application/pdf"),
// or a file extension (".md" or "md"). An empty/absent list means "no
// restriction", so every other pill (Gen UI, Agents, admin) is unaffected.
const _FORMAT_LABELS = {
  'image/*': 'Images', 'audio/*': 'Audio', 'video/*': 'Video',
  'application/pdf': 'PDF', 'application/json': 'JSON', 'text/csv': 'CSV',
  'text/plain': 'Text', 'text/markdown': 'Markdown',
  '.pdf': 'PDF', '.json': 'JSON', '.csv': 'CSV', '.txt': 'Text', '.md': 'Markdown',
};

export function acceptedFormatsLabel(tokens) {
  if (!Array.isArray(tokens)) return '';
  const seen = new Set();
  const labels = [];
  for (const raw of tokens) {
    const token = String(raw || '').trim();
    if (!token) continue;
    const key = token.toLowerCase();
    let label = _FORMAT_LABELS[key];
    if (!label) {
      label = key.endsWith('/*') ? key.slice(0, -2) : token.replace(/^\./, '');
      label = label.charAt(0).toUpperCase() + label.slice(1);
    }
    const dedupe = label.toLowerCase();
    if (seen.has(dedupe)) continue;
    seen.add(dedupe);
    labels.push(label);
  }
  return labels.join(', ');
}

export function matchesAcceptedFormats(file, tokens) {
  if (!Array.isArray(tokens) || tokens.length === 0) return true;
  const name = String(file?.name || '').toLowerCase();
  const type = String(file?.type || '').toLowerCase();
  return tokens.some((raw) => {
    const token = String(raw || '').trim().toLowerCase();
    if (!token) return false;
    if (token.startsWith('.')) return name.endsWith(token);
    if (token.endsWith('/*')) return type.startsWith(token.slice(0, -1));
    if (token.includes('/')) return type === token;
    return name.endsWith('.' + token);
  });
}

// Resolve the list the attach element published on the composer's hidden file
// input (attach.js sets data-accepted-formats from chat_ui.json).
function _acceptedFormatsForInput(inputEl) {
  const raw = inputEl?.dataset?.acceptedFormats;
  if (!raw) return null;
  const list = raw.split(',').map((token) => token.trim()).filter(Boolean);
  return list.length ? list : null;
}

export async function uploadAndPreview(file, opts = {}) {
  if (!file) return;

  if (opts.acceptedFormats && opts.acceptedFormats.length && !matchesAcceptedFormats(file, opts.acceptedFormats)) {
    alert(`Unsupported file type: ${file.name}\nAccepted formats: ${acceptedFormatsLabel(opts.acceptedFormats)}.`);
    return;
  }

  const maxSize = 25 * 1024 * 1024; // 25MB
  if (file.size > maxSize) {
    alert(`File too large: ${file.name} (${(file.size / 1024 / 1024).toFixed(1)}MB). Max 25MB.`);
    return;
  }

  const previewBar = opts.previewBar || document.getElementById('chat-preview-bar');
  const targetPending = opts.pending || pendingAttachments;
  const onChange = opts.onChange;
  if (!previewBar) return;

  // Create local blob URL for instant preview — no server upload until send.
  const objectUrl = URL.createObjectURL(file);

  const entry = {
    _file: file,
    _objectUrl: objectUrl,
    url: objectUrl,
    original_name: file.name,
    mime_type: file.type || 'application/octet-stream',
    size_bytes: file.size,
    storage_provider: 'local',
  };

  const chip = document.createElement('span');
  chip.className = 'chat-attachment-pill clickable';
  chip.title = 'Click to expand';
  chip.innerHTML = '';
  // Click the chip body (but not the ✕) to expand it in the viewer.
  chip.addEventListener('click', (e) => {
    if (e.target.closest('.chat-attachment-remove')) return;
    openAttachmentViewer(entry);
  });

  // Thumbnail for images
  if (entry.mime_type.startsWith('image/')) {
    const img = document.createElement('img');
    img.src = objectUrl;
    img.alt = entry.original_name;
    img.className = 'chat-attachment-thumb';
    chip.appendChild(img);
  } else if (entry.mime_type.startsWith('audio/')) {
    _appendIconText(chip, 'mic', entry.original_name);
  } else {
    _appendIconText(chip, 'paperclip', entry.original_name);
  }

  // Remove button — revokes local URL, no server call needed.
  const removeBtn = document.createElement('button');
  removeBtn.className = 'chat-attachment-remove';
  removeBtn.innerHTML = icon('x', { size: '11px' });
  removeBtn.title = 'Remove attachment';
  removeBtn.addEventListener('click', () => {
    chip.remove();
    URL.revokeObjectURL(objectUrl);
    const idx = targetPending.indexOf(entry);
    if (idx >= 0) targetPending.splice(idx, 1);
    if (targetPending.length === 0) previewBar.style.display = 'none';
    if (onChange) onChange(targetPending);
  });
  chip.appendChild(removeBtn);

  previewBar.appendChild(chip);
  previewBar.style.display = 'flex';

  targetPending.push(entry);
  if (onChange) onChange(targetPending);
}

// ── Shared paste + drag/drop wiring for any chat pill ──────────────────────
// Both drop and paste target the pill row (= the chat field), so each pill
// handles only its own clipboard/drag input — a paste lands in the focused
// pill, never a different page's pill.
// Caller can route uploads to a custom preview bar / pending list via opts.

function _dragHasFiles(e) {
  if (!e.dataTransfer) return false;
  const types = e.dataTransfer.types;
  if (!types) return false;
  for (let i = 0; i < types.length; i++) {
    if (types[i] === 'Files') return true;
  }
  return false;
}

// Extract image(s)/file(s) from a paste event and upload them to the given
// pill. Routes uploads via opts (custom preview bar / pending list).
async function _processImagePaste(e, opts) {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;

  // Phase 1: direct File items (Chrome/Edge/Safari)
  const files = [];
  for (const item of items) {
    if (item.kind === 'file') {
      const f = item.getAsFile();
      if (f) files.push(f);
    }
  }
  if (files.length > 0) {
    e.preventDefault();
    for (const file of files) await uploadAndPreview(file, opts);
    return;
  }

  // Phase 2: Firefox exposes a pasted image as string items (an image/* item,
  // or an <img src="data:…"> inside text/html) rather than as a File. Only take
  // over the paste when there's a genuine image signal — a string item whose
  // type is image/*. Crucially we must NOT preventDefault merely because a
  // text/html item is present: ordinary rich text copied from a web page also
  // carries text/html, and cancelling that here would swallow normal text
  // pastes into the chat box (the very thing the user types). Plain/rich text
  // with no image therefore falls through to the browser's native paste.
  const hasImageType = Array.from(items).some(
    i => i.kind === 'string' && i.type.startsWith('image/')
  );

  if (!hasImageType) return;  // no image to capture → let native paste insert text

  // Prevent default so Firefox doesn't show its error message
  e.preventDefault();

  // Try async Clipboard API first (works in Firefox with permissions)
  try {
    const clipboardItems = await navigator.clipboard.read();
    const imageFiles = [];
    for (const ci of clipboardItems) {
      for (const type of ci.types) {
        if (type.startsWith('image/')) {
          const blob = await ci.getType(type);
          const ext = type.split('/')[1] || 'png';
          imageFiles.push(new File(
            [blob],
            'clipboard-' + Date.now() + '.' + ext,
            { type }
          ));
        }
      }
    }
    if (imageFiles.length > 0) {
      for (const file of imageFiles) await uploadAndPreview(file, opts);
      return;
    }
  } catch {
    // Clipboard API not available
  }

  // Fallback: extract data URL from HTML string items
  for (const item of items) {
    if (item.kind === 'string' && item.type === 'text/html') {
      item.getAsString(function(html) {
        var re = /src\s*=\s*"(data:image\/[^"]+)"/;
        var m = html.match(re);
        if (m && m[1]) {
          fetch(m[1]).then(function(r) { return r.blob(); }).then(function(blob) {
            var ext = blob.type.split('/')[1] || 'png';
            var f = new File([blob], 'clipboard-' + Date.now() + '.' + ext, { type: blob.type });
            uploadAndPreview(f, opts);
          });
        }
      });
    }
  }
}

export function wireChatPillUploads(rowEl, inputEl, opts = {}) {
  if (rowEl && !rowEl.dataset.chatPillUploadsWired) {
    rowEl.dataset.chatPillUploadsWired = '1';
    let counter = 0;
    rowEl.addEventListener('dragenter', (e) => {
      if (!_dragHasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      counter++;
      if (counter === 1) rowEl.classList.add('drag-over');
    });
    rowEl.addEventListener('dragover', (e) => {
      if (!_dragHasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    });
    rowEl.addEventListener('dragleave', (e) => {
      if (!_dragHasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      counter--;
      if (counter <= 0) { counter = 0; rowEl.classList.remove('drag-over'); }
    });
    rowEl.addEventListener('drop', async (e) => {
      if (!_dragHasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      counter = 0;
      rowEl.classList.remove('drag-over');
      const files = Array.from(e.dataTransfer.files);
      for (const file of files) await uploadAndPreview(file, opts);
    });

    // Paste is scoped to this pill: a paste event fires on the focused element
    // and bubbles from the text field (or any focused control inside the pill)
    // up to the row. Binding here — rather than on the document — means the
    // paste always lands in the pill the user is working in, never a different
    // page's pill. The focused pill only.
    rowEl.addEventListener('paste', (e) => _processImagePaste(e, opts));
  }
}

function clearPendingAttachments() {
  // Revoke blob URLs before clearing.
  for (const entry of pendingAttachments) {
    if (entry._objectUrl) URL.revokeObjectURL(entry._objectUrl);
  }
  pendingAttachments.length = 0;
  const previewBar = document.getElementById('chat-preview-bar');
  if (previewBar) {
    previewBar.innerHTML = '';
    previewBar.style.display = 'none';
  }
}

// ── Voice Dictation (Web Speech API) ───────────────────────────────────────
// A fresh recognizer is used for every session. Each result event replaces the
// in-progress transcript from the recognizer's full authoritative result list,
// including revisions at indexes that were previously interim.
function _renderDictationButton(btn, state) {
  if (!btn) return;
  const recording = state === 'recording';
  const busy = state === 'starting' || state === 'stopping' || state === 'transcribing';
  const active = recording || busy;
  btn.innerHTML = icon(recording ? 'circle-stop' : active ? 'loader-circle' : 'mic', { size: '18px' });
  btn.title = recording
    ? 'Stop dictation'
    : state === 'starting'
      ? 'Starting microphone…'
      : state === 'stopping'
        ? 'Finishing recording…'
        : state === 'transcribing'
          ? 'Transcribing…'
          : 'Voice dictation';
  btn.classList.toggle('recording', recording);
  btn.classList.toggle('transcribing', busy);
  btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  btn.setAttribute('aria-label', btn.title);
  btn.disabled = busy;
  btn.closest('.chat-pill')?.classList.toggle('dictating', active);
}

function _dictationError(error) {
  if (error === 'unsupported') {
    alert('Voice dictation is not supported in this browser.');
  } else if (error === 'not-allowed' || error === 'service-not-allowed') {
    alert('Microphone access denied. Allow it in the browser settings to use voice dictation.');
  } else if (error === 'audio-capture') {
    alert('No microphone was found. Connect a microphone and try again.');
  } else if (error === 'network') {
    alert('Speech recognition is temporarily unavailable. Check your connection and try again.');
  } else if (error === 'start-failed' || error === 'recording-failed') {
    alert('Voice dictation could not start. Check microphone permissions and try again.');
  } else if (error === 'empty-recording') {
    alert('No audio was captured. Please try again.');
  } else if (error === 'transcription-failed') {
    alert('The recording could not be transcribed. Check your configured AI provider and try again.');
  } else if (error === 'insecure-context') {
    alert('Microphone recording requires HTTPS or localhost. Open WebAgent over a secure address and try again.');
  } else if (error === 'llm-disabled') {
    alert('LLM voice dictation is disabled by the administrator, and this browser has no built-in speech recognition.');
  }
  // no-speech and aborted are normal endings and do not need an alert.
}

const dictationController = new VoiceDictationController({
  createRecognition: () => {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    return SR ? new SR() : null;
  },
  language: () => navigator.language || 'en-US',
  renderButton: _renderDictationButton,
  onError: _dictationError,
});

function _createAudioRecorder(stream) {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/ogg;codecs=opus',
    'audio/mp4',
    'audio/webm',
  ];
  const mimeType = candidates.find(type => MediaRecorder.isTypeSupported?.(type));
  return mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
}

const recordedDictationController = new RecordedAudioDictationController({
  getStream: () => navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
    },
  }),
  createRecorder: _createAudioRecorder,
  transcribe: async (blob) => {
    const form = new FormData();
    const subtype = (blob.type.split('/')[1] || 'webm').split(';')[0];
    form.append('file', blob, `dictation.${subtype}`);
    form.append('user_id', app.currentUserId || 'admin');
    const language = (navigator.language || '').split('-')[0];
    if (language) form.append('language', language);

    const response = await fetch(apiPath('/api/v1/transcribe'), {
      method: 'POST',
      body: form,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(_serverErrText(body.detail, 'Transcription failed'));
    }
    const body = await response.json();
    return body.text || '';
  },
  renderButton: _renderDictationButton,
  onError: _dictationError,
});

let _voicePolicy = null;
let _voicePolicyLoadedAt = 0;

async function _loadVoicePolicy() {
  if (window.__waVoiceDictationPolicy) {
    _voicePolicy = window.__waVoiceDictationPolicy;
    return _voicePolicy;
  }
  if (_voicePolicy && Date.now() - _voicePolicyLoadedAt < 15_000) return _voicePolicy;
  try {
    const response = await window.__fetchUiConfig({ headers: { ...authHeaders() } });
    if (response.ok) {
      const data = await response.json();
      _voicePolicy = {
        llm_enabled: data.voice_dictation_llm_enabled !== false,
        mode: data.voice_dictation_mode === 'llm_only' ? 'llm_only' : 'browser_then_llm',
      };
      _voicePolicyLoadedAt = Date.now();
    }
  } catch {}
  return _voicePolicy || { llm_enabled: true, mode: 'browser_then_llm' };
}

// Whether ANY voice input path can work in this browser/context. Mirrors the
// runtime matrix in startSpeechDictation: native SpeechRecognition, or the
// MediaRecorder + getUserMedia recorder fallback that transcribes via the
// server LLM. Both require a secure context (HTTPS or localhost).
export function isVoiceInputSupported() {
  if (!window.isSecureContext) return false;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const canRecord = !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  return !!(SR || canRecord || isStreamingVoiceSupported());
}

// `inputEl` is optional — secondary pills (e.g. the ability-table search/chat
// pill) pass their own textarea so dictation lands in THAT pill, not the main
// composer. Defaults to the main chat input when omitted.
export function isSpeechDictationActive() {
  return dictationController.active || recordedDictationController.active;
}

export async function startSpeechDictation(btn, inputEl, options = {}) {
  const input = inputEl || document.getElementById('chat-input');
  if (!input) return;
  const policy = await _loadVoicePolicy();
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const canRecord = !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  const useLlm = policy.llm_enabled && (
    policy.mode === 'llm_only' || !SR
  );

  if (!useLlm && SR) {
    return dictationController.toggle(btn, input, options);
  } else if (useLlm && canRecord) {
    return recordedDictationController.toggle(btn, input, options);
  } else if (!policy.llm_enabled) {
    _dictationError('llm-disabled');
  } else if (!window.isSecureContext) {
    _dictationError('insecure-context');
  } else {
    _dictationError('unsupported');
  }
  return false;
}

// ── Upload-on-send helpers ─────────────────────────────────────────────────

// Upload a single file to the server (normal multipart path).
// Returns the server response { attachment_id, url, original_name, … }.
async function _uploadFileToServer(file) {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('user_id', app.currentUserId);
  formData.append('session_id', app.currentSessionId);
  const res = await fetch(apiPath('/api/v1/upload'), { method: 'POST', body: formData });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(_serverErrText(err.detail, 'Upload failed'));
  }
  return await res.json();
}

// Upload all locally-pending files to the server and return their server
// attachment_ids. Replaces each pending entry with the server response
// so callers can reference .url / .attachment_id after.
export async function uploadPendingAttachments(pending) {
  const ids = [];
  for (const entry of pending) {
    if (entry._file) {
      try {
        const data = await _uploadFileToServer(entry._file);
        Object.assign(entry, data);
        delete entry._file;
        if (data.attachment_id) ids.push(data.attachment_id);
        if (entry._objectUrl) {
          URL.revokeObjectURL(entry._objectUrl);
          entry._objectUrl = null;
        }
      } catch (err) {
        console.warn('Failed to upload attachment:', err);
        // Keep the local File and preview URL intact so the user can retry.
        // Sending text without the selected attachment is never a valid
        // fallback: reject the preflight and let the composer surface it.
        throw err;
      }
    } else if (entry.attachment_id) {
      // Already has a server id (legacy or already-uploaded entry).
      ids.push(entry.attachment_id);
    }
  }
  return ids;
}

// Called by chat.js before sending — upload pending files, then add
// attachment_ids to the message payload.
export async function addAttachmentsToMessage(msgObj) {
  if (pendingAttachments.length > 0) {
    msgObj.attachment_ids = await uploadPendingAttachments(pendingAttachments);
  }
  return msgObj;
}

// ── Render attachments in chat bubbles ─────────────────────────────────────

function _isBrowserStored(att) {
  if (att.storage_provider === 'browser') return true;
  const p = att.storage_path || '';
  return p.startsWith('indexeddb://');
}

// Small, repeatedly-rendered media (images, audio) is worth holding in the RAM
// cache; large/opaque/streamed types (video, PDF, other) are left to stream from
// their source so we never pull a whole video into memory.
function _cacheable(att) {
  const m = att.mime_type || '';
  return m.startsWith('image/') || m.startsWith('audio/');
}

function _serverUrl(att) {
  return att.url || '';
}

function _resolveAttachmentUrl(att) {
  // Returns a thenable; null indicates "no resolvable URL". For browser-stored
  // attachments we mint an object URL from IndexedDB (the bytes live on the
  // device). Server-backed cacheable media is served through the RAM speed layer
  // — instant on repeat renders — falling back to the raw URL on a miss/failure.
  if (_isBrowserStored(att) && att.attachment_id) {
    return getObjectUrl(att.attachment_id);
  }
  const url = _serverUrl(att);
  if (url && att.attachment_id && _cacheable(att)) {
    return mediaCache.fetchUrl(att.attachment_id, url);
  }
  return Promise.resolve(url);
}

// Resolve a DOWNSCALED thumbnail for an image attachment (list/preview views),
// generated once and held in RAM. Browser-stored images fall back to their full
// IndexedDB object URL (already local); everything else tries the RAM thumbnail.
function _resolveThumbUrl(att) {
  if (_isBrowserStored(att)) return _resolveAttachmentUrl(att);
  const url = _serverUrl(att);
  if (url && att.attachment_id && (att.mime_type || '').startsWith('image/')) {
    return mediaCache.thumbUrl(att.attachment_id, url, att.mime_type);
  }
  return Promise.resolve(url);
}

function _setSrcAsync(el, attrName, att, missingClass, resolver) {
  // For browser-stored bytes the URL is async; render with an empty src and
  // swap it in when IDB resolves. For server-stored bytes we still go through
  // the same path for consistency. `resolver` lets callers pick the thumbnail
  // path (_resolveThumbUrl) instead of the full-size default.
  (resolver || _resolveAttachmentUrl)(att).then(url => {
    if (url) {
      el[attrName] = url;
    } else if (missingClass) {
      el.classList.add(missingClass);
      el.alt = `(missing) ${att.original_name || ''}`;
    }
  }).catch(() => {
    if (missingClass) el.classList.add(missingClass);
  });
}

// ── Full-screen attachment viewer (lightbox) ───────────────────────────────
// Shared "expand" used by both the composer preview chips and the attachments
// rendered in the chat history. Clicking a preview opens a dimmed full-screen
// overlay showing the file large: images full-size, audio/video with players,
// PDFs embedded, and text/markdown/CSV/JSON content read inline. Only one is
// open at a time; it closes on Esc, a backdrop click, or the ✕ button.
let _viewerOpen = false;

export function openAttachmentViewer(att) {
  if (!att || _viewerOpen) return;
  _viewerOpen = true;
  const mime = att.mime_type || '';
  const name = att.original_name || 'attachment';

  const backdrop = document.createElement('div');
  backdrop.className = 'attachment-viewer-backdrop';

  const frame = document.createElement('div');
  frame.className = 'attachment-viewer-frame';

  const head = document.createElement('div');
  head.className = 'attachment-viewer-head';
  const title = document.createElement('span');
  title.className = 'attachment-viewer-title';
  title.textContent = name;
  const closeBtn = document.createElement('button');
  closeBtn.className = 'attachment-viewer-close';
  closeBtn.innerHTML = icon('x', { size: '18px' });
  closeBtn.title = 'Close (Esc)';
  head.appendChild(title);
  head.appendChild(closeBtn);

  const body = document.createElement('div');
  body.className = 'attachment-viewer-body';
  body.innerHTML = `<div class="attachment-viewer-loading">${icon('loader-2', { size: '20px' })}</div>`;

  frame.appendChild(head);
  frame.appendChild(body);
  backdrop.appendChild(frame);
  document.body.appendChild(backdrop);

  function close() {
    if (!_viewerOpen) return;
    _viewerOpen = false;
    document.removeEventListener('keydown', onKey, true);
    backdrop.remove();
  }
  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
  }
  document.addEventListener('keydown', onKey, true);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
  closeBtn.addEventListener('click', close);

  _renderViewerBody(body, att, mime, name);
}

function _renderViewerBody(body, att, mime, name) {
  const fail = (msg) => {
    body.replaceChildren();
    const missing = document.createElement('div');
    missing.className = 'attachment-viewer-missing';
    missing.textContent = msg;
    body.appendChild(missing);
  };
  const withUrl = (cb) => _resolveAttachmentUrl(att)
    .then(url => { if (!url) return fail(`Couldn't load "${name}".`); body.innerHTML = ''; cb(url); })
    .catch(() => fail(`Couldn't load "${name}".`));

  if (mime.startsWith('image/')) {
    return withUrl(url => {
      const img = document.createElement('img');
      img.src = url; img.alt = name; img.className = 'attachment-viewer-img';
      body.appendChild(img);
    });
  }
  if (mime.startsWith('video/')) {
    return withUrl(url => {
      const v = document.createElement('video');
      v.src = url; v.controls = true; v.autoplay = true; v.className = 'attachment-viewer-video';
      body.appendChild(v);
    });
  }
  if (mime.startsWith('audio/')) {
    return withUrl(url => {
      const a = document.createElement('audio');
      a.src = url; a.controls = true; a.autoplay = true; a.className = 'attachment-viewer-audio';
      body.appendChild(a);
    });
  }
  if (mime === 'application/pdf' || /\.pdf$/i.test(name)) {
    return withUrl(url => {
      const f = document.createElement('iframe');
      f.src = url; f.className = 'attachment-viewer-pdf';
      body.appendChild(f);
    });
  }
  const isText = mime.startsWith('text/') || mime === 'application/json'
    || /\.(md|markdown|csv|txt|json|log|tsv|xml|yaml|yml)$/i.test(name);
  if (isText) {
    return withUrl(url => {
      fetch(url).then(r => r.text()).then(text => {
        const isMd = mime === 'text/markdown' || /\.(md|markdown)$/i.test(name);
        if (isMd && window.marked) {
          const div = document.createElement('div');
          div.className = 'attachment-viewer-text attachment-viewer-md';
          try {
            const rendered = window.marked.parse(text);
            if (window.DOMPurify) {
              div.innerHTML = window.DOMPurify.sanitize(rendered, { FORBID_ATTR: ['style'] });
            } else {
              div.textContent = text;
            }
          } catch {
            div.textContent = text;
          }
          body.appendChild(div);
        } else {
          const pre = document.createElement('pre');
          pre.className = 'attachment-viewer-text';
          pre.textContent = text;
          body.appendChild(pre);
        }
      }).catch(() => fail(`Couldn't read "${name}".`));
    });
  }
  // Anything else: offer to open in a new tab.
  return withUrl(url => {
    const a = document.createElement('a');
    a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    a.className = 'attachment-viewer-download';
    _appendIconText(a, 'external-link', `Open "${name}" in a new tab`, '16px');
    body.appendChild(a);
  });
}

// Inline stand-in for an image attachment whose real URL has not resolved yet.
// The element is created and appended synchronously, but its source arrives
// asynchronously (IndexedDB read, thumbnail downscale, or RAM-cache hit) — and
// CSS gives it a fixed box, so the browser paints its broken-file glyph in that
// gap and again if a resolved URL later fails (an evicted object URL). Seeding
// a valid, instantly-loading source keeps the slot reading as "image loading"
// instead. Rendered at 24×24 so both the compact thumbnail and the full-size
// `.chat-attachment-img` box get the same icon-sized placeholder.
// PREVENTIVE-COMMENTS: the `error` fallback in renderAttachmentElement below
// must keep pointing at this constant, or a failed real source re-exposes the
// browser's broken glyph.
const IMAGE_PLACEHOLDER_SRC = 'data:image/svg+xml,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
  + '<g fill="none" stroke="#9aa0a6" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
  + '<rect x="3" y="4" width="18" height="16" rx="2.5" fill="#9aa0a6" fill-opacity="0.14"/>'
  + '<circle cx="8.5" cy="9.2" r="1.6"/>'
  + '<path d="m20.5 15.5-3.4-3.4a1.7 1.7 0 0 0-2.4 0L7 19.8"/>'
  + '</g></svg>'
);

export function renderAttachmentElement(att, { thumbnail = false } = {}) {
  const mime = att.mime_type || '';

  // Image. User-message history requests the same compact thumbnail treatment
  // as the composer pill; other callers retain the full attachment preview.
  if (mime.startsWith('image/')) {
    const img = document.createElement('img');
    img.alt = att.original_name || 'attachment';
    img.className = thumbnail
      ? 'chat-attachment-thumb chat-message-attachment-thumb clickable'
      : 'chat-attachment-img clickable';
    img.loading = 'lazy';
    img.title = 'Click to expand';
    img.addEventListener('click', () => openAttachmentViewer(att));
    // Seed a real source immediately so the fixed-size box never flashes the
    // browser's broken-file glyph while the URL resolves, and fall back to the
    // same placeholder if that resolved source fails to load.
    img.src = IMAGE_PLACEHOLDER_SRC;
    img.addEventListener('error', () => {
      if (img.getAttribute('src') !== IMAGE_PLACEHOLDER_SRC) img.src = IMAGE_PLACEHOLDER_SRC;
    });
    _setSrcAsync(img, 'src', att, 'chat-attachment-missing', _resolveThumbUrl);
    return img;
  }

  // Audio
  if (mime.startsWith('audio/')) {
    const wrapper = document.createElement('div');
    wrapper.className = 'chat-audio-wrapper';
    const label = document.createElement('div');
    label.className = 'chat-attachment-label';
    _appendIconText(label, 'mic', att.original_name || 'Voice recording');
    wrapper.appendChild(label);
    const audio = document.createElement('audio');
    audio.controls = true;
    audio.className = 'chat-audio-player';
    audio.preload = 'metadata';
    _setSrcAsync(audio, 'src', att);
    wrapper.appendChild(audio);
    return wrapper;
  }

  // Video
  if (mime.startsWith('video/')) {
    const video = document.createElement('video');
    video.controls = true;
    video.className = 'chat-attachment-video';
    video.preload = 'metadata';
    _setSrcAsync(video, 'src', att);
    return video;
  }

  // Default (PDF, text, anything else): open the in-app viewer on click. We
  // keep the resolved URL on href too, so middle-click / "open in new tab"
  // still works as a fallback.
  const link = document.createElement('a');
  _appendIconText(link, 'paperclip', att.original_name || 'Attachment');
  link.className = 'chat-attachment-link clickable';
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.title = 'Click to expand';
  link.addEventListener('click', (e) => { e.preventDefault(); openAttachmentViewer(att); });
  _setSrcAsync(link, 'href', att);
  return link;
}

// ── Handle attachment event from WebSocket ─────────────────────────────────

export function handleAttachmentEvent(event) {
  // The chat pipeline emits this while it resolves attachments submitted with
  // the user's request. The composer already renders those in the user bubble;
  // this metadata lacks a display URL and must never be mistaken for an
  // attachment returned by the agent.
  if (event.type !== 'attachment' || !event.attachments) return;
}

/** Forward a secondary chat pill's attach/voice buttons to the main chat composer.
 *  This avoids duplicating the file-picker / recorder logic in every secondary
 *  chat pill (agent builder, genui pages, etc.).
 *  @param {HTMLElement|null} attachBtn — the secondary pill's 📎 button
 *  @param {HTMLElement|null} voiceBtn  — the secondary pill's 🎤 button
 */
export function forwardChatPillControls(attachBtn, voiceBtn) {
  if (attachBtn) {
    attachBtn.addEventListener('click', () => {
      const mainAttach = document.getElementById('chat-attach-btn');
      if (mainAttach) mainAttach.click();
    });
  }
  if (voiceBtn) {
    if (!isVoiceInputSupported()) {
      // Never forward to a hidden mic — hide the secondary button too.
      voiceBtn.hidden = true;
      voiceBtn.style.setProperty('display', 'none', 'important');
    } else {
      voiceBtn.addEventListener('click', () => {
        const mainVoice = document.getElementById('chat-voice-btn');
        if (mainVoice) mainVoice.click();
      });
    }
  }
}

// ── Init on DOM ready (only if not already imported by chat.js) ──
// initAttachments is also called from main.js; guard against double init.
let _initDone = false;

export function ensureAttachmentsInit() {
  if (!_initDone) {
    _initDone = true;
    initAttachments();
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => ensureAttachmentsInit());
} else {
  ensureAttachmentsInit();
}
