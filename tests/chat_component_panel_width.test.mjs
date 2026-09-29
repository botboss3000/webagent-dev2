import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';

const source = await fs.readFile(
  new URL('../ui/chat/js/chat-components.js', import.meta.url),
  'utf8',
);
const helperSource = source.match(
  /export function _fullViewHorizontalBounds\(chatPanel, inner\) \{[\s\S]*?\n\}/,
)?.[0];
assert.ok(helperSource, 'width helper remains exported for geometry coverage');
const boundsFor = Function(
  `${helperSource.replace(/^export\s+/, '')}; return _fullViewHorizontalBounds;`,
)();

test('opened chat element follows the rendered transcript width on mobile', () => {
  const chatPanel = { getBoundingClientRect: () => ({ left: 0, right: 390, width: 390 }) };
  const inner = { getBoundingClientRect: () => ({ left: 8, right: 382, width: 374 }) };
  assert.deepEqual(boundsFor(chatPanel, inner), { left: 8, width: 374 });
});

test('opened chat element follows an already-capped desktop transcript', () => {
  const chatPanel = { getBoundingClientRect: () => ({ left: 100, right: 1300, width: 1200 }) };
  const inner = { getBoundingClientRect: () => ({ left: 200, right: 1200, width: 1000 }) };
  assert.deepEqual(boundsFor(chatPanel, inner), { left: 200, width: 1000 });
  assert.match(source, /const bounds = _fullViewHorizontalBounds\(chatPanel, inner\);/);
  assert.doesNotMatch(source, /parseFloat\(rawMax\)/);
});
