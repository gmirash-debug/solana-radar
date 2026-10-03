import copy
import time
import unittest
from unittest.mock import Mock, patch

from eth_abi import encode
import robinhood as rh
from tools.build_pages import choose_robinhood


TOKEN, QUOTE, POOL, WALLET, ROUTER = ["0x" + str(n) * 40 for n in range(1, 6)]


def topic(addr):
    return "0x" + addr[2:].rjust(64, "0")


def fixture(recipient=WALLET, received=100, status="0x1"):
    return {"status": status, "from": WALLET, "blockNumber": "0x64", "logs": [
        {"address": POOL, "topics": [rh.SWAP, topic(ROUTER), topic(recipient)],
         "data": "0x" + encode(["int256", "int256", "uint160", "uint128", "int24"], [-100, 5, 1, 1, 0]).hex()},
        {"address": TOKEN, "topics": [rh.TRANSFER, topic(POOL), topic(WALLET)],
         "data": "0x" + encode(["uint256"], [received]).hex()}]}


def transfer(sender, recipient, amount=100, token=TOKEN):
    return {"address": token, "topics": [rh.TRANSFER, topic(sender), topic(recipient)],
            "data": "0x" + encode(["uint256"], [amount]).hex()}


class RobinhoodTests(unittest.TestCase):
    def test_chain_scoped_key_and_address_validation(self):
        self.assertEqual(rh.token_key(TOKEN.upper().replace("0X", "0x")), f"4663:{TOKEN}")
        with self.assertRaises(ValueError):
            rh.token_key("SolanaMintpump")

    def test_direct_buy(self):
        self.assertEqual(rh.routed_buy(fixture(), {"pool": POOL, "token": TOKEN}, 0), (WALLET, 100))

    def test_router_not_treated_as_wallet(self):
        self.assertEqual(rh.routed_buy(fixture(ROUTER), {"pool": POOL, "token": TOKEN}, 0), (WALLET, 100))

    def test_fee_on_transfer_not_full_buy(self):
        self.assertIsNone(rh.routed_buy(fixture(received=90), {"pool": POOL, "token": TOKEN}, 0))

    def test_failed_transaction(self):
        self.assertIsNone(rh.routed_buy(fixture(status="0x0"), {"pool": POOL, "token": TOKEN}, 0))

    def test_transfer_alone_is_not_buy(self):
        receipt = fixture()
        receipt["logs"] = receipt["logs"][1:]
        self.assertIsNone(rh.routed_buy(receipt, {"pool": POOL, "token": TOKEN}, 0))

    def test_buy_and_forward_excluded(self):
        receipt = fixture()
        receipt["logs"].append({"address": TOKEN, "topics": [rh.TRANSFER, topic(WALLET), topic(ROUTER)],
            "data": "0x" + encode(["uint256"], [100]).hex()})
        self.assertIsNone(rh.routed_buy(receipt, {"pool": POOL, "token": TOKEN}, 0))

    def test_multi_swap_ambiguous(self):
        receipt = fixture()
        receipt["logs"].append(receipt["logs"][0])
        self.assertIsNone(rh.routed_buy(receipt, {"pool": POOL, "token": TOKEN}, 0))

    def test_unrelated_inflow_does_not_make_sender_the_pool_buyer(self):
        recipient, donor = "0x" + "6" * 40, "0x" + "7" * 40
        receipt = fixture()
        receipt["logs"][1:] = [transfer(POOL, recipient), transfer(donor, WALLET)]
        pool = {"pool": POOL, "token": TOKEN}
        self.assertEqual(rh.transfer_net(receipt, TOKEN, WALLET), 100)
        self.assertEqual(rh.transfer_net(receipt, TOKEN, POOL), -100)
        self.assertIsNone(rh.routed_buy(receipt, pool, 0))
        rpc = Mock()
        self.assertIsNone(rh.beneficiary_buy(receipt, pool, 0, rpc))
        rpc.call.assert_not_called()

    def test_incremental_scan_does_not_export_unrelated_inflow_as_a_buyer(self):
        receipt = fixture()
        receipt.update(transactionHash="tx", blockHash="hash")
        receipt["logs"][1:] = [transfer(POOL, "0x" + "6" * 40), transfer("0x" + "7" * 40, WALLET)]
        swap = dict(receipt["logs"][0], blockNumber="0x64", blockHash="hash", transactionHash="tx", logIndex="0x1")
        rpc = Mock()
        rpc.contract.side_effect = [(6,), (1000,)]
        rpc.call.side_effect = [{"hash": "hash"}, receipt]
        pool = {"pool": POOL, "token": TOKEN, "key": rh.token_key(TOKEN)}
        with patch.object(rh, "verify_pool", return_value=0), patch.object(rh, "get_logs", return_value=[swap]), patch.object(
                rh, "security_check", return_value={"status": "unknown", "flags": []}):
            result = rh.inspect_incremental(rpc, pool, 1, 100, rh.CONFIG, rh.Store(), Mock(), 0)
        self.assertEqual(result["attributed_buy_transactions"], 0)
        self.assertFalse(result["attribution_complete"])
        self.assertEqual(result["wallets"], [])
        self.assertIsNone(result["retained_supply_upper_bound_pct"])

    def test_pool_output_can_pass_through_zero_net_router(self):
        receipt = fixture(ROUTER)
        receipt["logs"][1:] = [transfer(POOL, ROUTER), transfer(ROUTER, WALLET)]
        pool = {"pool": POOL, "token": TOKEN}
        self.assertEqual(rh.routed_buy(receipt, pool, 0), (WALLET, 100))
        rpc = Mock()
        rpc.call.return_value = "0x"
        self.assertEqual(rh.beneficiary_buy(receipt, pool, 0, rpc), (WALLET, 100))

    def test_prefunded_router_is_not_proven_by_later_reimbursement(self):
        receipt = fixture(ROUTER)
        receipt["logs"][1:] = [transfer(ROUTER, WALLET), transfer(POOL, ROUTER)]
        pool = {"pool": POOL, "token": TOKEN}
        self.assertIsNone(rh.routed_buy(receipt, pool, 0))
        rpc = Mock()
        self.assertIsNone(rh.beneficiary_buy(receipt, pool, 0, rpc))
        rpc.call.assert_not_called()

    def test_shared_manager_custody_cannot_identify_one_of_multiple_pools(self):
        pool_id = "0x" + "a" * 64
        receipt = fixture()
        swap = {"address": rh.POOL_MANAGER, "topics": [rh.SWAP_V4, pool_id, topic(ROUTER)],
                "data": "0x" + encode(["int128", "int128", "uint160", "uint128", "int24", "uint24"],
                                       [100, -5, 1, 1, 0, 3000]).hex()}
        receipt["logs"] = [swap, dict(swap, topics=[rh.SWAP_V4, "0x" + "b" * 64, topic(ROUTER)]),
                           transfer(rh.POOL_MANAGER, WALLET)]
        pool = {"protocol": "v4", "pool": pool_id, "token": TOKEN}
        self.assertEqual(rh.transfer_net(receipt, TOKEN, rh.POOL_MANAGER), -100)
        self.assertIsNone(rh.routed_buy(receipt, pool, 0))
        rpc = Mock()
        self.assertIsNone(rh.beneficiary_buy(receipt, pool, 0, rpc))
        rpc.call.assert_not_called()
        match = {"recipient": WALLET, "bought_raw": "100"}
        receipt["from"] = ROUTER
        self.assertIsNone(rh.verified_route_buy(receipt, pool, 0, match))

    def test_other_token_transfers_do_not_invalidate_proven_output(self):
        receipt = fixture()
        receipt["logs"].append(transfer(ROUTER, WALLET, token=QUOTE))
        self.assertEqual(rh.routed_buy(receipt, {"pool": POOL, "token": TOKEN}, 0), (WALLET, 100))

    def test_pool_return_and_reissue_is_ambiguous_despite_matching_net(self):
        receipt = fixture()
        receipt["logs"][1:] = [transfer(POOL, ROUTER), transfer(ROUTER, POOL), transfer(POOL, WALLET)]
        self.assertIsNone(rh.routed_buy(receipt, {"pool": POOL, "token": TOKEN}, 0))

    def test_budget_is_hard(self):
        session = Mock()
        rpc = rh.Rpc(budget=0, session=session)
        with self.assertRaisesRegex(rh.RpcError, "budget"):
            rpc.call("eth_chainId", [])
        session.post.assert_not_called()

    def test_wrong_network_preserves_last_good_snapshot(self):
        rpc = Mock(provider="test", calls=1)
        rpc.call.return_value = "0x1"
        old = {"chain_id": 4663, "generated_at": "2026-09-07T00:00:00Z", "tokens": [{"key": "old"}]}
        result = rh.scan(old, rpc=rpc)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["tokens"], old["tokens"])
        self.assertEqual(result["generated_at"], old["generated_at"])
        self.assertNotIn("status", old)

    def test_foreign_snapshot_not_preserved(self):
        rpc = Mock(provider="test", calls=1)
        rpc.call.return_value = "0x1"
        result = rh.scan({"chain_id": 1, "tokens": ["foreign"]}, rpc=rpc)
        self.assertEqual(result["tokens"], [])

    def test_factory_mismatch_fails_closed(self):
        rpc = Mock()
        rpc.contract.return_value = (ROUTER,)
        with self.assertRaisesRegex(rh.RpcError, "factory"):
            rh.verify_pool(rpc, {"pool": POOL}, "0x64")

    def test_getpool_registry_checked(self):
        rpc = Mock()
        rpc.contract.side_effect = [(rh.FACTORY,), (TOKEN,), (QUOTE,), (3000,), (ROUTER,)]
        with self.assertRaisesRegex(rh.RpcError, "identity"):
            rh.verify_pool(rpc, {"pool": POOL, "token": TOKEN, "quote": QUOTE}, "0x64")

    def test_log_failure_does_not_create_success(self):
        rpc = Mock()
        rpc.contract.side_effect = [(18,), (1000,)]
        rpc.call.side_effect = rh.RpcError("history unavailable")
        with patch.object(rh, "verify_pool", return_value=0):
            with self.assertRaisesRegex(rh.RpcError, "history"):
                rh.inspect_incremental(rpc, {"pool": POOL, "token": TOKEN}, 1, 100, rh.CONFIG, rh.Store(), Mock(), 0)

    def test_publication_does_not_roll_back_or_mix_network(self):
        old = {"chain_id":4663,"tokens":[],"attempted_at":"2026-09-06T00:00:00Z"}
        new = dict(old, attempted_at="2026-09-07T00:00:00Z")
        self.assertIs(choose_robinhood(old, new), new)
        self.assertIsNone(choose_robinhood(dict(old, chain_id=1), None))

    def test_same_block_balance_and_retention_bound(self):
        receipt = fixture()
        receipt.update(transactionHash="tx", blockHash="hash")
        swap = dict(receipt["logs"][0], blockNumber="0x64", blockHash="hash", transactionHash="tx", logIndex="0x1")
        rpc = Mock()
        rpc.contract.side_effect = [(6,), (1000,), (30,)]
        rpc.call.side_effect = [{"hash": "hash"}, receipt]
        with patch.object(rh, "verify_pool", return_value=0), patch.object(rh, "get_logs", side_effect=[[swap], []]), patch.object(rh, "security_check", return_value={"status":"unknown", "flags":[]}):
            result = rh.inspect_incremental(rpc, {"pool": POOL, "token": TOKEN, "key": rh.token_key(TOKEN)}, 1, 100, rh.CONFIG, rh.Store(), Mock(), 0)
        self.assertEqual(result["retained_supply_upper_bound_pct"], 3)
        self.assertEqual(result["wallets"][0]["retention_upper_bound_pct"], 30)
        self.assertEqual(result["swap_transactions"], 1)
        self.assertTrue(result["history_complete"])
        self.assertEqual(result["status"], "observed")
        self.assertEqual(rpc.contract.call_args.args[-1], "0x64")

    def test_malformed_swap_is_not_silent_no_buy(self):
        with self.assertRaisesRegex(rh.RpcError, "Invalid swap"):
            rh.swap_amounts({"data":"0x01"})


