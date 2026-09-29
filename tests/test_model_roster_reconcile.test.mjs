import { test } from 'node:test';
import assert from 'node:assert/strict';

import { reconcileSavedModelRows } from '../ui/shared/js/model-roster-reconcile.js';

test('existing duplicate models reconcile only by entry_id across server reordering', () => {
  const local = [
    { _uid: 11, entry_id: 'one', provider: 'openai', model: 'same', credential_suffix: 'old-1' },
    { _uid: 12, entry_id: 'two', provider: 'openai', model: 'same', credential_suffix: 'old-2' },
  ];
  reconcileSavedModelRows(local, [
    { entry_id: 'two', provider: 'openai', model: 'same', credential_suffix: 'key-2' },
    { entry_id: 'one', provider: 'openai', model: 'same', credential_suffix: 'key-1' },
  ]);

  assert.deepEqual(local.map(row => [row._uid, row.entry_id, row.credential_suffix]), [
    [11, 'one', 'key-1'],
    [12, 'two', 'key-2'],
  ]);
});

test('new identical models receive distinct server IDs by credential suffix', () => {
  const local = [
    { _uid: 21, entry_id: '', provider: 'openai', model: 'same', api_key: 'secret-AAAAA' },
    { _uid: 22, entry_id: '', provider: 'openai', model: 'same', api_key: 'secret-BBBBB' },
  ];
  reconcileSavedModelRows(local, [
    { entry_id: 'server-two', provider: 'openai', model: 'same', credential_suffix: 'BBBBB' },
    { entry_id: 'server-one', provider: 'openai', model: 'same', credential_suffix: 'AAAAA' },
  ]);

  assert.deepEqual(local.map(row => [row._uid, row.entry_id]), [
    [21, 'server-one'],
    [22, 'server-two'],
  ]);
});

test('indistinguishable new duplicates consume each returned row once', () => {
  const local = [
    { entry_id: '', provider: 'openai', model: 'same' },
    { entry_id: '', provider: 'openai', model: 'same' },
  ];
  reconcileSavedModelRows(local, [
    { entry_id: 'server-one', provider: 'openai', model: 'same' },
    { entry_id: 'server-two', provider: 'openai', model: 'same' },
  ]);

  assert.deepEqual(local.map(row => row.entry_id), ['server-one', 'server-two']);
  assert.equal(new Set(local.map(row => row.entry_id)).size, 2);
});

test('a structurally different returned row is never adopted as a new identity', () => {
  const local = [{ entry_id: '', provider: 'openai', model: 'wanted' }];
  reconcileSavedModelRows(local, [
    { entry_id: 'other', provider: 'anthropic', model: 'different' },
  ]);
  assert.equal(local[0].entry_id, '');
});
