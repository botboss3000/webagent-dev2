import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  modelAddGuard, MODEL_ADD_DUPLICATE, MODEL_ADD_SELF_LINK,
} from '../ui/shared/js/model-add-guard.js';

// REGRESSION: the alias feature reused the live configurator, so the Add button
// drove both normal adds and backup adds. A normal add has no alias target and
// (outside the WebAgent Catalog) no duplicate lookup, so the previous
// `dupe === aliasTarget` self-link check evaluated null === null and blocked
// EVERY normal Add with the backup-only message. A normal ChatGPT-subscription
// add therefore "errored" instead of saving.

test('normal BYO/subscription add is never blocked by alias self-link state', () => {
  const { dupe, message } = modelAddGuard({
    platformCatalog: false,
    providers: [{ entry_id: 'existing', provider: 'openai_subscription', model: 'gpt-5' }],
    selectedModel: 'gpt-5',
    aliasTarget: null,
  });
  assert.equal(dupe, null);
  assert.equal(message, '');
});

test('normal catalog add of a new model is allowed', () => {
  const { dupe, message } = modelAddGuard({
    platformCatalog: true,
    providers: [{ entry_id: 'catalog-a', model: 'a' }],
    selectedModel: 'catalog-b',
    aliasTarget: null,
  });
  assert.equal(dupe, null);
  assert.equal(message, '');
});

test('normal catalog add of a model already in the roster is rejected', () => {
  const existing = { entry_id: 'catalog-a', model: 'a' };
  const { dupe, message } = modelAddGuard({
    platformCatalog: true,
    providers: [existing],
    selectedModel: 'catalog-a',
    aliasTarget: null,
  });
  assert.equal(dupe, existing);
  assert.equal(message, MODEL_ADD_DUPLICATE);
});

test('alias add of a different connection is allowed', () => {
  const parent = { entry_id: 'parent', model: 'a' };
  const { dupe, message } = modelAddGuard({
    platformCatalog: true,
    providers: [parent],
    selectedModel: 'catalog-b',
    aliasTarget: parent,
  });
  assert.equal(dupe, null);
  assert.equal(message, '');
});

test('alias add cannot self-link back to its own parent', () => {
  const parent = { entry_id: 'parent', model: 'a' };
  const { dupe, message } = modelAddGuard({
    platformCatalog: true,
    providers: [parent],
    selectedModel: 'parent',
    aliasTarget: parent,
  });
  assert.equal(dupe, parent);
  assert.equal(message, MODEL_ADD_SELF_LINK);
});

test('a duplicate catalog row different from the alias parent links instead of blocking', () => {
  const parent = { entry_id: 'parent', model: 'a' };
  const other = { entry_id: 'catalog-other', model: 'b' };
  const { dupe, message } = modelAddGuard({
    platformCatalog: true,
    providers: [parent, other],
    selectedModel: 'catalog-other',
    aliasTarget: parent,
  });
  assert.equal(dupe, other);
  assert.equal(message, '');
});
