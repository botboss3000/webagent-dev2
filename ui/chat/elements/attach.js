'use strict';

import {
  cameraAccessMessage,
  microphoneAccessMessage,
  requestMicrophoneAccess,
} from '../../shared/js/microphone-diagnostics.js';
import { acceptedFormatsLabel } from '../../shared/js/attachments.js';

// Chat element: attach — plus-triggered attachment, camera, and microphone menu.
// Mounted by ui/chat-controls/chat-controls-config.js; action availability comes
// from data/config/chat_ui.json → active_footer/idle_footer.chat_pill.controls.attach.

function _closeMenu(menu, trigger) {
  menu.hidden = true;
  trigger.setAttribute('aria-expanded', 'false');
}

function _positionMenu(menu, trigger) {
  const rect = trigger.getBoundingClientRect();
  menu.style.left = `${Math.max(8, rect.left)}px`;
  menu.style.top = `${Math.max(8, rect.top - menu.offsetHeight - 8)}px`;
}

export function html(cfg = {}) {
  const size = cfg.element_size || '22px';
  return `<button type="button" class="chat-pill-attach" title="Add to message" aria-label="Add to message" aria-haspopup="menu" aria-expanded="false" data-element-name="attach">
    <i data-lucide="plus" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-attach-btn';
  el.dataset.popover = 'true';

  // Accepted-format policy from chat_ui.json → controls.attach.accepted_formats.
  // Applied to the shared hidden file input (this drives the OS file picker) and
  // published as data-accepted-formats so the composer's drop/paste validator
  // honours the same list — previously only the picker filtered and drops/pastes
  // bypassed the accepted list entirely.
  const formats = Array.isArray(cfg.accepted_formats)
    ? cfg.accepted_formats.map((token) => String(token).trim()).filter(Boolean)
    : [];
  if (formats.length) {
    const fileInput = document.getElementById('chat-file-input');
    if (fileInput) {
      fileInput.setAttribute('accept', formats.join(','));
      fileInput.dataset.acceptedFormats = formats.join(',');
    }
  }

  const menu = document.createElement('div');
  menu.className = 'chat-attach-popover';
  menu.hidden = true;
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Add to message');
  menu.innerHTML = `
    <button type="button" role="menuitem" class="chat-attach-popover-action" data-attach-action="file">
      <i data-lucide="paperclip"></i><span>${cfg.attach_label || 'Attach files'}</span>
    </button>
    <button type="button" role="menuitem" class="chat-attach-popover-action" data-attach-action="take-picture">
      <i data-lucide="camera"></i><span>${cfg.take_picture_label || 'Take picture'}</span>
    </button>
    <button type="button" role="menuitem" class="chat-attach-popover-action" data-attach-action="camera">
      <i data-lucide="camera"></i><span>${cfg.camera_label || 'Enable camera'}</span>
    </button>
    <button type="button" role="menuitem" class="chat-attach-popover-action" data-attach-action="mic">
      <i data-lucide="mic"></i><span>${cfg.mic_label || 'Enable mic'}</span>
    </button>
    <p class="chat-attach-popover-status" role="status" aria-live="polite"></p>
    <section class="chat-camera-capture" hidden aria-label="Camera preview">
      <video class="chat-camera-capture-preview" autoplay muted playsinline></video>
      <div class="chat-camera-capture-actions">
        <button type="button" class="chat-camera-capture-button primary" data-attach-action="capture-picture">Capture</button>
        <button type="button" class="chat-camera-capture-button" data-attach-action="cancel-picture">Cancel</button>
      </div>
    </section>`;
  document.body.appendChild(menu);
  window.lucide?.createIcons?.({ nodes: [menu] });

  const status = menu.querySelector('.chat-attach-popover-status');
  if (formats.length && cfg.show_accepted_formats !== false) {
    const hint = document.createElement('p');
    hint.className = 'chat-attach-popover-hint';
    hint.textContent = `Accepted: ${acceptedFormatsLabel(formats)}`;
    status.insertAdjacentElement('beforebegin', hint);
  }
  const capturePanel = menu.querySelector('.chat-camera-capture');
  const preview = menu.querySelector('.chat-camera-capture-preview');
  let cameraStream = null;
  const setStatus = (message = '') => { status.textContent = message; };
  const stopCamera = () => {
    if (cameraStream) cameraStream.getTracks().forEach(track => track.stop());
    cameraStream = null;
    preview.srcObject = null;
    capturePanel.hidden = true;
  };
  const closeMenu = () => {
    stopCamera();
    _closeMenu(menu, el);
  };
  const openMenu = () => {
    menu.hidden = false;
    _positionMenu(menu, el);
    el.setAttribute('aria-expanded', 'true');
  };
  const startCamera = async () => {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('CameraUnavailable');
    stopCamera();
    cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } } });
    preview.srcObject = cameraStream;
    capturePanel.hidden = false;
    await preview.play().catch(() => {});
    _positionMenu(menu, el);
  };

  el.addEventListener('click', (event) => {
    event.stopPropagation();
    if (menu.hidden) openMenu(); else closeMenu();
  });
  menu.addEventListener('click', async (event) => {
    const action = event.target.closest('[data-attach-action]')?.dataset.attachAction;
    if (!action) return;
    if (action === 'file') {
      closeMenu();
      document.getElementById('chat-file-input')?.click();
      return;
    }
    if (action === 'cancel-picture') {
      stopCamera();
      setStatus('Camera preview cancelled.');
      _positionMenu(menu, el);
      return;
    }
    if (action === 'capture-picture') {
      if (!cameraStream || preview.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
        setStatus('Camera preview is not ready yet.');
        return;
      }
      const canvas = document.createElement('canvas');
      canvas.width = preview.videoWidth || 1280;
      canvas.height = preview.videoHeight || 720;
      canvas.getContext('2d')?.drawImage(preview, 0, 0, canvas.width, canvas.height);
      canvas.toBlob((blob) => {
        if (!blob) {
          setStatus('Picture capture failed. Please try again.');
          return;
        }
        const fileInput = document.getElementById('chat-file-input');
        if (!fileInput) return;
        const file = new File([blob], `camera-${Date.now()}.jpg`, { type: 'image/jpeg' });
        const transfer = new DataTransfer();
        transfer.items.add(file);
        fileInput.files = transfer.files;
        fileInput.dispatchEvent(new Event('change', { bubbles: true }));
        closeMenu();
      }, 'image/jpeg', 0.92);
      return;
    }
    if (action !== 'take-picture' && action !== 'camera' && action !== 'mic') return;
    const camera = action === 'camera' || action === 'take-picture';
    const takingPicture = action === 'take-picture';
    const button = event.target.closest('[data-attach-action]');
    if (!navigator.mediaDevices?.getUserMedia) {
      setStatus(`${camera ? 'Camera' : 'Microphone'} access is not supported in this browser.`);
      return;
    }
    button.disabled = true;
    setStatus(takingPicture ? 'Starting camera…' : `Requesting ${camera ? 'camera' : 'microphone'} access…`);
    try {
      if (takingPicture) {
        await startCamera();
        setStatus('Frame your picture, then select Capture.');
      } else {
        const stream = camera
          ? await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } } })
          : await requestMicrophoneAccess({ audio: true });
        stream.getTracks().forEach(track => track.stop());
        setStatus(`${camera ? 'Camera' : 'Microphone'} enabled.`);
      }
    } catch (error) {
      stopCamera();
      setStatus(await (camera ? cameraAccessMessage(error) : microphoneAccessMessage(error)));
    } finally {
      button.disabled = false;
    }
  });
  document.addEventListener('click', (event) => {
    if (!menu.hidden && !menu.contains(event.target) && event.target !== el) closeMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !menu.hidden) {
      closeMenu();
      el.focus();
    }
  });
}

export function destroy(el) {}

export function style() { return ''; }
