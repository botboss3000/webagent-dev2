'use strict';

// Shared full-view dialog for rendered chat elements. Callers pass the element
// that holds the readable content; its DOM is cloned so the dialog is a stable
// snapshot even while the live transcript changes underneath it.

import { _refreshLucideIcons } from './presentation.js';

let _activeFullView = null;
let _viewerId = 0;

export function closeChatElementFullView() {
  const state = _activeFullView;
  if (!state) return;
  _activeFullView = null;
  document.removeEventListener('keydown', state.onKeyDown);
  state.backdrop.remove();
  if (state.returnFocus?.isConnected) state.returnFocus.focus();
}

export function openChatElementFullView(source, returnFocus, title = 'Full view') {
  closeChatElementFullView();
  if (!source) return;

  const titleId = `chat-element-full-view-title-${++_viewerId}`;
  const backdrop = document.createElement('div');
  backdrop.className = 'chat-element-full-view-backdrop';
  const viewer = document.createElement('section');
  viewer.className = 'chat-element-full-view';
  viewer.setAttribute('role', 'dialog');
  viewer.setAttribute('aria-modal', 'true');
  viewer.setAttribute('aria-labelledby', titleId);

  const header = document.createElement('header');
  header.className = 'chat-element-full-view-head';
  const heading = document.createElement('h2');
  heading.id = titleId;
  heading.textContent = title;
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'chat-element-full-view-close';
  close.title = `Close ${title.toLowerCase()}`;
  close.setAttribute('aria-label', `Close ${title.toLowerCase()}`);
  close.innerHTML = '<i data-lucide="x" style="width:16px;height:16px;"></i>';
  header.append(heading, close);

  const snapshot = document.createElement('div');
  snapshot.className = 'chat-element-full-view-message';
  snapshot.appendChild(source.cloneNode(true));
  viewer.append(header, snapshot);
  backdrop.appendChild(viewer);
  document.body.appendChild(backdrop);

  const onKeyDown = (event) => {
    if (event.key === 'Escape') closeChatElementFullView();
  };
  _activeFullView = { backdrop, returnFocus, onKeyDown };
  document.addEventListener('keydown', onKeyDown);
  close.addEventListener('click', closeChatElementFullView);
  backdrop.addEventListener('click', (event) => {
    if (event.target === backdrop) closeChatElementFullView();
  });
  _refreshLucideIcons(viewer);
  close.focus();
}
