import copy
import unittest
from unittest.mock import Mock, patch

import scanner as s
import robinhood as r


NOW = s.parse_timestamp("2026-10-01T12:00:00Z")


def alert():
    owners = ["buyer-a", "buyer-b", "buyer-c"]
    events = [{"kind": "buy", "signature": "tx-" + owner, "signer": owner,
               "token_recipient": owner, "block_time": NOW - 60 + i * 20,
               "token_amount": 5, "sol_amount": 1} for i, owner in enumerate(owners)]
    return {"created_at": s.iso(NOW), "signal_family": "reactivation_wave", "action_tier": "candidate",
            "pool": {"pool_address": "pool", "token_address": "token"},
            "data_quality": {"status": "complete"}, "events": events,
            "wallet_graph": {"wallets": {owner: {"previous_tx_count_50": 2,
                "history_complete": True, "first_activity_at": NOW - 86400,
                "as_of_signature": "tx-" + owner, "as_of_time": NOW - 60 + i * 20,
                "funding_verified": True, "funding_source": "funder", "funding_sol": 1,
                "funding_at": NOW - 300} for i, owner in enumerate(owners)}},
            "wave": {"unique_buyers": 3, "supply": 1000, "balance_coverage_pct": 100,
                     "top_buyers": [{"owner": owner, "token_bought": 50, "current_balance": 50,
                                     "retained_from_wave": 50, "balance_verified": True} for owner in owners]}}


