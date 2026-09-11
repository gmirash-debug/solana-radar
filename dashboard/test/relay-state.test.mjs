import test from 'node:test';
import assert from 'node:assert/strict';
import {ageFilterLabel, relaySignalLabel, validSnapshot} from '../robinhood-state.js';

test('age filter label follows snapshot, including old publication', () => {
  assert.equal(ageFilterLabel({min_pool_age_hours:0, max_pool_age_hours:360}), 'From launch - 15d');
  assert.equal(ageFilterLabel({min_pool_age_hours:24, max_pool_age_hours:360}), '1d - 15d');
  assert.equal(ageFilterLabel(null), 'Age filter unavailable');
});
test('frozen Relay reason survives missing current wave', () => {
  assert.equal(relaySignalLabel({cohort_reason:'Relay buy wave'}), 'Relay buy wave');
  assert.equal(relaySignalLabel({relay:{wave:{}}}), 'Relay wave / holding unconfirmed');
  assert.equal(relaySignalLabel({}), '');
});
test('malformed Relay timestamps fail snapshot validation', () => {
  const token = '0x' + '1'.repeat(40);
  const t = {token, pool:'0x'+'2'.repeat(40), key:`4663:${token}`, wallets:[]};
  assert.equal(validSnapshot({chain_id:4663, tokens:[t]}), true);
  t.relay = {wave:{to_timestamp:'invalid'}};
  assert.equal(validSnapshot({chain_id:4663, tokens:[t]}), false);
});
