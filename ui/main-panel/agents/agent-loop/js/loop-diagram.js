'use strict';

// ── Shared loop diagram data and renderer ──────────────────────────────────────
// Single source of truth for node positions, edge topology, and SVG rendering.
// Consumed by loop-logic.js (live runtime view) and agents.js (config/test view).
//
// 9 stages  ·  34 nodes  ·  matches app/api/chat.py + app/agent/loop.py exactly

export const LOOP_W = 1560;
const LOOP_H = 400;

// ── Toggleable nodes — these have runtime gating in loop.py ──────────────────
// UI can render toggle switches only for these node IDs.
// All other nodes run unconditionally and cannot be user-disabled.
export const TOGGLEABLE_NODES = new Set([
  'interrupt_chk',   // Skip interrupt checks — useful for batch / automated agents
  'guardrails',      // Skip destructive-tool confirmation — useful for admin agents
  'delegation_chk',  // Skip agent-delegation detection
  'skill_track',     // Skip skill-execution DB writes — useful for lightweight agents
  'memory_search',   // Skip brain context lookup — useful for tool-only agents
  'memory_save',     // Skip post-chat memory upsert — useful for ephemeral agents
  'copy_defaults',   // Skip copying default context docs on first use
  // permission_chk is configured via the Turn Counter node editor, not as a standalone node
]);

// Width below which the diagram switches from horizontal to vertical layout.
const BREAKPOINT_VERTICAL = 900;

// ── Stage column bands ────────────────────────────────────────────────────────
const LOOP_STAGES = [
  { label: 'INPUT',     x1: 0,    x2: 138,  color: '#7dcfff' },
  { label: 'PRE-LOOP',  x1: 138,  x2: 306,  color: '#f7768e' },
  { label: 'CONTEXT',   x1: 306,  x2: 486,  color: '#c0caf5' },
  { label: 'LOOP INIT', x1: 486,  x2: 670,  color: '#2ac3de' },
  { label: 'INFERENCE', x1: 670,  x2: 860,  color: '#bb9af7' },
  { label: 'ROUTING',   x1: 860,  x2: 1054, color: '#e0af68' },
  { label: 'EXECUTION', x1: 1054, x2: 1236, color: '#a9b1d6' },
  { label: 'CONTINUE?', x1: 1236, x2: 1396, color: '#e0af68' },
  { label: 'OUTPUT',    x1: 1396, x2: 1560, color: '#9ece6a' },
];