class CoordinationPipelineTests(unittest.TestCase):
    def test_attachment_uses_authoritative_window_amounts_without_promoting_tier(self):
        item = alert()
        s.attach_coordinated_activity(item, {})
        result = item["coordinated_activity"]
        self.assertEqual(result["metrics"]["held_supply_pct"], 15)
        self.assertEqual(result["metrics"]["observed_supply_pct"], 15)
        self.assertEqual(result["status"], "coordination_watch")
        self.assertEqual(item["action_tier"], "candidate")
        self.assertEqual(result["coverage"]["total_buyers"], 3)
        self.assertEqual(result["ownership"], "not_established")

    def test_routed_executor_profile_is_not_recipient_profile(self):
        item = alert()
        item["wallet_graph"] = {}
        item["events"][0].update(wallet="router", previous_tx_count_50=0, history_complete=True)
        inputs = s.coordination_inputs_from_alert(item)
        self.assertNotIn("buyer-a", inputs["profiles"])
        self.assertEqual(inputs["buys"][0]["owner"], "buyer-a")

    def test_full_window_sells_do_not_become_buyers(self):
        item = alert()
        item["coordination_events"] = item["events"] + [{"kind": "sell", "signature": "sell",
            "signer": "buyer-a", "token_sender": "buyer-a", "token_sender_amount": 20,
            "coordination_sale_owner": "buyer-a", "coordination_sale_amount": 20,
            "block_time": NOW - 1, "token_amount": 20}]
        inputs = s.coordination_inputs_from_alert(item)
        self.assertEqual(inputs["buys"][-1]["bought_tokens"], 0)
        self.assertEqual(inputs["buys"][-1]["sold_tokens"], 20)
        s.attach_coordinated_activity(item, {})
        self.assertEqual(item["coordinated_activity"]["metrics"]["buyer_count"], 3)

    def test_summary_and_public_thesis_strip_private_inputs(self):
        item = alert()
        item["coordination_events"] = copy.deepcopy(item["events"])
        s.attach_coordinated_activity(item, {})
        thesis = s.signal_thesis_from_alert(item, {})
        self.assertTrue(thesis["coordination_inputs"]["buys"])
        compact = s.compact_alert_for_dashboard(item)
        self.assertNotIn("coordination_events", compact)
        self.assertNotIn("members", compact["coordinated_activity"]["signals"][0])
        self.assertNotIn("coordination_inputs", s.compact_signal_thesis_for_dashboard(thesis))
        self.assertNotIn("coordination_inputs", s.public_signal_thesis(thesis))

    def test_recheck_keeps_original_group_and_abstains_on_failed_balances(self):
        item = alert()
        s.attach_coordinated_activity(item, {})
        thesis = s.signal_thesis_from_alert(item, {})
        original = [row["owner"] for row in thesis["cohort"]]
        checked = s.iso(NOW + 600)
        for row in thesis["cohort"]:
            row.update(current_balance=10, current_retained_tokens=10, checked_at=checked)
        s.update_thesis_coordination(thesis, {}, checked)
        self.assertEqual(thesis["coordinated_activity"]["metrics"]["held_supply_pct"], 3)
        thesis["cohort"][0]["checked_at"] = s.iso(NOW)
        s.update_thesis_coordination(thesis, {}, checked)
        self.assertIsNone(thesis["coordinated_activity"]["metrics"]["held_supply_pct"])
        self.assertEqual([row["owner"] for row in thesis["cohort"]], original)

    def test_flag_and_infrastructure_exclusion(self):
        item = alert()
        s.attach_coordinated_activity(item, {"coordinated_activity_enabled": False})
        self.assertNotIn("coordinated_activity", item)
        s.attach_coordinated_activity(item, {"coordinated_activity_infrastructure_addresses": ["funder"]})
        self.assertNotEqual(item["coordinated_activity"]["status"], "coordination_watch")
        self.assertFalse(any(signal["family"] == "funding" for signal in item["coordinated_activity"]["signals"]))

    def test_capped_history_cannot_be_called_young(self):
        rpc = Mock()
        rpc.signatures_for_address.return_value = [{"signature": "old-" + str(i), "blockTime": NOW - 100 - i} for i in range(50)]
        profile = s.classify_wallet(rpc, "buyer", "buy", NOW, {
            "freshish_max_previous_txs": 3, "dormant_gap_days": 30, "low_tx_max_previous_txs": 20}, {})
        self.assertFalse(profile["history_complete"])
        self.assertIsNone(profile["first_activity_at"])

    def test_inner_parsed_direct_transfer_vs_balance_heuristic(self):
        rpc = Mock()
        rpc.transaction.return_value = {"transaction": {"message": {"instructions": [], "accountKeys": ["funder", "buyer"]}},
            "meta": {"err": None, "preBalances": [3_000_000_000, 0], "postBalances": [1_000_000_000, 2_000_000_000],
                     "innerInstructions": [{"instructions": [{"program": "system", "parsed": {"type": "transfer",
                         "info": {"source": "funder", "destination": "buyer", "lamports": 2_000_000_000}}}]}]}}
        self.assertEqual(s.extract_wallet_funding(rpc, "tx", "buyer"), ("funder", 2, True))
        rpc.transaction.return_value["meta"]["innerInstructions"] = []
        self.assertEqual(s.extract_wallet_funding(rpc, "tx", "buyer"), ("funder", 2, False))

    def test_two_direct_sources_do_not_become_verified_common_funder(self):
        rpc = Mock()
        rpc.transaction.return_value = {"meta": {"err": None}, "transaction": {"message": {"instructions": [
            {"program": "system", "parsed": {"type": "transfer", "info": {"source": source, "destination": "buyer", "lamports": 1_000_000_000}}}
            for source in ("a", "b")]}}}
        self.assertFalse(s.extract_wallet_funding(rpc, "tx", "buyer")[2])

    def test_returned_funding_is_not_material_credit(self):
        rpc = Mock()
        rpc.transaction.return_value = {"meta": {"err": None}, "transaction": {"message": {"instructions": [
            {"program": "system", "parsed": {"type": "transfer", "info": {"source": source, "destination": destination, "lamports": 1_000_000_000}}}
            for source, destination in (("funder", "buyer"), ("buyer", "funder"))]}}}
        self.assertEqual(s.extract_wallet_funding(rpc, "tx", "buyer"), ("funder", 0, False))

    def test_cached_history_must_match_actual_first_buy_boundary(self):
        item = alert()
        for profile in item["wallet_graph"]["wallets"].values():
            profile.update(as_of_signature="old-buy", as_of_time=NOW - 10000)
        inputs = s.coordination_inputs_from_alert(item)
        self.assertFalse(any(profile["history_complete"] for profile in inputs["profiles"].values()))
        s.attach_coordinated_activity(item, {})
        self.assertFalse(any(signal["family"] == "age_activity" for signal in item["coordinated_activity"]["signals"]))

    def test_fee_payer_is_not_assigned_outside_owners_sale(self):
        item = alert()
        item["coordination_events"] = item["events"] + [{"kind": "sell", "signature": "sale", "signer": "buyer-a",
            "token_sender": "outside-owner", "token_sender_amount": 100, "token_amount": 100, "block_time": NOW - 1}]
        self.assertFalse(any(row["kind"] == "sell" for row in s.coordination_inputs_from_alert(item)["buys"]))

    def test_recheck_does_not_restore_sale_adjusted_original_position(self):
        item = alert()
        item["coordination_events"] = item["events"] + [{"kind": "sell", "signature": "sale-" + owner,
            "coordination_sale_owner": owner, "coordination_sale_amount": 50,
            "token_amount": 50, "block_time": NOW - 1} for owner in ("buyer-a", "buyer-b", "buyer-c")]
        s.attach_coordinated_activity(item, {})
        thesis = s.signal_thesis_from_alert(item, {})
        checked = s.iso(NOW + 600)
        for row in thesis["cohort"]:
            row.update(current_balance=50, current_retained_tokens=50, checked_at=checked)
        s.update_thesis_coordination(thesis, {}, checked)
        self.assertEqual(thesis["coordinated_activity"]["metrics"]["held_supply_pct"], 0)
        self.assertFalse(thesis["coordinated_activity"]["metrics"]["material_pattern"])

    def test_failed_initial_balance_does_not_freeze_a_zero_cap(self):
        item = alert()
        item["wave"]["balance_coverage_pct"] = 66
        item["wave"]["top_buyers"][0].update(balance_verified=False, current_balance=0, retained_from_wave=0)
        s.attach_coordinated_activity(item, {})
        thesis = s.signal_thesis_from_alert(item, {})
        self.assertNotIn("buyer-a", thesis["coordination_inputs"]["retention_caps"])
        checked = s.iso(NOW + 600)
        for row in thesis["cohort"]:
            row.update(current_balance=50, current_retained_tokens=50, checked_at=checked)
        s.update_thesis_coordination(thesis, {}, checked)
        self.assertEqual(thesis["coordinated_activity"]["metrics"]["held_supply_pct"], 15)

    def test_proven_post_window_exits_survive_failed_initial_balances(self):
        item = alert()
        for row in item["wave"]["top_buyers"]:
            row.update(balance_verified=False, current_balance=0, coordination_sold_tokens=50)
        s.attach_coordinated_activity(item, {})
        thesis = s.signal_thesis_from_alert(item, {})
        self.assertEqual(thesis["coordination_inputs"]["retention_caps"], {})
        self.assertEqual(set(thesis["coordination_inputs"]["proven_sales"].values()), {50})
        checked = s.iso(NOW + 600)
        for row in thesis["cohort"]:
            row.update(current_balance=50, current_retained_tokens=50, checked_at=checked)
        s.update_thesis_coordination(thesis, {}, checked)
        self.assertEqual(thesis["coordinated_activity"]["metrics"]["held_supply_pct"], 0)
        self.assertFalse(thesis["coordinated_activity"]["metrics"]["material_pattern"])

    def test_gift_sender_is_not_swap_seller(self):
        accounts = [("buyer-a", "token", "100", "0"), ("gift", "token", "0", "100"),
                    ("outside-owner", "token", "50", "0"), ("pool", "token", "1000", "1050"),
                    ("pool", s.SOL_MINT, "100", "99")]
        keys = [{"pubkey": "account-" + str(i), "signer": False} for i in range(5)]
        keys.insert(0, {"pubkey": "buyer-a", "signer": True})
        def balances(offset):
            return [{"accountIndex": i + 1, "owner": owner, "mint": mint,
                     "uiTokenAmount": {"amount": values[offset], "decimals": 0}}
                    for i, (owner, mint, *values) in enumerate(accounts)]
        def transfer(source, destination, amount):
            return {"program": "spl-token", "parsed": {"type": "transfer", "info": {
                "source": source, "destination": destination, "amount": amount}}}
        tx = {"blockTime": NOW - 1, "transaction": {"signatures": ["sale"], "message": {"accountKeys": keys}},
              "meta": {"err": None, "preTokenBalances": balances(0), "postTokenBalances": balances(1),
                       "innerInstructions": [{"instructions": [transfer("account-0", "account-1", "100"),
                                                                 transfer("account-2", "account-3", "50")]}]}}
        swap = s.parse_pool_swap(tx, s.Pool("pool", token_address="token"))
        self.assertEqual(swap["token_sender"], "buyer-a")
        self.assertEqual(swap["coordination_sale_owner"], "outside-owner")
        self.assertEqual(swap["coordination_sale_amount"], 50)
        item = alert()
        item["coordination_events"] = item["events"] + [swap]
        self.assertFalse(any(row["kind"] == "sell" for row in s.coordination_inputs_from_alert(item)["buys"]))
        tx["meta"]["innerInstructions"] = []
        swap = s.parse_pool_swap(tx, s.Pool("pool", token_address="token"))
        self.assertIsNone(swap["coordination_sale_owner"])

    def test_legacy_retention_from_unresolved_sales_does_not_cap_overlay(self):
        item = alert()
        for row in item["wave"]["top_buyers"]:
            row["retained_from_wave"] = 0
        s.attach_coordinated_activity(item, {})
        self.assertEqual(item["coordinated_activity"]["metrics"]["held_supply_pct"], 15)

    def test_unresolved_vault_inbound_leg_invalidates_seller_attribution(self):
        keys = [{"pubkey": value} for value in ("buyer-account", "vault")]
        meta = {"preTokenBalances": [
            {"accountIndex": i, "owner": owner, "mint": "token", "uiTokenAmount": {"decimals": 0}}
            for i, owner in enumerate(("buyer-a", "pool"))]}
        instructions = [{"program": "spl-token", "parsed": {"type": "transfer", "info": {
            "source": source, "destination": "vault", "amount": amount}}}
            for source, amount in (("buyer-account", "10"), ("ephemeral", "90"))]
        self.assertEqual(s.resolved_pool_token_sale(meta, keys, "pool", "token", instructions), (None, None))

    def test_proven_sales_include_ambiguous_same_second_conservatively(self):
        swaps = [{"kind": "sell", "signature": "sale-" + str(at), "block_time": at,
                  "coordination_sale_owner": "buyer", "coordination_sale_amount": 10}
                 for at in (NOW - 1, NOW, NOW + 1)]
        self.assertEqual(s.resolved_cohort_sales(swaps + swaps, "buyer", s.iso(NOW), s.iso(NOW)), 10)

    def test_post_window_sales_cap_overlay_through_actual_alert_builder(self):
        item = alert()
        window = item["events"]
        for event in window:
            event["token_amount"] = 50
        config = {"lane": "reactivation", "reactivation_wave_enabled": True,
            "reactivation_wave_min_buy_sol": 1, "reactivation_wave_min_net_buy_sol": 1,
            "reactivation_wave_min_unique_buyers": 3, "reactivation_wave_min_large_buyers": 3}
        metrics = s.reactivation_wave_window_metrics(window, config)
        candidate = {"window": window, "start": NOW - 60, "end": NOW - 20, "metrics": metrics}
        sales = [{"kind": "sell", "signature": "post-sale-" + owner, "block_time": NOW - 10,
                  "coordination_sale_owner": owner, "coordination_sale_amount": 50,
                  "token_amount": 50, "sol_amount": 1, "token_sender": "unresolved-router"}
                 for owner in ("buyer-a", "buyer-b", "buyer-c")]
        rpc = Mock()
        rpc.token_supply.return_value = 1000
        rpc.token_balance.return_value = 50
        with patch.object(s, "reactivation_wave_window_candidates", return_value=[candidate]), \
             patch.object(s, "analyze_wave_wallet_graph", return_value=item["wallet_graph"]), \
             patch.object(s, "utc_now", return_value=s.datetime.fromtimestamp(NOW, s.timezone.utc)), \
             patch.object(s, "classify_alert_tier", return_value=("candidate", [], [], {})):
            result = s.build_reactivation_wave_alerts(s.Pool("pool", token_address="token"), window + sales + sales, config, rpc)[0]
        self.assertEqual(len(result["coordination_events"]), 3)
        self.assertTrue(all(row["coordination_sold_tokens"] == 50 for row in result["wave"]["top_buyers"]))
        s.attach_coordinated_activity(result, {})
        self.assertEqual(result["coordinated_activity"]["metrics"]["held_supply_pct"], 0)
        self.assertFalse(result["coordinated_activity"]["metrics"]["material_pattern"])

    def test_churn_inventory_is_not_inferred_from_the_sale_cap(self):
        trades = [{"owner":"buyer", "transaction":"buy-" + str(i), "timestamp":NOW - 100 + i,
                   "bought_tokens":50} for i in range(2)]
        trades += [{"owner":"buyer", "transaction":"sell-" + str(i), "timestamp":NOW - 50 + i,
                    "kind":"sell", "sold_tokens":40} for i in range(2)]
        result = s.analyze_coordinated_activity(trades, positions=[{"owner":"buyer", "current_balance":100,
            "attributed_tokens":100, "retained_tokens":20, "balance_verified":True, "checked_at":NOW}],
            supply=1000, observed_at=NOW)
        self.assertFalse(any(signal["code"] == "inventory_churn" for signal in result["signals"]))

    def test_robinhood_uses_cache_only_and_preserves_status(self):
        events = [{"recipient": owner, "transaction": "tx-" + owner, "block_hash": "hash-" + owner, "bought_raw": "50"}
                  for owner in ("a", "b", "c")]
        row = {"status": "buy_wave", "checked_at": s.iso(NOW), "balance_block": 100, "total_supply_raw": "1000",
               "history_complete": True, "attribution_complete": True,
               "wallets": [{"address": owner, "bought_raw": "50", "balance_raw": "50", "retained_lower_bound_raw": "30"}
                           for owner in ("a", "b", "c")]}
        store = Mock()
        store.get.side_effect = lambda key: NOW - 60
        r.attach_coordination_evidence(row, events, store, {})
        self.assertEqual(row["coordinated_activity"]["metrics"]["held_supply_pct"], 9)
        self.assertFalse(row["coordinated_activity"]["metrics"]["material_pattern"])
        self.assertEqual(row["status"], "buy_wave")
        self.assertEqual(store.get.call_count, 3)
        store.get.return_value = None
        store.get.side_effect = None
        r.attach_coordination_evidence(row, events, store, {})
        self.assertEqual(row["coordinated_activity"]["status"], "not_checked")

    def test_transaction_version_is_requested_on_both_rpc_paths(self):
        provider = s.HeliusRpc("test", max_retries=0)
        provider.call = Mock(return_value={})
        provider.transaction("signature")
        self.assertEqual(provider.call.call_args.args[1][1]["maxSupportedTransactionVersion"], 1)
        router = s.RoutedSolanaRpc([provider])
        router.transaction("signature")
        self.assertEqual(provider.call.call_args.kwargs["params"][1]["maxSupportedTransactionVersion"], 1)

    def test_deterministic_invalid_version_does_not_repeat_on_another_provider(self):
        first, second = s.HeliusRpc("test", max_retries=0), s.AlchemyRpc("https://example.invalid", max_retries=0)
        first.call = Mock(side_effect=s.HeliusRpcError("getTransaction", "client", "Transaction version (2) is not supported"))
        second.call = Mock(return_value={})
        router = s.RoutedSolanaRpc([first, second], standard_order=["helius", "alchemy"])
        with self.assertRaises(s.HeliusRpcError):
            router.transaction("signature")
        second.call.assert_not_called()
        self.assertEqual(first.error_category(code=-32015, detail="Transaction version (2) is not supported"), "client")


if __name__ == "__main__":
    unittest.main()
