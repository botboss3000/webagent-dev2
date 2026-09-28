'use strict';

// Session dropdown list — session data + rendering, centralized in
// ui/chat/elements/session-dropdown/list.js. This file is a compatibility
// re-export shim so the existing importers (session-core.js, chat-send.js,
// chat-stream.js, chat-ui.js, session-load.js, chat-bubble-actions.js,
// session-notification.js, genui-toolbar.js, ...) keep working unchanged.
// Module map: ui/chat/js/README.md.

export * from '../elements/session-dropdown/list.js';