// ── Nodes — cx,cy = centre; hw,hh = half-width, half-height ─────────────────
export const LOOP_NODES = [

  // ── INPUT ────────────────────────────────────────────────────────────────────
  { id: 'user_input',      label: 'User Input',      type: 'input',    cx: 65,   cy: 200, hw: 52, hh: 18 },

  // ── PRE-LOOP  (chat.py — before stream_agent_events) ─────────────────────────
  { id: 'slash_cmd',       label: 'Slash Cmd',        type: 'process',  cx: 217,  cy: 166, hw: 58, hh: 13 },
  { id: 'session_setup',   label: 'Session Setup',    type: 'process',  cx: 217,  cy: 200, hw: 58, hh: 13 },
  { id: 'save_user_msg',   label: 'Save User Msg',    type: 'process',  cx: 217,  cy: 234, hw: 58, hh: 13 },

  // ── CONTEXT  (chat.py — context + prompt assembly) ────────────────────────────
  { id: 'load_context',    label: 'Load Context',     type: 'process',  cx: 396,  cy: 80,  hw: 60, hh: 13 },
  { id: 'memory_search',   label: 'Memory Search',    type: 'process',  cx: 396,  cy: 114, hw: 60, hh: 13 },
  { id: 'resolve_attach',     label: 'Resolve Attach',         type: 'process',  cx: 396,  cy: 148, hw: 60, hh: 13 },
  { id: 'attachment_describe',label: 'Attachment Description', type: 'llm',      cx: 396,  cy: 182, hw: 60, hh: 13 },
  { id: 'build_prompt',       label: 'Build Prompt',           type: 'process',  cx: 396,  cy: 216, hw: 60, hh: 13 },
  { id: 'build_history',      label: 'Build History',          type: 'process',  cx: 396,  cy: 250, hw: 60, hh: 13 },

  // ── LOOP INIT  (once per request, before the while loop) ─────────────────────
  { id: 'load_provider',       label: 'Load Provider',      type: 'process',  cx: 578,  cy: 138, hw: 60, hh: 13 },
  { id: 'load_tools',          label: 'Load Tools',         type: 'process',  cx: 578,  cy: 172, hw: 60, hh: 13 },
  { id: 'integration_status',  label: 'Integration Status', type: 'process',  cx: 578,  cy: 240, hw: 66, hh: 13 },
  { id: 'assemble_msgs',       label: 'Assemble Msgs',      type: 'process',  cx: 578,  cy: 274, hw: 60, hh: 13 },

  // ── INFERENCE  (per-turn while loop) ─────────────────────────────────────────
  { id: 'interrupt_chk',   label: 'Interrupt Chk',    type: 'decision', cx: 762,  cy: 100, hw: 58, hh: 13 },
  { id: 'turn_counter',    label: 'Turn Counter',     type: 'process',  cx: 762,  cy: 134, hw: 58, hh: 13 },
  { id: 'build_tool_defs', label: 'Tool Defs',        type: 'process',  cx: 762,  cy: 168, hw: 58, hh: 13 },
  { id: 'llm_call',        label: 'LLM Call',         type: 'llm',      cx: 762,  cy: 240, hw: 55, hh: 20 },

  // ── ROUTING  (validate + guard, per tool call) ────────────────────────────────
  { id: 'db_persist_asst', label: 'Persist Asst',     type: 'process',  cx: 957,  cy: 132, hw: 60, hh: 13 },
  { id: 'validate_tools',  label: 'Validate',          type: 'process',  cx: 957,  cy: 166, hw: 60, hh: 13 },
  { id: 'destructive_chk', label: 'Destructive Chk',  type: 'guard',    cx: 957,  cy: 200, hw: 60, hh: 13 },
  { id: 'guardrails',      label: 'Guardrails',        type: 'guard',    cx: 957,  cy: 234, hw: 60, hh: 13 },
  { id: 'post_val_chk',    label: 'Post-Val Chk',     type: 'process',  cx: 957,  cy: 268, hw: 60, hh: 13 },

  // ── EXECUTION  (per tool result) ─────────────────────────────────────────────
  { id: 'execute_tools',   label: 'Execute Tools',    type: 'process',  cx: 1145, cy: 140, hw: 62, hh: 16 },
  { id: 'db_persist_tool', label: 'Persist Tool',     type: 'process',  cx: 1145, cy: 222, hw: 62, hh: 13 },
  { id: 'delegation_chk',  label: 'Delegation Chk',   type: 'process',  cx: 1145, cy: 256, hw: 62, hh: 13 },
  { id: 'skill_track',     label: 'Skill Track',      type: 'process',  cx: 1145, cy: 290, hw: 62, hh: 13 },

  // ── CONTINUE? ────────────────────────────────────────────────────────────────
  { id: 'check_continue',  label: 'Continue?',        type: 'decision', cx: 1316, cy: 200, hw: 58, hh: 18 },

  // ── OUTPUT ───────────────────────────────────────────────────────────────────
  { id: 'final_response',  label: 'Final Response',   type: 'output',   cx: 1478, cy: 150, hw: 62, hh: 14 },
  { id: 'db_persist_final',label: 'Persist Final',    type: 'process',  cx: 1478, cy: 192, hw: 62, hh: 13 },
  { id: 'memory_save',     label: 'Memory Save',      type: 'process',  cx: 1478, cy: 226, hw: 62, hh: 13 },
];

// ── Edges ─────────────────────────────────────────────────────────────────────
const LOOP_EDGES = [

  // INPUT → PRE-LOOP
  { from: 'user_input',      to: 'slash_cmd'                                                    },

  // PRE-LOOP chain
  { from: 'slash_cmd',       to: 'session_setup',   vertical: true                              },
  { from: 'session_setup',   to: 'save_user_msg',   vertical: true                              },

  // PRE-LOOP → CONTEXT
  { from: 'save_user_msg',   to: 'load_context'                                                 },

  // CONTEXT chain
  { from: 'load_context',    to: 'memory_search',   vertical: true                              },
  { from: 'memory_search',     to: 'resolve_attach',      vertical: true                          },
  { from: 'resolve_attach',    to: 'attachment_describe', vertical: true                          },
  { from: 'attachment_describe', to: 'build_prompt',      vertical: true                          },
  { from: 'build_prompt',      to: 'build_history',       vertical: true                          },

  // CONTEXT → LOOP INIT
  { from: 'build_history',   to: 'load_provider'                                                },

  // LOOP INIT chain
  { from: 'load_provider',      to: 'load_tools',          vertical: true                          },
  { from: 'load_tools',         to: 'integration_status',  vertical: true                          },
  { from: 'integration_status', to: 'assemble_msgs',      vertical: true                          },

  // LOOP INIT → INFERENCE
  { from: 'assemble_msgs',   to: 'interrupt_chk'                                                },

  // INFERENCE chain
  { from: 'interrupt_chk',   to: 'turn_counter',    vertical: true                              },
  { from: 'turn_counter',    to: 'build_tool_defs', vertical: true                              },
  { from: 'build_tool_defs', to: 'llm_call',        vertical: true                              },

  // LLM → ROUTING (tools present)
  { from: 'llm_call',        to: 'db_persist_asst', label: 'tools?'                             },

  // LLM → CONTINUE? (no tool calls — skip routing + execution entirely)
  { from: 'llm_call',        to: 'check_continue',  label: 'no tools', above: true, aboveY: 42  },

  // ROUTING chain
  { from: 'db_persist_asst', to: 'validate_tools',  vertical: true                              },
  { from: 'validate_tools',  to: 'destructive_chk', vertical: true, label: 'valid'              },
  { from: 'destructive_chk', to: 'guardrails',      vertical: true                              },
  { from: 'guardrails',      to: 'post_val_chk',    vertical: true, label: 'pass'               },

  // guardrails blocked → CONTINUE? (bypass execution)
  { from: 'guardrails',      to: 'check_continue',  label: 'blocked', below: true, belowY: 340  },

  // ROUTING → EXECUTION
  { from: 'post_val_chk',    to: 'execute_tools'                                                },

  // EXECUTION chain
  { from: 'execute_tools',   to: 'db_persist_tool', vertical: true                              },
  { from: 'db_persist_tool', to: 'delegation_chk',  vertical: true                              },
  { from: 'delegation_chk',  to: 'skill_track',     vertical: true                              },

  // EXECUTION → CONTINUE?
  { from: 'skill_track',     to: 'check_continue'                                               },

  // CONTINUE? → OUTPUT
  { from: 'check_continue',  to: 'final_response',  label: 'stop'                               },

  // CONTINUE? → INFERENCE loopback (more turns)
  { from: 'check_continue',  to: 'interrupt_chk',   label: '↺ continue', loopback: 358          },

  // OUTPUT chain
  { from: 'final_response',  to: 'db_persist_final', vertical: true                             },
  { from: 'db_persist_final',to: 'memory_save',      vertical: true                             },
];

