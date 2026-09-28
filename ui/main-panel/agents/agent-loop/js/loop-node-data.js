'use strict';

// Mutable exports — populated by the fetch below.
// Consumers (loop-logic.js, agents.js) hold references to these objects;
// mutations here are visible to them by the time any user interaction fires.
export const NODE_STATIC_ITEMS = {};
export const NODE_PANEL_INFO   = {};
export const OPTIMIZER_NODES   = [];

const nodeDataReady = fetch(new URL('./loop-nodes.json', import.meta.url))
  .then(r => r.json())
  .then(data => {
    Object.assign(NODE_STATIC_ITEMS, data.NODE_STATIC_ITEMS);
    Object.assign(NODE_PANEL_INFO,   data.NODE_PANEL_INFO);
    OPTIMIZER_NODES.push(...data.OPTIMIZER_NODES);
  })
  .catch(e => console.error('[loop-node-data] Failed to load loop-nodes.json', e));
