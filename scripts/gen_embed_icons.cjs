// One-shot generator: extracts SVG markup for the curated agent-icon set from
// the bundled Lucide UMD and writes data/config/embed_icons.json.
// Run: node scripts/gen_embed_icons.cjs   (from repo root)
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const BUNDLE = path.join(ROOT, 'ui/vendor/lucide/lucide.min.js');
const OUT = path.join(ROOT, 'data/config/embed_icons.json');
const PICKER = path.join(ROOT, 'ui/shared/js/icon-picker.js');

// Parse the curated icon names out of icon-picker.js (ICON_PICKER_ICONS array).
const pickerSrc = fs.readFileSync(PICKER, 'utf8');
const m = pickerSrc.match(/ICON_PICKER_ICONS\s*=\s*\[([\s\S]*?)\]/);
if (!m) throw new Error('ICON_PICKER_ICONS not found');
const names = [...m[1].matchAll(/'([a-z0-9-]+)'/g)].map((x) => x[1]);
console.log('pickup icons:', names.length);

// Load lucide UMD in a sandbox and grab its icon table (PascalCase keys).
const ctx = {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(BUNDLE, 'utf8'), ctx);
const icons = ctx.lucide.icons;
console.log('lucide icons available:', Object.keys(icons).length);

const kebabToPascal = (name) =>
  name.split('-').map((s) => s.charAt(0).toUpperCase() + s.slice(1)).join('');

function serialize(node) {
  const [tag, attrs, children] = node;
  const a = Object.entries(attrs || {})
    .map(([k, v]) => `${k}="${String(v).replace(/"/g, '&quot;')}"`)
    .join(' ');
  if (Array.isArray(children) && children.length) {
    return `<${tag} ${a}>${children.map(serialize).join('')}</${tag}>`;
  }
  return `<${tag} ${a}/>`;
}

const out = {};
let missing = [];
for (const name of names) {
  const pascal = kebabToPascal(name);
  const data = icons[pascal];
  if (!data) { missing.push(name); continue; }
  out[name] = serialize(data);
}
// Always include the classic chat bubble for the built-in fallback.
for (const extra of ['message-circle', 'bot']) {
  if (!out[extra] && icons[kebabToPascal(extra)]) {
    out[extra] = serialize(icons[kebabToPascal(extra)]);
  }
}

fs.writeFileSync(OUT, JSON.stringify(out, null, 0), 'utf8');
console.log('wrote', Object.keys(out).length, 'icons ->', OUT);
console.log('size:', fs.statSync(OUT).size, 'bytes');
if (missing.length) console.log('MISSING (will fall back):', missing.join(', '));
