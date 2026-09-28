'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// This module ships NO colour literals of its own beyond the documented palette
// DEFAULTS / preset definitions, which mirror the design-system.css palette
// blocks + the ui/shared/js/appearance.js DEFAULTS.

/**
 * Appearance editor — shared theme-table definitions & colour math.
 *
 * The SINGLE SOURCE for the palette token metadata, the one-click preset themes
 * and the hex/opacity helpers used to build the Light/Dark theme editor. Two
 * panels render that editor and BOTH import from here, so the token list, the
 * presets and the colour maths never drift between them:
 *   • Admin (global theme)  — ui/admin-tools/instances/app-config/app-settings/app-settings.js
 *   • Per-user (own theme)  — ui/shared/js/appearance-me.js  (the "My appearance"
 *                             editor on the account page, ui/shared/js/account.js)
 *
 * The swatch ORDER + which CSS variable each token drives is owned by
 * ui/shared/js/appearance.js (window.WA_APPEARANCE.tokens); here we only attach a
 * friendly label + icon + flags. The per-theme saved-value key for a token is
 * `<base>_<theme>` (the border's is the historical `border_color_<theme>` — same
 * shape, since its base is `border_color`).
 *
 * MIRROR: any change to the table's look/structure must be applied in BOTH
 * builders (admin _buildThemeCard + appearance-me.js buildThemeBody).
 */

// Label + Lucide icon per palette token (base key from WA_APPEARANCE.tokens).
// `alpha: true` adds an opacity slider next to the colour swatch (surfaces +
// bubbles); the colour + opacity combine into a #rrggbbaa value. `follow: true`
// tokens default to the theme (blank) and are left out of preset patches so
// they keep tracking whatever the preset sets.
export const TOKEN_META = {
  accent:        { label: 'Accent',       icon: 'sparkles' },
  secondary:     { label: 'Secondary',    icon: 'sparkle' },
  success:       { label: 'Success',      icon: 'circle-check' },
  warning:       { label: 'Warning',      icon: 'triangle-alert' },
  danger:        { label: 'Danger',       icon: 'octagon-alert' },
  surface_bg:    { label: 'Background',   icon: 'square', alpha: true },
  surface_panel: { label: 'Panel',       icon: 'square', alpha: true },
  surface_tint:  { label: 'Tint',        icon: 'square', alpha: true },
  text:          { label: 'Text',        icon: 'type' },
  text_muted:    { label: 'Muted text',  icon: 'type' },
  ambient:       { label: 'Ambient glow', icon: 'sun-dim' },
  border_color:  { label: 'Border',      icon: 'square-dashed', alpha: true },
  user_bubble:        { label: 'User bubble',        icon: 'message-circle', alpha: true, follow: 'panel' },
  user_bubble_border: { label: 'User bubble border', icon: 'square-dashed',  alpha: true, follow: 'border' },
  agent_bubble:       { label: 'Agent bubble',       icon: 'bot',            alpha: true, follow: 'panel' },
  agent_bubble_border:{ label: 'Agent bubble border',icon: 'square-dashed',  alpha: true, follow: 'border' },
  chat_pill_bg:       { label: 'Chat pill',          icon: 'panel-bottom',   alpha: true, follow: 'panel' },
  chat_pill_border:   { label: 'Chat pill border',   icon: 'square-dashed',  alpha: true, follow: 'border' },
};

// Design-system defaults per theme (mirror of the appearance.js DEFAULTS).
// Used to fall back when a saved value is blank and as the base a preset
// merges onto, so switching presets always lands on a complete palette.
export const APP_DEFAULTS = {
  dark: {
    accent: '#ffffff', secondary: '#d9d9d9', success: '#22c55e', warning: '#eab308',
    danger: '#ef4444', surface_bg: '#000000', surface_panel: '#0d0d0d',
    surface_tint: '#1a1a1a', text: '#ffffff', text_muted: '#a3a3a3',
    ambient: '#404040', border_color: '#333333',
    // Bubbles + chat pill are `follow` tokens — NO hard-coded default here. Their
    // swatch is derived live from the Panel colour via followDefault() so the
    // table tracks the active theme instead of a frozen stock colour.
  },
  light: {
    accent: '#000000', secondary: '#262626', success: '#16a34a', warning: '#ca8a04',
    danger: '#dc2626', surface_bg: '#ffffff', surface_panel: '#f5f5f5',
    surface_tint: '#e8e8e8', text: '#000000', text_muted: '#666666',
    ambient: '#d9d9d9', border_color: '#cccccc',
    // (see dark) — follow tokens derive from the live Panel colour.
  },
};

// The panel-tint opacity index.css uses for a blank bubble/pill, per theme
// (color-mix(var(--bg-elev) 82%/85%) in ui/shared/css/index.css). Kept here so
// the swatch's default opacity matches what actually paints.
export const FOLLOW_ALPHA = { dark: 82, light: 85 };