// ── Horizontal layout — group bounding boxes in the 1560px baseline ──────────
// Each group moves as a unit when the genui is compressed to fit narrower widths.
const _H_GROUPS = [
  { nodeIds: ['user_input'],
    left: 13,   right: 117  },
  { nodeIds: ['slash_cmd','session_setup','save_user_msg'],
    left: 159,  right: 275  },
  { nodeIds: ['load_context','memory_search','resolve_attach','attachment_describe','build_prompt','build_history'],
    left: 336,  right: 456  },
  { nodeIds: ['load_provider','load_tools','integration_status','assemble_msgs'],
    left: 518,  right: 638  },
  { nodeIds: ['interrupt_chk','turn_counter','build_tool_defs','llm_call'],
    left: 704,  right: 820  },
  { nodeIds: ['db_persist_asst','validate_tools','destructive_chk','guardrails','post_val_chk'],
    left: 897,  right: 1017 },
  { nodeIds: ['execute_tools','db_persist_tool','delegation_chk','skill_track'],
    left: 1083, right: 1207 },
  { nodeIds: ['check_continue'],
    left: 1258, right: 1374 },
  { nodeIds: ['final_response','db_persist_final','memory_save'],
    left: 1416, right: 1540 },
];

const _H_GAPS       = _H_GROUPS.slice(1).map((g, i) => g.left - _H_GROUPS[i].right);
const _H_TOTAL_GAPS = _H_GAPS.reduce((a, b) => a + b, 0);
const _H_CONTENT_W  = LOOP_W - _H_TOTAL_GAPS;
const _H_MIN_GAP    = 4;

// ── buildHorizontalLayout ─────────────────────────────────────────────────────
// Compresses inter-group gaps (never node sizes) to fit availableWidth.
function buildHorizontalLayout(availableWidth) {
  const W = availableWidth > 0 ? availableWidth : LOOP_W;

  if (W >= LOOP_W) {
    return { nodes: LOOP_NODES, stages: LOOP_STAGES, genuiW: LOOP_W + 16, genuiH: LOOP_H, mode: 'horizontal' };
  }

  // gs = 1.0 at full width, 0.0 at minimum — gaps clamp at _H_MIN_GAP
  const gs = Math.max(0, Math.min(1, (W - _H_CONTENT_W) / _H_TOTAL_GAPS));

  const newLeft = [];
  let cursor = _H_GROUPS[0].left;
  for (let i = 0; i < _H_GROUPS.length; i++) {
    newLeft.push(cursor);
    if (i < _H_GAPS.length) {
      cursor += (_H_GROUPS[i].right - _H_GROUPS[i].left) + Math.max(_H_MIN_GAP, Math.round(_H_GAPS[i] * gs));
    }
  }

  const shift = new Map();
  _H_GROUPS.forEach((g, i) => g.nodeIds.forEach(id => shift.set(id, newLeft[i] - g.left)));

  const nodes  = LOOP_NODES.map(n  => ({ ...n,  cx: Math.round(n.cx  + (shift.get(n.id)  || 0)) }));
  const stages = LOOP_STAGES.map((s, i) => {
    const dx = newLeft[i] - _H_GROUPS[i].left;
    return { ...s, x1: s.x1 + dx, x2: s.x2 + dx };
  });

  const lastG   = _H_GROUPS[_H_GROUPS.length - 1];
  const genuiW = newLeft[newLeft.length - 1] + (lastG.right - lastG.left) + 8;
  return { nodes, stages, genuiW, genuiH: LOOP_H, mode: 'horizontal' };
}

