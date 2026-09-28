'use strict';

// Canonical Agents page entry. The shared control factory preserves existing
// save overlays and listeners while this folder owns the page lifecycle.
import { createAgentConfigPage } from '../../configs/shared.js';
import { renderAgentConfigTopicPage } from '../../configs/content.js?v=12';

export default createAgentConfigPage();

export function renderAgentConfigTopic(body, agent, panelEl, renderList, options) {
  return renderAgentConfigTopicPage(body, agent, panelEl, renderList, 'model', options);
}