// One-click preset themes. Each lists the DISTINCTIVE hues AND its own neutral
// "temperature" (surfaces / text / border) per theme, so applying a preset
// re-tints the WHOLE UI — not just the accent. Merged onto APP_DEFAULTS, so any
// key omitted falls back to the default. Bubbles are intentionally NOT listed
// (they're `follow` tokens — they track whatever surfaces/accent the preset
// sets). The app DEFAULT is the first preset: a strictly achromatic Black & White
// palette with white-on-black dark mode and black-on-white light mode (renamed "Slate").
export const PRESETS = [
  { id: 'default', name: 'Slate',
    dark:  { ...APP_DEFAULTS.dark },
    light: { ...APP_DEFAULTS.light } },
  { id: 'amber', name: 'Amber',
    dark:  { accent: '#e0a35e', secondary: '#c8915a', ambient: '#7a5636', border_color: '#3a2c1e',
             surface_bg: '#16100b', surface_panel: '#241a12', surface_tint: '#36281c',
             text: '#ece0d2', text_muted: '#9a8266' },
    light: { accent: '#ff8c42', secondary: '#7a4abf', ambient: '#ffb478', border_color: '#ffdec4',
             surface_bg: '#fffaf5', surface_panel: '#fff2e6', surface_tint: '#ffeee0',
             text: '#3d2c2e', text_muted: '#b09580' } },
  { id: 'ocean', name: 'Ocean',
    dark:  { accent: '#5ec8f0', secondary: '#7aa2f7', ambient: '#3b6ea5', border_color: '#22384f',
             surface_bg: '#0a0f17', surface_panel: '#13202e', surface_tint: '#213346',
             text: '#cdd9e8', text_muted: '#5e7290' },
    light: { accent: '#1f8fbf', secondary: '#3b6ea5', ambient: '#9fd6ec', border_color: '#cfe6f2',
             surface_bg: '#f5fafd', surface_panel: '#e9f3fa', surface_tint: '#dbeaf5',
             text: '#163040', text_muted: '#5a7589' } },
  { id: 'forest', name: 'Forest',
    dark:  { accent: '#9ece6a', secondary: '#73daca', ambient: '#3f6f4a', border_color: '#25402c',
             surface_bg: '#0a120c', surface_panel: '#15241a', surface_tint: '#23382a',
             text: '#cfe0cf', text_muted: '#5f7a64' },
    light: { accent: '#4f9d54', secondary: '#2f8f7a', ambient: '#bfe6b0', border_color: '#d3ebcf',
             surface_bg: '#f6fbf5', surface_panel: '#e9f5e7', surface_tint: '#dbefd8',
             text: '#1b3a1f', text_muted: '#5a7a58' } },
  { id: 'grape', name: 'Grape',
    dark:  { accent: '#bb9af7', secondary: '#c4a7f0', ambient: '#7a4abf', border_color: '#382955',
             surface_bg: '#100b1a', surface_panel: '#1f162e', surface_tint: '#322347',
             text: '#ddd2ec', text_muted: '#6a5a85' },
    light: { accent: '#7a4abf', secondary: '#9a6fd0', ambient: '#d3b8f0', border_color: '#e4d4f2',
             surface_bg: '#faf7fe', surface_panel: '#f2ecfb', surface_tint: '#e8ddf7',
             text: '#2c1f3a', text_muted: '#7a6a88' } },
  { id: 'rose', name: 'Rose',
    dark:  { accent: '#f7a8c0', secondary: '#f7768e', ambient: '#a8456a', border_color: '#472837',
             surface_bg: '#160a10', surface_panel: '#29161f', surface_tint: '#42222f',
             text: '#ecd2da', text_muted: '#856a72' },
    light: { accent: '#d44872', secondary: '#e06a8c', ambient: '#f2bcd0', border_color: '#f2d4de',
             surface_bg: '#fef7f9', surface_panel: '#fbecf1', surface_tint: '#f7dde6',
             text: '#3a1f28', text_muted: '#855f6a' } },
];

// Saved-value key for a token base under a theme.
export function keyFor(base, theme) { return `${base}_${theme}`; }

// type="color" only accepts #rrggbb — guard a stored non-hex value so a swatch
// hand-edited to rgb()/a name in the JSON doesn't silently reset to black.
// Accepts #rrggbb and #rrggbbaa (the alpha is dropped — the colour input shows
// only the rgb part; opacity lives in the separate slider).
export function asHex(val, fallback) {
  const m = (typeof val === 'string') && /^#([0-9a-f]{6})(?:[0-9a-f]{2})?$/i.exec(val.trim());
  return m ? '#' + m[1] : fallback;
}

// Split a stored colour into { hex:'#rrggbb', alpha:0..100 }. A bare #rrggbb
// (or anything non-alpha) is fully opaque (100). Used to seed the swatch +
// opacity slider for alpha-capable tokens.
export function splitColor(val, fallbackHex) {
  const v = (typeof val === 'string') ? val.trim() : '';
  const m = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(v);
  if (!m) return { hex: fallbackHex || '#000000', alpha: 100 };
  const alpha = m[2] != null ? Math.round(parseInt(m[2], 16) / 255 * 100) : 100;
  return { hex: '#' + m[1], alpha };
}

// Combine a #rrggbb + opacity (0..100) back into a stored value: #rrggbb when
// fully opaque (keeps values tidy), otherwise #rrggbbaa.
export function combineColor(hex, alphaPct) {
  const h = asHex(hex, '#000000');
  const a = Math.max(0, Math.min(100, Number(alphaPct)));
  if (a >= 100) return h;
  const aa = ('0' + Math.round(a / 100 * 255).toString(16)).slice(-2);
  return h + aa;
}