// ── buildVerticalLayout ───────────────────────────────────────────────────────
// All 35 nodes stacked in a single column, stages as horizontal bands.
// (attachment_describe added — every node from
//  the inserted node onward shifts down by 34, stage bands extended accordingly.)
function buildVerticalLayout(availableWidth) {
  const w  = Math.max(availableWidth > 0 ? availableWidth : 360, 260);
  const cx = w / 2;

  const nodes = [
    // INPUT
    { id: 'user_input',      label: 'User Input',      type: 'input',    cx,        cy: 45,   hw: 52, hh: 18 },
    // PRE-LOOP
    { id: 'slash_cmd',       label: 'Slash Cmd',        type: 'process',  cx,        cy: 152,  hw: 58, hh: 13 },
    { id: 'session_setup',   label: 'Session Setup',    type: 'process',  cx,        cy: 186,  hw: 58, hh: 13 },
    { id: 'save_user_msg',   label: 'Save User Msg',    type: 'process',  cx,        cy: 220,  hw: 58, hh: 13 },
    // CONTEXT  (+34 inserted: attachment_describe)
    { id: 'load_context',    label: 'Load Context',     type: 'process',  cx,        cy: 322,  hw: 60, hh: 13 },
    { id: 'memory_search',   label: 'Memory Search',    type: 'process',  cx,        cy: 356,  hw: 60, hh: 13 },
    { id: 'resolve_attach',  label: 'Resolve Attach',   type: 'process',  cx,        cy: 390,  hw: 60, hh: 13 },
    { id: 'attachment_describe', label: 'Attachment Description', type: 'llm', cx,    cy: 424,  hw: 60, hh: 13 },
    { id: 'build_prompt',    label: 'Build Prompt',     type: 'process',  cx,        cy: 458,  hw: 60, hh: 13 },
    { id: 'build_history',   label: 'Build History',    type: 'process',  cx,        cy: 492,  hw: 60, hh: 13 },
    // LOOP INIT  (+34 from attachment_describe shift)
    { id: 'load_provider',      label: 'Load Provider',      type: 'process',  cx,        cy: 628,  hw: 60, hh: 13 },
    { id: 'load_tools',         label: 'Load Tools',         type: 'process',  cx,        cy: 662,  hw: 60, hh: 13 },
    { id: 'integration_status', label: 'Integration Status', type: 'process',  cx,        cy: 730,  hw: 66, hh: 13 },
    { id: 'assemble_msgs',      label: 'Assemble Msgs',      type: 'process',  cx,        cy: 764,  hw: 60, hh: 13 },
    // INFERENCE
    { id: 'interrupt_chk',   label: 'Interrupt Chk',    type: 'decision', cx,        cy: 832,  hw: 58, hh: 13 },
    { id: 'turn_counter',    label: 'Turn Counter',     type: 'process',  cx,        cy: 866,  hw: 58, hh: 13 },
    { id: 'build_tool_defs', label: 'Tool Defs',        type: 'process',  cx,        cy: 900,  hw: 58, hh: 13 },
    { id: 'llm_call',        label: 'LLM Call',         type: 'llm',      cx,        cy: 972,  hw: 55, hh: 20 },
    // ROUTING
    { id: 'db_persist_asst', label: 'Persist Asst',     type: 'process',  cx,        cy: 1074, hw: 60, hh: 13 },
    { id: 'validate_tools',  label: 'Validate',          type: 'process',  cx,        cy: 1108, hw: 60, hh: 13 },
    { id: 'destructive_chk', label: 'Destructive Chk',  type: 'guard',    cx,        cy: 1142, hw: 60, hh: 13 },
    { id: 'guardrails',      label: 'Guardrails',        type: 'guard',    cx,        cy: 1176, hw: 60, hh: 13 },
    { id: 'post_val_chk',    label: 'Post-Val Chk',     type: 'process',  cx,        cy: 1210, hw: 60, hh: 13 },
    // EXECUTION
    { id: 'execute_tools',   label: 'Execute Tools',    type: 'process',  cx,        cy: 1278, hw: 62, hh: 16 },
    { id: 'db_persist_tool', label: 'Persist Tool',     type: 'process',  cx,        cy: 1362, hw: 62, hh: 13 },
    { id: 'delegation_chk',  label: 'Delegation Chk',   type: 'process',  cx,        cy: 1396, hw: 62, hh: 13 },
    { id: 'skill_track',     label: 'Skill Track',      type: 'process',  cx,        cy: 1430, hw: 62, hh: 13 },
    // CONTINUE?
    { id: 'check_continue',  label: 'Continue?',        type: 'decision', cx,        cy: 1498, hw: 58, hh: 18 },
    // OUTPUT
    { id: 'final_response',  label: 'Final Response',   type: 'output',   cx,        cy: 1566, hw: 62, hh: 14 },
    { id: 'db_persist_final',label: 'Persist Final',    type: 'process',  cx,        cy: 1608, hw: 62, hh: 13 },
    { id: 'memory_save',     label: 'Memory Save',      type: 'process',  cx,        cy: 1642, hw: 62, hh: 13 },
  ];

  const stages = [
    { label: 'INPUT',     y1: 20,   y2: 78,   color: '#7dcfff' },
    { label: 'PRE-LOOP',  y1: 92,   y2: 280,  color: '#f7768e' },
    { label: 'CONTEXT',   y1: 294,  y2: 586,  color: '#c0caf5' },
    { label: 'LOOP INIT', y1: 600,  y2: 790,  color: '#2ac3de' },
    { label: 'INFERENCE', y1: 804,  y2: 1040, color: '#bb9af7' },
    { label: 'ROUTING',   y1: 1054, y2: 1236, color: '#e0af68' },
    { label: 'EXECUTION', y1: 1250, y2: 1456, color: '#a9b1d6' },
    { label: 'CONTINUE?', y1: 1470, y2: 1530, color: '#e0af68' },
    { label: 'OUTPUT',    y1: 1544, y2: 1704, color: '#9ece6a' },
  ];

  return { nodes, stages, genuiW: w, genuiH: 1716, mode: 'vertical' };
}

