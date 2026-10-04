import test from "node:test";
import assert from "node:assert/strict";
import {walletActivityView} from "../wallet-activity-view.js";
import {originalSaleHistoryUnknown} from "../decision-view.js";

test("found sales and transfers are not hidden behind a universal unresolved label", () => {
  const thesis = {wallet_activity:{status:"backfilling",wallets_checked:2,wallets_total:10,amounts_tokens:{sold:25,transferred:10}}};
  const view = walletActivityView(thesis);
  assert.equal(view.label,"Sales + transfers observed");
  assert.match(view.reason,/2\/10/);
  assert.equal(view.checked,false);
  assert.doesNotMatch(view.reason,/sale\/transfer unresolved/);
});

test("transfers, custody, and missing facts never become sales", () => {
  assert.equal(walletActivityView({wallet_activity:{amounts_tokens:{transferred:25}}}).label,"Transfers observed");
  assert.equal(walletActivityView({wallet_activity:{amounts_tokens:{service:25}}}).label,"Service outflow observed");
  assert.equal(walletActivityView({}).label,"Original-wallet outflow");
  assert.match(walletActivityView({}).reason,/queued/);
  assert.equal(walletActivityView({wallet_activity:{amounts_tokens:{sold:"25"}}}).sold,false);
});

test("legacy pool observations remain visible while wallet reconstruction is pending", () => {
  assert.match(walletActivityView({outflow_evidence:{observed_sale_transactions:3}}).reason,/Sale trades observed/);
  assert.equal(originalSaleHistoryUnknown({retention_evidence_version:3,original_sale_history_status:"reconstructed_from_capture"}),true);
  assert.equal(originalSaleHistoryUnknown({retention_evidence_version:2,original_sale_history_status:"reconstructed_from_capture"}),true);
});

test("history recovery requires matching complete wallet evidence, not a label", () => {
  const t = {retention_evidence_version:3,original_sale_history_status:"reconstructed_from_capture",
    wallet_activity:{status:"checked",interpretation_complete:true,wallet_coverage_pct:100,token_coverage_pct:100,checked_at:"2026-10-04T12:00:00Z"},
    sale_history_recovery:{checked_at:"2026-10-04T12:00:00Z"}};
  assert.equal(originalSaleHistoryUnknown(t),false);
  for (const patch of [{status:"backfilling"},{interpretation_complete:false},{token_coverage_pct:null},{wallet_coverage_pct:50}]) {
    assert.equal(originalSaleHistoryUnknown({...t,wallet_activity:{...t.wallet_activity,...patch}}),true);
  }
  assert.equal(originalSaleHistoryUnknown({...t,sale_history_recovery:{checked_at:"older"}}),true);
});
