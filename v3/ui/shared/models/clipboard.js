'use strict';
export function copyText(text) {
  text = text == null ? '' : String(text);
  if (navigator.clipboard && window.isSecureContext && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  // Fallback for non-secure contexts (http://<ip>:8080 etc.)
  return new Promise((resolve, reject) => {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0;';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error('execCommand copy failed'));
    } catch (e) { reject(e); }
  });
}