// ── Edge path computation ─────────────────────────────────────────────────────
export function computeEdgePath(edge, nodes, layout = {}) {
  const { mode = 'horizontal', genuiW = LOOP_W } = layout;
  const src = nodes.find(n => n.id === edge.from);
  const dst = nodes.find(n => n.id === edge.to);
  if (!src || !dst) return null;

  if (mode === 'vertical') {
    return _computeEdgePathVertical(edge, src, dst, genuiW);
  }

  // ── Horizontal mode ───────────────────────────────────────────────────────
  if (edge.vertical) {
    const x = src.cx, y1 = src.cy + src.hh, y2 = dst.cy - dst.hh;
    return { d: `M ${x} ${y1} L ${x} ${y2}`, labelX: x + 14, labelY: (y1 + y2) / 2 + 4 };
  }
  if (edge.above) {
    const arcY = edge.aboveY ?? 40;
    const x1 = src.cx + src.hw, y1 = src.cy, x2 = dst.cx - dst.hw, y2 = dst.cy;
    return { d: `M ${x1} ${y1} C ${x1} ${arcY}, ${x2} ${arcY}, ${x2} ${y2}`,
             labelX: (x1 + x2) / 2, labelY: arcY - 6 };
  }
  if (edge.below) {
    const arcY = edge.belowY ?? 320;
    const x1 = src.cx + src.hw, y1 = src.cy, x2 = dst.cx - dst.hw, y2 = dst.cy;
    return { d: `M ${x1} ${y1} C ${x1} ${arcY}, ${x2} ${arcY}, ${x2} ${y2}`,
             labelX: (x1 + x2) / 2, labelY: arcY + 12 };
  }
  if (edge.loopback) {
    const arcY = edge.loopback, x1 = src.cx, y1 = src.cy + src.hh, x2 = dst.cx, y2 = dst.cy + dst.hh;
    return { d: `M ${x1} ${y1} C ${x1} ${arcY}, ${x2} ${arcY}, ${x2} ${y2}`,
             labelX: (x1 + x2) / 2, labelY: arcY + 11 };
  }
  // Standard S-curve between stages
  const x1 = src.cx + src.hw, y1 = src.cy, x2 = dst.cx - dst.hw, y2 = dst.cy, mx = (x1 + x2) / 2;
  return { d: `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`,
           labelX: mx, labelY: (y1 < y2 ? y1 : y2) - 5 };
}

