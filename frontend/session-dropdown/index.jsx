"use strict";

import React, { memo, useLayoutEffect, useSyncExternalStore } from "react";
import { createPortal, createRoot } from "react-dom/client";

const MarkupNode = memo(function MarkupNode({ item }) {
  const attributes = {
    className: item.className,
    id: item.id || undefined,
    role: item.role || undefined,
    dangerouslySetInnerHTML: { __html: item.html },
  };
  for (const [name, value] of Object.entries(item.dataset || {})) {
    attributes["data-" + name] = value;
  }
  return React.createElement("div", attributes);
}, (previous, next) => previous.item.html === next.item.html
  && previous.item.className === next.item.className
  && previous.item.id === next.item.id
  && previous.item.role === next.item.role);

function Trigger() {
  return React.createElement("div", {
    // SESSION-TRIGGER-COMPAT: list.js and app1.css use this legacy ID.
    id: "session-dropdown-trigger",
    className: "session-dropdown-trigger",
    role: "button",
    tabIndex: 0,
    title: "Show sessions",
    "aria-label": "Show sessions",
  },
  React.createElement("span", { className: "session-row-agent-icon", id: "session-dropdown-icon" }, "◉"),
  React.createElement("span", { className: "session-row-title", id: "session-dropdown-label" }, "—"),
  React.createElement("span", { className: "session-row-status", id: "session-dropdown-status" }),
  React.createElement("button", { className: "session-row-kebab", id: "session-dropdown-kebab", type: "button", title: "More…" }, "⋮"),
  React.createElement("button", { className: "session-row-delete", id: "session-dropdown-delete", type: "button", title: "Delete session" }, "×"));
}

function SessionMenu({ snapshot }) {
  const content = snapshot.presentation || [];
  return createPortal(
    React.createElement("div", {
      className: "session-dropdown-menu",
      id: "session-dropdown-menu",
      hidden: true,
      "data-react-managed": "true",
    }, content.map(item => React.createElement(MarkupNode, { key: item.key, item }))),
    document.body,
  );
}

function SessionDropdownIsland({ bridge, root }) {
  const snapshot = useSyncExternalStore(bridge.store.subscribe, bridge.store.getSnapshot, bridge.store.getSnapshot);
  useLayoutEffect(() => {
    const handle = bridge.mount(root);
    bridge.activate();
    return () => {
      bridge.deactivate();
      handle?.destroy?.();
    };
  }, [bridge, root]);
  useLayoutEffect(() => {
    root.dataset.reactSessionRevision = String(snapshot.revision);
  }, [root, snapshot.revision]);
  useLayoutEffect(() => {
    const focus = snapshot.searchFocus;
    if (!focus) return;
    const input = document.querySelector('#session-dropdown-menu .session-manage-search-input');
    if (!input) return;
    input.focus();
    try { input.setSelectionRange(focus.s ?? focus.value.length, focus.e ?? focus.value.length); } catch (_) {}
  }, [snapshot.revision, snapshot.searchFocus]);

  return React.createElement(React.Fragment, null,
    React.createElement(Trigger),
    React.createElement(SessionMenu, { snapshot }));
}

export function mountSessionDropdownIsland(rootElement, bridge) {
  const reactRoot = createRoot(rootElement);
  reactRoot.render(React.createElement(SessionDropdownIsland, { bridge, root: rootElement }));
  return { destroy: () => reactRoot.unmount() };
}
