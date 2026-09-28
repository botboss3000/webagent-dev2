'use strict';

/**
 * Agents — icon picker popover. Thin wrapper around the shared, generic icon
 * picker (ui/shared/js/icon-picker.js): it opens the shared grid, then applies
 * the chosen icon to the agent (live UI update + persist for custom agents).
 */

import { openIconPicker } from '../../../shared/js/icon-picker.js';
import { _renderAgentIcon, _putAgentField } from './utils.js';


export function _openIconPicker(anchorEl, agent, panelEl) {
  openIconPicker({ current: agent.icon || '', title: 'Choose an icon' }).then((chosen) => {
    if (!chosen) return;
    agent.icon = chosen;
    document.querySelectorAll(`.agent-row[data-agent-id="${CSS.escape(agent.id)}"] .agent-card-icon-wrap`).forEach(iconWrap => {
      const iconSize = iconWrap.closest('.agent-card.active') ? '20px' : '24px';
      iconWrap.innerHTML = _renderAgentIcon({ icon: chosen }, iconSize);
    });
    if (agent.source === 'custom') {
      _putAgentField(agent, { icon: chosen });
    }
  });
}