function _computeEdgePathVertical(edge, src, dst, genuiW) {
  const sameX   = Math.abs(src.cx - dst.cx) < genuiW * 0.06;
  const sameY   = Math.abs(src.cy - dst.cy) < 8;
  const forward = dst.cy > src.cy;

  // Purely horizontal (validate→destructive_chk etc. don't appear sameY in vert mode — this is a safety net)
  if (sameY) {
    const ltr = dst.cx > src.cx;
    const x1  = ltr ? src.cx + src.hw : src.cx - src.hw;
    const x2  = ltr ? dst.cx - dst.hw : dst.cx + dst.hw;
    return { d: `M ${x1} ${src.cy} L ${x2} ${dst.cy}`, labelX: (x1 + x2) / 2, labelY: src.cy - 8 };
  }

  // "below" arc in vertical mode: right-side bypass (guardrails → check_continue)
  if (edge.below) {
    const arcX = genuiW * 0.97;
    const x = src.cx, y1 = src.cy + src.hh, y2 = dst.cy - dst.hh;
    return { d: `M ${x} ${y1} C ${arcX} ${y1}, ${arcX} ${y2}, ${x} ${y2}`,
             labelX: arcX + 3, labelY: (y1 + y2) / 2 };
  }

  // "above" / llm_call→check_continue bypass (skip ROUTING + EXECUTION)
  if (edge.from === 'llm_call' && edge.to === 'check_continue') {
    const arcX = genuiW * 0.93;
    const x = src.cx, y1 = src.cy + src.hh, y2 = dst.cy - dst.hh;
    return { d: `M ${x} ${y1} C ${arcX} ${y1}, ${arcX} ${y2}, ${x} ${y2}`,
             labelX: arcX + 4, labelY: (y1 + y2) / 2 };
  }

  // Backward loopback arc (check_continue → interrupt_chk, ↺ continue)
  if (!forward && sameX) {
    const arcX = edge.from === 'check_continue' ? genuiW * 0.97 : genuiW * 0.91;
    const x = src.cx, y1 = src.cy + src.hh, y2 = dst.cy - dst.hh;
    return { d: `M ${x} ${y1} C ${arcX} ${y1}, ${arcX} ${y2}, ${x} ${y2}`,
             labelX: arcX + 3, labelY: (y1 + y2) / 2 };
  }

  // Straight down (same x, forward)
  if (forward && sameX) {
    const x = src.cx, y1 = src.cy + src.hh, y2 = dst.cy - dst.hh;
    return { d: `M ${x} ${y1} L ${x} ${y2}`, labelX: x + 14, labelY: (y1 + y2) / 2 + 4 };
  }

  // Diagonal S-curve (different x, e.g. inter-stage connections)
  const x1 = src.cx, y1 = src.cy + src.hh, x2 = dst.cx, y2 = dst.cy - dst.hh, my = (y1 + y2) / 2;
  return { d: `M ${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2}`,
           labelX: (x1 + x2) / 2, labelY: my };
}

// ── Edge state (shared by the full renderer and the in-place updater) ──────────
// An edge lights up once its source has run: 'done' when both ends are complete,
// 'active' while the source is running/just-ran but the target hasn't finished.
function _edgeState(nodeStates, from, to) {
  const fromState = nodeStates.get(from);
  const toState   = nodeStates.get(to);
  if (fromState === 'done' && (toState === 'done' || toState === 'active')) return 'done';
  if (fromState === 'active' || fromState === 'done') return 'active';
  return '';
}

// ── In-place state update ──────────────────────────────────────────────────────
// Refreshes node state-classes + detail text and edge styling on an ALREADY
// rendered diagram (the {rootEl, svgEl} from renderLoopDiagram). This is what the
// live runtime view calls on every streaming event instead of tearing down and
// rebuilding the whole diagram — no DOM churn, no scroll jump, no flicker.
// Only structural changes (page switch, resize) need a full renderLoopDiagram.
export function applyDiagramStates(root, svg, nodeStates, { markerPrefix = 'ld', getNodeDetail = null } = {}) {
  if (!root || !svg) return;
  const p = markerPrefix;

  // Nodes — toggle state classes + refresh detail text in place
  root.querySelectorAll('.lv-node[data-id]').forEach(el => {
    const id    = el.dataset.id;
    const state = nodeStates.get(id) || '';
    el.classList.toggle('lv-active', state === 'active');
    el.classList.toggle('lv-done',   state === 'done');
    el.classList.toggle('lv-error',  state === 'error');
    if (getNodeDetail) {
      const detailEl = el.querySelector('.lv-node-detail');
      if (detailEl) {
        const nd  = LOOP_NODES.find(n => n.id === id);
        const txt = nd ? (getNodeDetail(nd) || '') : '';
        if (detailEl.textContent !== txt) detailEl.textContent = txt;
      }
    }
  });

  // Edges — restyle path class + marker (data-from set only on diagram edges, so
  // the static optimizer paths/labels appended by loop-logic.js are left alone)
  svg.querySelectorAll('path[data-from]').forEach(path => {
    const state = _edgeState(nodeStates, path.dataset.from, path.dataset.to);
    const isAlt = path.dataset.alt === '1';
    let cls = 'lv-arrow';
    if (isAlt)                   cls += ' lv-arrow-alt';
    if (state === 'done')        cls += ' lv-arrow-done';
    else if (state === 'active') cls += ' lv-arrow-active';
    path.setAttribute('class', cls);
    const mSuffix = state === 'done' ? '-done' : state === 'active' ? '-active' : '';
    path.setAttribute('marker-end', `url(#${p}-ah${mSuffix})`);
  });

  // Edge labels — toggle active styling
  svg.querySelectorAll('text[data-from]').forEach(text => {
    const state = _edgeState(nodeStates, text.dataset.from, text.dataset.to);
    text.setAttribute('class', state ? 'lv-arrow-label lv-arrow-label-active' : 'lv-arrow-label');
  });
}

