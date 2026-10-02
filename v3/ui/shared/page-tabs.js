'use strict';

// Shared Agents/App interaction: a 500ms hold arms reorder; a short tap navigates.
export function wirePageTabDrag(strip, { getOrder, save, prefix }) {
  let press = null, suppress = false;
  const tabFor = id => [...strip.children].find(tab => tab.id === prefix + id);
  const paint = () => { for (const id of getOrder()) strip.append(tabFor(id)); };
  const finish = event => {
    if (!press || event.pointerId !== press.pointerId) return;
    clearTimeout(press.timer);
    if (press.armed) { suppress = true; save(); setTimeout(() => { suppress = false; }, 500); }
    press.tab.classList.remove('dragging');
    if (strip.hasPointerCapture(event.pointerId)) strip.releasePointerCapture(event.pointerId);
    press = null;
  };
  strip.addEventListener('pointerdown', event => {
    const tab = event.target.closest('[role=tab]');
    if (!tab || tab.classList.contains('ephemeral') || event.button !== 0 || press) return;
    press = { tab, pointerId: event.pointerId, x: event.clientX, y: event.clientY, armed: false };
    const current = press;
    current.timer = setTimeout(() => {
      if (press !== current) return;
      current.armed = true; tab.classList.add('dragging');
      if (strip.hasPointerCapture(event.pointerId) || event.isTrusted) strip.setPointerCapture(event.pointerId);
    }, 500);
  });
  strip.addEventListener('pointermove', event => {
    if (!press || event.pointerId !== press.pointerId) return;
    if (!press.armed) {
      if (Math.hypot(event.clientX - press.x, event.clientY - press.y) > 8) { clearTimeout(press.timer); press = null; }
      return;
    }
    event.preventDefault();
    const target = [...strip.querySelectorAll('[role=tab]:not([hidden]):not(.ephemeral)')].find(tab => {
      const rect = tab.getBoundingClientRect(); return event.clientX >= rect.left && event.clientX <= rect.right;
    });
    if (!target || target === press.tab) return;
    const order = getOrder(), from = order.indexOf(press.tab.id.slice(prefix.length)), to = order.indexOf(target.id.slice(prefix.length));
    if (from < 0 || to < 0) return;
    const [id] = order.splice(from, 1); order.splice(to, 0, id); paint();
  });
  strip.addEventListener('pointerup', finish);
  strip.addEventListener('pointercancel', finish);
  strip.addEventListener('click', event => { if (suppress) { event.preventDefault(); event.stopImmediatePropagation(); } }, true);
  strip.addEventListener('keydown', event => {
    const tab = event.target.closest('[role=tab]');
    if (!tab || tab.classList.contains('ephemeral') || !event.altKey || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const order = getOrder(), from = order.indexOf(tab.id.slice(prefix.length)), to = from + (event.key === 'ArrowLeft' ? -1 : 1);
    if (from < 0 || to < 0 || to >= order.length) return;
    const [id] = order.splice(from, 1); order.splice(to, 0, id); save(); tab.focus();
  });
}