class LegacyAttributionTests(unittest.TestCase):
    def setUp(self):
        self.store = rh.Store()
        self.addCleanup(self.store.close)
        self.pool = {"pool": POOL, "token": TOKEN, "quote": QUOTE, "key": rh.token_key(TOKEN),
                     "eligible": True, "volume_h1_usd": 100}
        self.receipt = fixture()
        self.receipt.update(transactionHash="tx", blockHash="hash")
        self.swap = dict(self.receipt["logs"][0], transactionHash="tx", blockHash="hash", blockNumber="0x64", logIndex="0x1")
        self.event = {"recipient": WALLET, "bought_raw": "100", "transaction": "tx", "block": 100, "block_hash": "hash"}
        self.frozen = {"wallets": [{"address": WALLET, "bought_raw": "100", "balance_raw": "100",
                       "retained_lower_bound_raw": "100", "retained_upper_bound_raw": "100"}],
                       "checks": 2, "created_at": "2026-10-03T00:00:00Z", "checked_at": "2026-10-03T00:00:00Z",
                       "balance_block": 100, "last_check_timestamp": 1000, "reason": "Distributed net buy wave",
                       "initial_supply_raw": "1000", "initial_retained_raw": "100", "evidence_buys": [self.event],
                       "evidence_from_block": 100, "evidence_to_block": 100}
        self.store.put("cohort:" + self.pool["key"], self.frozen)
        self.store.put("receipt:tx", self.receipt)
        self.store.put("block-time:hash", 1000)
        self.store.add_logs(POOL, [self.swap])
        self.store.finish()

    def inspect(self, start=100, head=100, config=None, pool=None):
        rpc = Mock()
        rpc.call.side_effect = lambda method, args: {"hash": "hash" if int(args[0], 16) == 100 else "hash200"}
        rpc.contract.side_effect = lambda target, method, *args, **kwargs: (
            (6,) if method == "decimals()" else (1000,) if method == "totalSupply()" else (100,))
        with patch.object(rh, "verify_pool", return_value=0), patch.object(rh, "get_logs", return_value=[]), patch.object(
                rh, "security_check", return_value={"status": "no_flags", "flags": []}), patch.object(
                rh, "position_check", return_value={"status": "pending"}) as position, patch.object(
                rh, "preparation_check", return_value={"status": "not_checked"}) as preparation:
            row = rh.inspect_incremental(rpc, pool or dict(self.pool), start, head, config or rh.CONFIG,
                                         self.store, Mock(), 3000)
        self.last_position, self.last_preparation, self.last_rpc = position, preparation, rpc
        return row

    def previous_row(self, routed=False):
        row = dict(self.pool, status="retained", wallets=copy.deepcopy(self.frozen["wallets"]),
                   cohort_created_at=self.frozen["created_at"], cohort_checks=2,
                   cohort_reason="Relay buy wave" if routed else self.frozen["reason"],
                   attribution_complete=True, retained_supply_lower_bound_pct=10,
                   retained_supply_upper_bound_pct=10, history_complete=True,
                   position_flow={"status": "checked", "amounts_raw": {"original": "100"}})
        if routed:
            row["relay"] = {"signal_wave": {"buy_transactions": 5}}
        return row

    def test_legacy_cohort_outside_window_cannot_be_confirmed_by_an_empty_window(self):
        row = self.inspect(start=101, head=200)
        self.assertEqual(row["status"], "needs_data")
        self.assertFalse(row["attribution_complete"])
        self.assertEqual(row["cohort_attribution_status"], "legacy_unverified")
        self.assertIsNone(row["retained_supply_lower_bound_pct"])
        self.assertIsNone(row["retained_supply_upper_bound_pct"])
        self.assertEqual(row["wallets"][0]["address"], WALLET)
        self.assertEqual(row["wallets"][0]["bought_raw"], "100")
        self.assertIsNone(row["wallets"][0]["retained_lower_bound_raw"])
        self.assertEqual(row["legacy_ordinary_attribution"]["wallets"][0]["retained_lower_bound_raw"], "100")
        self.assertEqual(row["legacy_ordinary_attribution"]["evidence_buys"], [self.event])
        self.assertEqual(self.store.get("cohort:" + self.pool["key"])["wallets"][0]["retained_lower_bound_raw"], "100")
        self.last_position.assert_not_called()
        self.last_preparation.assert_not_called()

    def test_cached_mixed_source_receipt_is_reprocessed_but_does_not_replace_legacy_buyers(self):
        bad = copy.deepcopy(self.receipt)
        bad["logs"][1:] = [transfer(POOL, "0x" + "6" * 40), transfer("0x" + "7" * 40, WALLET)]
        self.store.put("receipt:tx", bad)
        row = self.inspect()
        self.assertEqual(row["receipts_checked"], 1)
        self.assertEqual(row["attributed_buy_transactions"], 0)
        self.assertEqual(row["status"], "needs_data")
        self.assertEqual(self.store.get("receipt:tx"), bad)
        self.assertEqual(self.store.get("cohort:" + self.pool["key"])["evidence_buys"], [self.event])
        self.assertEqual(row["wallets"][0]["address"], WALLET)
        self.assertNotIn("ordinary_attribution_version", self.store.get("cohort:" + self.pool["key"]))
        self.assertFalse(any(call.args[0] == "eth_getTransactionReceipt" for call in self.last_rpc.call_args_list))

    def test_exact_original_receipt_revalidation_upgrades_legacy_cohort(self):
        previous = rh.guard_legacy_ordinary_row(self.previous_row())
        row = self.inspect(pool=previous)
        self.assertEqual(row["status"], "retained")
        self.assertTrue(row["attribution_complete"])
        self.assertEqual(row["cohort_attribution_status"], "verified")
        self.assertEqual(row["ordinary_attribution_version"], rh.ORDINARY_ATTRIBUTION_VERSION)
        self.assertNotIn(rh.LEGACY_ATTRIBUTION_WARNING, row.get("error") or "")
        self.assertEqual(row["retained_supply_lower_bound_pct"], 10)
        self.assertEqual(self.store.get("cohort:" + self.pool["key"])["ordinary_attribution_version"], rh.ORDINARY_ATTRIBUTION_VERSION)
        self.last_position.assert_called_once()

    def test_new_ordinary_cohort_is_stamped_with_current_provenance(self):
        self.store.put("cohort:" + self.pool["key"], None)
        receipt = dict(self.receipt, transactionHash="tx2")
        self.store.put("receipt:tx2", receipt)
        self.store.add_logs(POOL, [dict(self.swap, transactionHash="tx2", logIndex="0x2")])
        row = self.inspect(config=dict(rh.CONFIG, min_cohort_wallets=1, ordinary_wave_min_age_hours=0))
        self.assertEqual(row["status"], "buy_wave")
        self.assertEqual(row["ordinary_attribution_version"], rh.ORDINARY_ATTRIBUTION_VERSION)
        self.assertEqual(row["cohort_attribution_status"], "verified")
        self.assertNotIn("legacy_ordinary_attribution", row)

    def test_partial_receipt_window_cannot_upgrade_legacy_provenance(self):
        row = self.inspect(config=dict(rh.CONFIG, max_receipts_per_pool=0))
        self.assertEqual(row["receipts_checked"], 0)
        self.assertEqual(row["status"], "needs_data")
        self.assertNotIn("ordinary_attribution_version", self.store.get("cohort:" + self.pool["key"]))

    def test_missing_original_evidence_or_mismatched_totals_cannot_upgrade(self):
        for mutation in (lambda frozen: frozen.update(evidence_buys=[]),
                         lambda frozen: frozen["wallets"][0].update(bought_raw="200")):
            with self.subTest(mutation=mutation):
                frozen = copy.deepcopy(self.frozen)
                mutation(frozen)
                self.store.put("cohort:" + self.pool["key"], frozen)
                row = self.inspect()
                self.assertEqual(row["status"], "needs_data")
                self.assertEqual(row["cohort_attribution_status"], "legacy_unverified")

    def test_validated_legacy_relay_cohort_is_not_quarantined(self):
        for reason in ("Relay buy wave", "Cross-chain buy wave"):
            with self.subTest(reason=reason):
                frozen = dict(self.frozen, reason=reason, relay_wave={"buy_transactions": 5})
                self.store.put("cohort:" + self.pool["key"], frozen)
                row = self.inspect(start=101, head=200)
                self.assertEqual(row["status"], "retained")
                self.assertEqual(row["relay"]["signal_wave"], frozen["relay_wave"])
                self.assertEqual(row["retained_supply_lower_bound_pct"], 10)
                self.assertNotIn("legacy_ordinary_attribution", row)
                self.last_position.assert_called_once()

    def test_repeated_publication_guard_preserves_raw_evidence(self):
        row = rh.guard_legacy_ordinary_row(self.previous_row())
        historical = copy.deepcopy(row["legacy_ordinary_attribution"])
        first = copy.deepcopy(row)
        self.assertEqual(rh.guard_legacy_ordinary_row(row), first)
        self.assertEqual(row["legacy_ordinary_attribution"], historical)
        self.assertEqual(row["legacy_ordinary_attribution"]["position_flow"]["status"], "checked")
        self.assertEqual(row["position_flow"]["status"], "pending")

    def test_unavailable_scan_guards_only_legacy_ordinary_snapshot_rows(self):
        rows = [self.previous_row(), self.previous_row(routed=True)]
        previous = {"chain_id": rh.CHAIN_ID, "generated_at": "2026-10-03T00:00:00Z", "tokens": rows}
        original = copy.deepcopy(previous)
        rpc = Mock(provider="test", calls=1)
        rpc.call.return_value = "0x1"
        output = rh.scan(previous, rpc=rpc, store=self.store)
        self.assertEqual(output["status"], "unavailable")
        self.assertEqual(output["generated_at"], previous["generated_at"])
        self.assertEqual(output["tokens"][0]["status"], "needs_data")
        self.assertEqual(output["tokens"][1], original["tokens"][1])
        self.assertEqual(previous, original)

    def test_queued_and_failed_scan_publications_have_unknown_legacy_bounds(self):
        for budget, expected in ((100, "queued"), (1000, "check_failed")):
            with self.subTest(status=expected):
                previous = {"chain_id": rh.CHAIN_ID, "tokens": [self.previous_row()]}
                rpc = Mock(provider="test", calls=0, budget=budget, deadline=time.monotonic() + 10000)
                rpc.call.side_effect = lambda method, *args: hex(rh.CHAIN_ID) if method == "eth_chainId" else "0xc8" if method == "eth_blockNumber" else {"hash": "head"}
                block = {"hash": "head", "timestamp": hex(int(time.time()))}
                with patch.object(rh, "window_start", return_value=(100, block)), patch.object(
                        rh, "discover", return_value=([dict(self.pool)], [])), patch.object(
                        rh, "stock_registry", return_value=set()), patch.object(
                        rh, "token_age", return_value={"age_status": "eligible"}), patch.object(
                        rh, "inspect_incremental", side_effect=rh.RpcError("test provider failure")), patch.object(
                        rh, "enrich_gmgn", return_value={"status": "not_checked"}):
                    output = rh.scan(previous, rpc=rpc, store=self.store, session=Mock())
                row = output["tokens"][0]
                self.assertEqual(row["status"], expected)
                self.assertFalse(row["attribution_complete"])
                self.assertIsNone(row["retained_supply_lower_bound_pct"])
                self.assertEqual(row["legacy_ordinary_attribution"]["wallets"], self.frozen["wallets"])
                self.assertEqual(self.store.get("snapshot")["tokens"][0]["cohort_attribution_status"], "legacy_unverified")


if __name__ == "__main__":
    unittest.main()