// ── Shared renderer ────────────────────────────────────────────────────────────
// Renders stage columns/bands, edges, and node HTML onto containerEl.
// Returns { rootEl, svgEl, layout } so callers can extend the SVG.
//
// opts:
//   availableWidth  — px width to lay out for
//   markerPrefix    — unique SVG marker ID prefix (avoids DOM clashes)
//   genuiH         — override SVG height
//   nodeFilter      — array of node IDs to show; null/empty = show all
//   getNodeDetail   — (nodeDef) => string
//   onNodeClick     — (nodeDef, nodeEl, rootEl) => void
//   decorateNode    — (nodeDef, nodeEl) => void
// ── renderLoopDiagram ─────────────────────────────────────────────────────────
// opts.nodeConfig — Map<nodeId, {enabled: boolean}> or null.
//   When a node's enabled value is false the element gets the CSS class
//   'lv-disabled', which callers can style (e.g. muted color, strikethrough).
//   Only nodes in TOGGLEABLE_NODES should ever appear with enabled=false;
//   all other nodes are structurally required and the class won't be applied.
export function renderLoopDiagram(containerEl, nodeStates, {
  availableWidth = 0,
  markerPrefix   = 'ld',
  genuiH        = 0,
  nodeFilter     = null,
  excludeNodes   = null,
  extraEdges     = null,
  nodeLabelMap   = null,
  nodeConfig     = null,   // Map<nodeId, {enabled: boolean}> — drives lv-disabled
  getNodeDetail  = () => '',
  onNodeClick    = null,
  decorateNode   = null,
} = {}) {
  const cw = availableWidth > 0
    ? availableWidth
    : (containerEl.clientWidth || containerEl.offsetWidth || LOOP_W);

  const layout = cw < BREAKPOINT_VERTICAL
    ? buildVerticalLayout(cw)
    : buildHorizontalLayout(cw);

  // Apply nodeFilter — restrict to specific node IDs when agent has loop_logic set
  const filterSet  = (nodeFilter   && nodeFilter.length   > 0) ? new Set(nodeFilter)   : null;
  const excludeSet = (excludeNodes && excludeNodes.length > 0) ? new Set(excludeNodes) : null;

  let visibleNodes = filterSet
    ? layout.nodes.filter(n => filterSet.has(n.id))
    : layout.nodes;
  let visibleEdges = filterSet
    ? LOOP_EDGES.filter(e => filterSet.has(e.from) && filterSet.has(e.to))
    : LOOP_EDGES;

  if (excludeSet) {
    visibleNodes = visibleNodes.filter(n => !excludeSet.has(n.id));
    visibleEdges = visibleEdges.filter(e => !excludeSet.has(e.from) && !excludeSet.has(e.to));
  }

  if (extraEdges && extraEdges.length > 0) {
    const visibleIds = new Set(visibleNodes.map(n => n.id));
    visibleEdges = [...visibleEdges, ...extraEdges.filter(e => visibleIds.has(e.from) && visibleIds.has(e.to))];
  }

  const svgH = genuiH > 0 ? genuiH : layout.genuiH;

  const root = document.createElement('div');
  root.style.cssText = `position:relative;width:${layout.genuiW}px;min-height:${svgH}px;flex-shrink:0;margin:0 auto;`;
  containerEl.appendChild(root);

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width',   layout.genuiW);
  svg.setAttribute('height',  svgH);
  svg.setAttribute('viewBox', `0 0 ${layout.genuiW} ${svgH}`);
  svg.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;z-index:0;overflow:visible;';
  root.appendChild(svg);

  // Arrowhead markers — prefix keeps IDs unique when both views coexist in DOM
  const p = markerPrefix;
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  defs.innerHTML = `
    <marker id="${p}-ah"        markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#3a3a5a"/></marker>
    <marker id="${p}-ah-active" markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#7dcfff"/></marker>
    <marker id="${p}-ah-done"   markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#9ece6a"/></marker>
    <marker id="${p}-ah-opt"    markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#9ece6a50"/></marker>
  `;
  svg.appendChild(defs);

  // Stage backgrounds and labels
  if (layout.mode === 'vertical') {
    _renderStagesVertical(svg, layout.stages, layout.genuiW);
  } else {
    _renderStagesHorizontal(svg, layout.stages, svgH);
  }

  // Draw edges. data-from/data-to + data-alt let applyDiagramStates() restyle a
  // path/label in place on live events without rebuilding the whole diagram.
  const edgeLayout = { mode: layout.mode, genuiW: layout.genuiW };
  for (const edge of visibleEdges) {
    const edgeState = _edgeState(nodeStates, edge.from, edge.to);
    const pi        = computeEdgePath(edge, layout.nodes, edgeLayout);
    if (!pi) continue;

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d',    pi.d);
    path.setAttribute('fill', 'none');

    const isAlt = layout.mode === 'horizontal'
      ? (edge.above || edge.loopback || edge.below)
      : (edge.loopback || edge.below || (edge.from === 'llm_call' && edge.to === 'check_continue'));

    let cls = 'lv-arrow';
    if (isAlt)                   cls += ' lv-arrow-alt';
    if (edgeState === 'done')    cls += ' lv-arrow-done';
    else if (edgeState === 'active') cls += ' lv-arrow-active';
    path.setAttribute('class', cls);

    const mSuffix = edgeState === 'done' ? '-done' : edgeState === 'active' ? '-active' : '';
    path.setAttribute('marker-end', `url(#${p}-ah${mSuffix})`);
    path.dataset.from = edge.from;
    path.dataset.to   = edge.to;
    if (isAlt) path.dataset.alt = '1';
    svg.appendChild(path);

    if (edge.label) {
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', pi.labelX);
      text.setAttribute('y', pi.labelY);
      text.setAttribute('text-anchor', 'middle');
      text.setAttribute('class', edgeState ? 'lv-arrow-label lv-arrow-label-active' : 'lv-arrow-label');
      text.textContent = edge.label;
      text.dataset.from = edge.from;
      text.dataset.to   = edge.to;
      svg.appendChild(text);
    }
  }

  // Render node HTML elements
  for (const nd of visibleNodes) {
    const state = nodeStates.get(nd.id) || '';
    const el = document.createElement('div');
    el.className = `lv-node lv-type-${nd.type}`;
    el.dataset.id = nd.id;   // sidebar uses this to scroll a node into view
    if (state === 'active')     el.classList.add('lv-active');
    else if (state === 'done')  el.classList.add('lv-done');
    else if (state === 'error') el.classList.add('lv-error');

    // Disabled state — only applied to TOGGLEABLE_NODES when nodeConfig says so
    const _ncEntry = nodeConfig && nodeConfig.get(nd.id);
    if (_ncEntry && _ncEntry.enabled === false && TOGGLEABLE_NODES.has(nd.id)) {
      el.classList.add('lv-disabled');
    }

    el.style.left   = (nd.cx - nd.hw) + 'px';
    el.style.top    = (nd.cy - nd.hh) + 'px';
    el.style.width  = (nd.hw * 2) + 'px';
    el.style.height = (nd.hh * 2) + 'px';

    const labelEl = document.createElement('span');
    labelEl.className = 'lv-node-label';
    labelEl.textContent = (nodeLabelMap && nodeLabelMap[nd.id]) ? nodeLabelMap[nd.id] : nd.label;
    el.appendChild(labelEl);

    const detailEl = document.createElement('div');
    detailEl.className = 'lv-node-detail';
    detailEl.textContent = getNodeDetail(nd) || '';
    el.appendChild(detailEl);

    if (decorateNode) decorateNode(nd, el);

    if (onNodeClick) {
      el.style.cursor = 'pointer';
      el.addEventListener('click', e => { e.stopPropagation(); onNodeClick(nd, el, root); });
    }

    root.appendChild(el);
  }

  return { rootEl: root, svgEl: svg, layout };
}

function _renderStagesHorizontal(svg, stages, svgH) {
  stages.forEach((stage, i) => {
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x',      stage.x1 + 1);
    rect.setAttribute('y',      28);
    rect.setAttribute('width',  stage.x2 - stage.x1 - 2);
    rect.setAttribute('height', svgH - 28);
    rect.setAttribute('fill',   i % 2 === 0 ? '#ffffff03' : '#00000008');
    rect.setAttribute('rx',     '3');
    svg.appendChild(rect);

    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x',           (stage.x1 + stage.x2) / 2);
    text.setAttribute('y',           20);
    text.setAttribute('text-anchor', 'middle');
    text.setAttribute('class',       'lv-stage-label');
    text.setAttribute('fill',        stage.color);
    text.textContent = stage.label;
    svg.appendChild(text);
  });
}

function _renderStagesVertical(svg, stages, genuiW) {
  stages.forEach((stage, i) => {
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x',      1);
    rect.setAttribute('y',      stage.y1);
    rect.setAttribute('width',  genuiW - 2);
    rect.setAttribute('height', stage.y2 - stage.y1);
    rect.setAttribute('fill',   i % 2 === 0 ? '#ffffff03' : '#00000008');
    rect.setAttribute('rx',     '3');
    svg.appendChild(rect);

    if (i > 0) {
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', 0);       line.setAttribute('y1', stage.y1);
      line.setAttribute('x2', genuiW); line.setAttribute('y2', stage.y1);
      line.setAttribute('stroke', '#1e2035');
      line.setAttribute('stroke-width', '1');
      svg.appendChild(line);
    }

    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x',           6);
    text.setAttribute('y',           stage.y1 + (stage.y2 - stage.y1) / 2 + 4);
    text.setAttribute('text-anchor', 'start');
    text.setAttribute('class',       'lv-stage-label');
    text.setAttribute('fill',        stage.color);
    text.textContent = stage.label;
    svg.appendChild(text);
  });
}
