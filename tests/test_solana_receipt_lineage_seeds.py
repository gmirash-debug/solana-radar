"""Exact receipt components do not manufacture a historical cohort position."""

import copy
import json
import unittest

from position_evidence import (freeze_receipt_seeds as freeze, resolve_solana_position_history as resolve,
                               solana_position_summary as summarize)
from tests.test_solana_position_lineage import A, B, C, MINT, PROGRAM, SERVICE, move, sale, transaction


def buy(amount=100, *, owner=A, account="a", before=50, slot=11, signature=None):
    tx = move(amount, source="vault", destination=account, owner=SERVICE,
              recipient=owner, before=1000 + amount, recipient_before=before,
              slot=slot, signature=signature)
    swap = {"signature": tx["transaction"]["signatures"][0], "slot": slot,
            "kind": "buy", "pool_address": SERVICE, "token_address": MINT,
            "token_recipient": owner, "owner_resolution": "token_recipient",
            "token_amount": 999.123, "token_recipient_amount": 999.123}
    return tx, swap


def capture(tx, swap, owners=None, **kwargs):
    return freeze(MINT, [tx], [swap], owners if owners is not None else [A], **kwargs)


class ReceiptSeedTests(unittest.TestCase):
    def assert_unavailable(self, out, owner=A):
        self.assertEqual(out["owners"][owner]["status"], "unavailable")
        self.assertEqual(out["owners"][owner]["seeds"], [])
        self.assertIsNone(out["owners"][owner]["bought_raw"])
        self.assertFalse(out["original_cohort_denominator_complete"])
        self.assertFalse(out["confirmation_eligible"])

    def test_raw_positive_receipt_delta_not_post_balance_or_parsed_float_is_seeded(self):
        tx, swap = buy()
        out = capture(tx, swap)
        entry = out["owners"][A]
        self.assertEqual(entry["status"], "frozen_receipt_component")
        self.assertEqual(entry["seed_slot"], 11)
        self.assertEqual(entry["signature"], "tx-11")
        self.assertEqual(entry["bought_raw"], "100")
        self.assertEqual(entry["scope"], "receipt_component_not_entire_cohort")
        self.assertEqual(entry["capture_key"], f"{MINT}:{A}:tx-11")
        seed = entry["seeds"][0]
        self.assertEqual(seed["account"], "a")
        self.assertEqual(seed["balance_raw"], "150")
        self.assertEqual(seed["bought_raw"], "100")
        self.assertEqual(seed["retained_lower_raw"], "100")
        self.assertEqual(seed["retained_upper_raw"], "100")
        self.assertEqual(seed["decimals"], 0)
        self.assertTrue(entry["requires_seed_slot_tail_check"])
        self.assertEqual(entry["buy_classification"], "caller_parsed_not_protocol_verified")
        self.assertFalse(out["affects_original_cohort_retention"])
        self.assertEqual(out["ownership"], "not_established")
        json.dumps(out, allow_nan=False)

    def test_large_raw_amounts_and_decimals_do_not_pass_through_floats(self):
        amount = 2**60 + 123
        tx, swap = buy(amount)
        for field in ("preTokenBalances", "postTokenBalances"):
            for row in tx["meta"][field]:
                row["uiTokenAmount"]["decimals"] = 9
                row["uiTokenAmount"]["uiAmount"] = float("nan")
        for floating in (None, 0.0, -1.2, float("nan"), float("inf"), "bad"):
            swap.update(token_amount=floating, token_recipient_amount=floating)
            out = capture(tx, swap)
            self.assertEqual(out["owners"][A]["bought_raw"], str(amount))
            self.assertEqual(out["owners"][A]["seeds"][0]["decimals"], 9)
            json.dumps(out, allow_nan=False)

    def test_one_latest_successful_receipt_not_cumulative_buys(self):
        old, old_swap = buy(100, slot=11, signature="z")
        latest, latest_swap = buy(20, slot=12, signature="a")
        for txs, swaps in (([old, latest], [latest_swap, old_swap]), ([latest, old], [old_swap, latest_swap])):
            out = freeze(MINT, txs, swaps, [A])
            self.assertEqual(out["owners"][A]["signature"], "a")
            self.assertEqual(out["owners"][A]["bought_raw"], "20")
            self.assertEqual(out["owners"][A]["seed_slot"], 12)

    def test_failed_latest_buy_is_skipped_but_missing_success_metadata_is_not(self):
        old, old_swap = buy(slot=11)
        latest, latest_swap = buy(slot=12)
        latest["meta"]["err"] = {"InstructionError": [0, "failed"]}
        out = freeze(MINT, [latest, old], [old_swap, latest_swap], [A])
        self.assertEqual(out["owners"][A]["signature"], "tx-11")
        latest["meta"].pop("err")
        out = freeze(MINT, [latest, old], [old_swap, latest_swap], [A])
        self.assert_unavailable(out)
        self.assertIn("missing_receipt_success_metadata", out["issues"])

    def test_unsafe_or_missing_latest_receipt_does_not_fall_back_to_older_buy(self):
        old, old_swap = buy(slot=11)
        latest, latest_swap = buy(slot=12)
        latest["meta"]["preTokenBalances"].pop()
        out = freeze(MINT, [latest, old], [old_swap, latest_swap], [A])
        self.assert_unavailable(out)
        self.assertEqual(out["owners"][A]["signature"], "tx-12")
        out = freeze(MINT, [old], [old_swap, latest_swap], [A])
        self.assert_unavailable(out)
        self.assertIn("missing_buy_receipt", out["issues"])
        out = freeze(MINT, [old, latest], [old_swap, dict(latest_swap, signature=None)], [A])
        self.assert_unavailable(out)
        self.assertIn("missing_buy_receipt_signature", out["issues"])

    def test_duplicate_receipts_and_duplicate_or_conflicting_swap_rows_are_refused(self):
        tx, swap = buy()
        for txs, swaps, issue in (([tx, tx], [swap], "duplicate_receipt_signature"),
                                  ([tx], [swap, swap], "duplicate_or_conflicting_pool_swap_rows"),
                                  ([tx], [swap, dict(swap, kind="sell")], "duplicate_or_conflicting_pool_swap_rows")):
            with self.subTest(issue=issue):
                out = freeze(MINT, txs, swaps, [A])
                self.assert_unavailable(out)
                self.assertIn(issue, out["issues"])

    def test_latest_slot_ties_are_not_ordered_by_signature_or_timestamp(self):
        one, one_swap = buy(signature="z")
        two, two_swap = buy(signature="a")
        two["blockTime"] = 102
        out = freeze(MINT, [two, one], [two_swap, one_swap], [A])
        self.assert_unavailable(out)
        self.assertIn("ambiguous_latest_buy_slot", out["issues"])

    def test_other_successful_seed_account_activity_in_same_slot_is_ambiguous(self):
        tx, swap = buy()
        other = move(10, source="a", before=150, slot=11, signature="other")
        out = freeze(MINT, [tx, other], [swap], [A])
        self.assert_unavailable(out)
        self.assertIn("ambiguous_seed_slot_activity", out["issues"])
        other["slot"] = "11"
        self.assert_unavailable(freeze(MINT, [tx, other], [swap], [A]))
        other["meta"]["err"] = "failed"
        out = freeze(MINT, [tx, other], [swap], [A])
        self.assertEqual(out["owners"][A]["bought_raw"], "100")

    def test_multisignature_receipt_is_not_mistaken_for_another_same_slot_transaction(self):
        tx, swap = buy()
        tx["transaction"]["signatures"].append("second-signature")
        out = capture(tx, swap)
        self.assertEqual(out["owners"][A]["bought_raw"], "100")

    def test_split_into_same_owner_accounts_remains_one_disjoint_receipt_component(self):
        tx = transaction({"vault": (SERVICE, MINT, 1000, 900), "a": (A, MINT, 30, 90),
                          "b": (A, MINT, 40, 80)}, [("vault", "a", 60), ("vault", "b", 40)])
        _, swap = buy()
        out = capture(tx, swap)
        entry = out["owners"][A]
        self.assertEqual(entry["bought_raw"], "100")
        self.assertEqual([(s["account"], s["bought_raw"], s["balance_raw"]) for s in entry["seeds"]],
                         [("a", "60", "90"), ("b", "40", "80")])
        self.assertEqual(sum(int(s["retained_lower_raw"]) for s in entry["seeds"]), 100)

    def test_internal_account_reshuffle_cannot_manufacture_positive_purchase_amount(self):
        tx = transaction({"vault": (SERVICE, MINT, 1000, 900), "a": (A, MINT, 100, 70),
                          "b": (A, MINT, 0, 130)}, [("vault", "a", 100), ("a", "b", 130)])
        _, swap = buy()
        out = capture(tx, swap)
        self.assert_unavailable(out)
        self.assertIn("non_positive_or_mixed_recipient_delta", out["issues"])

    def test_multiple_recipients_are_not_resolved_by_largest_recipient(self):
        tx = transaction({"vault": (SERVICE, MINT, 1000, 900), "a": (A, MINT, 0, 60),
                          "b": (B, MINT, 0, 40)}, [("vault", "a", 60), ("vault", "b", 40)])
        _, swap = buy()
        out = capture(tx, swap, [A, B])
        self.assert_unavailable(out, A)
        self.assert_unavailable(out, B)
        self.assertIn("ambiguous_receipt_participants", out["issues"])

    def test_mixed_pool_and_gift_sources_are_not_a_pool_purchase_component(self):
        tx = transaction({"vault": (SERVICE, MINT, 1000, 900), "a": (A, MINT, 0, 150),
                          "c": (C, MINT, 50, 0)}, [("vault", "a", 100), ("c", "a", 50)])
        _, swap = buy()
        out = capture(tx, swap)
        self.assert_unavailable(out)
        self.assertIn("ambiguous_receipt_participants", out["issues"])
        tx, swap = buy()
        swap["pool_address"] = "different-pool"
        out = capture(tx, swap)
        self.assert_unavailable(out)

    def test_missing_raw_decimals_identity_lifecycle_and_duplicate_accounts_are_refused(self):
        for field, value in (("amount", None), ("amount", 100.0), ("amount", True), ("amount", "1e2"),
                             ("decimals", None), ("decimals", True), ("decimals", 1.0)):
            with self.subTest(field=field, value=value):
                tx, swap = buy()
                tx["meta"]["postTokenBalances"][1]["uiTokenAmount"][field] = value
                self.assert_unavailable(capture(tx, swap))
        for change in ("created", "closed", "duplicate", "owner_changed", "mint_changed", "decimals_changed"):
            with self.subTest(change=change):
                tx, swap = buy()
                if change == "created":
                    tx["meta"]["preTokenBalances"].pop()
                elif change == "closed":
                    tx["meta"]["postTokenBalances"].pop()
                elif change == "duplicate":
                    tx["meta"]["preTokenBalances"].append(copy.deepcopy(tx["meta"]["preTokenBalances"][1]))
                elif change == "owner_changed":
                    tx["meta"]["postTokenBalances"][1]["owner"] = B
                elif change == "mint_changed":
                    tx["meta"]["postTokenBalances"][1]["mint"] = MINT.lower()
                else:
                    tx["meta"]["postTokenBalances"][1]["uiTokenAmount"]["decimals"] = 9
                self.assert_unavailable(capture(tx, swap))

    def test_exact_mint_recipient_success_and_slot_are_required_not_signer_fallback(self):
        tx, swap = buy()
        for changes in ({"token_address": MINT.lower()}, {"token_recipient": B},
                        {"token_recipient": None, "signer": A}, {"kind": "sell"},
                        {"slot": 12}, {"slot": True}, {"owner_resolution": "unresolved"}):
            self.assert_unavailable(capture(tx, dict(swap, **changes)))
        for slot in (None, True, 11.0, -1):
            bad = dict(tx, slot=slot)
            self.assert_unavailable(capture(bad, swap))
        self.assert_unavailable(capture(tx, swap, [A.lower()]), A.lower())

    def test_zero_or_negative_recipient_delta_is_not_seeded(self):
        tx, swap = buy(amount=0)
        self.assert_unavailable(capture(tx, swap))
        tx = move(40, source="a", destination="vault", owner=A, recipient=SERVICE)
        self.assert_unavailable(capture(tx, swap))

    def test_known_public_services_and_vault_accounts_cannot_seed_wallet_components(self):
        for service in ("public-CEX", "public-router", "public-Relay"):
            tx, swap = buy(owner=service)
            out = capture(tx, swap, [service], service_owners=[service])
            self.assert_unavailable(out, service)
            self.assertIn("public_service_seed_owner", out["issues"])
        tx, swap = buy(owner=SERVICE)
        self.assert_unavailable(capture(tx, swap, [SERVICE]), SERVICE)
        tx, swap = buy()
        out = capture(tx, swap, service_accounts=["a"])
        self.assert_unavailable(out)
        self.assertIn("public_service_seed_account", out["issues"])

    def test_owners_are_bounded_and_observation_limits_reject_the_whole_set(self):
        tx, swap = buy()
        owners = [A] + [f"owner-{i}" for i in range(40)]
        out = capture(tx, swap, owners)
        self.assertEqual(out["supplied_owner_count"], 41)
        self.assertEqual(out["selected_owner_count"], 40)
        self.assertNotIn(owners[-1], out["owners"])
        self.assertIn("owner_limit", out["issues"])
        self.assertEqual(out["owners"][A]["bought_raw"], "100")
        for options in ({"max_transactions": 0}, {"max_swaps": 0}, {"max_accounts": 1}, {"max_instructions": 1}):
            self.assert_unavailable(capture(tx, swap, **options))
        out = capture(tx, swap, max_owners=0)
        self.assertEqual(out["owners"], {})
        self.assertEqual(out["status"], "unavailable")

    def test_input_objects_are_not_mutated_and_invalid_inputs_raise(self):
        tx, swap = buy()
        owners = [A]
        before = copy.deepcopy((tx, swap, owners))
        capture(tx, swap, owners)
        self.assertEqual((tx, swap, owners), before)
        for owners, options in (([A, A], {}), ([" owner"], {}), ([A], {"max_owners": True}),
                                ([A], {"max_transactions": -1})):
            with self.assertRaises(ValueError):
                capture(tx, swap, owners, **options)
        with self.assertRaises(ValueError):
            freeze(MINT, "not-transactions", [], [A])

    def test_per_owner_components_keep_their_own_seed_slot_not_a_shared_cohort_slot(self):
        one, one_swap = buy(owner=A, slot=11)
        two, two_swap = buy(owner=B, account="b", slot=12)
        out = freeze(MINT, [two, one], [two_swap, one_swap], [A, B])
        self.assertEqual(out["owners"][A]["seed_slot"], 11)
        self.assertEqual(out["owners"][B]["seed_slot"], 12)
        self.assertFalse(out["original_cohort_denominator_complete"])

    def test_capture_once_parent_contract_does_not_replace_with_later_rebuy(self):
        original, original_swap = buy(amount=100)
        rebuy, rebuy_swap = buy(amount=500, slot=20)
        thesis = {}
        for tx, swap in ((original, original_swap), (rebuy, rebuy_swap)):
            if "receipt_seed_capture" not in thesis:
                thesis["receipt_seed_capture"] = capture(tx, swap)
        frozen = thesis["receipt_seed_capture"]["owners"][A]
        self.assertEqual(frozen["signature"], "tx-11")
        self.assertEqual(frozen["bought_raw"], "100")
        self.assertEqual(thesis["receipt_seed_capture"]["capture_policy"], "once_per_thesis_never_replace_with_rebuys")

    def test_unavailable_initial_capture_is_not_backfilled_from_later_rebuy(self):
        rebuy, rebuy_swap = buy(amount=500, slot=20)
        thesis = {}
        for txs, swaps in (([], []), ([rebuy], [rebuy_swap])):
            if "receipt_seed_capture" not in thesis:
                thesis["receipt_seed_capture"] = freeze(MINT, txs, swaps, [A])
        self.assert_unavailable(thesis["receipt_seed_capture"])


class ReceiptBoundaryIntegrationTests(unittest.TestCase):
    def seeds(self):
        tx, swap = buy()
        return capture(tx, swap)["owners"][A]["seeds"]

    def page(self, account, proof=None):
        coverage = {"provider": "fixture-rpc", "account": account, "mint": MINT,
                    "from_slot": 11, "to_slot": 30, "scope": "all_token_account_activity",
                    "commitment": "finalized", "complete": True}
        if proof is not None:
            coverage["seed_boundary"] = proof
        return {"transactions": [], "next_cursor": None, "coverage": coverage}

    def proof(self, account="a", *, bought=100, balance=150):
        return {"account": account, "slot": 11, "signature": "tx-11", "owner": A,
                "mint": MINT, "decimals": 0, "bought_raw": str(bought), "balance_raw": str(balance),
                "no_later_successful_activity": True}

    def test_complete_later_history_does_not_establish_unverified_seed_slot_tail(self):
        out = resolve(MINT, self.seeds(), lambda account, **_: self.page(account), from_slot=11, to_slot=30)
        self.assertEqual(out["amounts_raw"], {"original": "0", "transferred": "0", "sold": "0", "unknown": "100"})
        self.assertIn("receipt_seed_slot_boundary_unverified", out["issues"])
        self.assertFalse(out["coverage"]["complete"])
        self.assertEqual(out["scope"], "receipt_component_not_entire_cohort")
        self.assertFalse(out["original_cohort_denominator_complete"])
        self.assertFalse(out["confirmation_eligible"])

    def test_direct_ledger_cannot_silently_assume_receipt_is_end_of_slot(self):
        out = summarize(MINT, self.seeds(), [], from_slot=11, to_slot=30, history_complete=True)
        self.assertEqual(out["amounts_raw"]["unknown"], "100")
        self.assertIn("receipt_seed_slot_boundary_unverified", out["issues"])
        self.assertFalse(out["history_complete"])

    def test_boundary_flag_on_input_does_not_bypass_resolver_adapter_proof(self):
        seeds = [dict(seed, receipt_slot_boundary_verified=True) for seed in self.seeds()]
        out = resolve(MINT, seeds, lambda account, **_: self.page(account), from_slot=11, to_slot=30)
        self.assertEqual(out["amounts_raw"]["unknown"], "100")
        self.assertIn("receipt_seed_slot_boundary_unverified", out["issues"])

    def test_split_receipt_accounts_each_need_their_own_boundary_proof(self):
        tx = transaction({"vault": (SERVICE, MINT, 1000, 900), "a": (A, MINT, 30, 90),
                          "b": (A, MINT, 40, 80)}, [("vault", "a", 60), ("vault", "b", 40)])
        _, swap = buy()
        seeds = capture(tx, swap)["owners"][A]["seeds"]
        proofs = {"a": self.proof("a", bought=60, balance=90)}
        out = resolve(MINT, seeds, lambda account, **_: self.page(account, proofs.get(account)), from_slot=11, to_slot=30)
        self.assertEqual(out["amounts_raw"]["unknown"], "100")
        proofs["b"] = self.proof("b", bought=40, balance=80)
        out = resolve(MINT, seeds, lambda account, **_: self.page(account, proofs.get(account)), from_slot=11, to_slot=30)
        self.assertEqual(out["amounts_raw"]["original"], "100")
        self.assertEqual(out["bought_raw"], "100")

    def test_finalized_verified_receipt_tail_allows_exact_component_replay(self):
        proof = self.proof()
        out = resolve(MINT, self.seeds(), lambda account, **_: self.page(account, proof), from_slot=11, to_slot=30)
        self.assertEqual(out["amounts_raw"]["original"], "100")
        self.assertEqual(out["bought_raw"], "100")
        self.assertEqual(out["locations"][0]["current_balance_raw"], "150")
        self.assertTrue(out["coverage"]["accounts"][0]["receipt_boundary_verified"])
        self.assertTrue(out["history_complete"])
        self.assertFalse(out["affects_original_cohort_retention"])
        self.assertFalse(out["confirmation_eligible"])

    def test_mismatched_or_false_receipt_boundary_proof_is_not_coverage(self):
        proof = self.proof()
        for changes in ({"account": "b"}, {"slot": 12}, {"slot": True}, {"signature": "rebuy"},
                        {"no_later_successful_activity": False}, {"no_later_successful_activity": "true"},
                        {"owner": B}, {"mint": MINT.lower()}, {"decimals": 9}, {"decimals": True},
                        {"bought_raw": "101"}, {"balance_raw": "100"}, {"balance_raw": None}):
            bad = dict(proof, **changes)
            out = resolve(MINT, self.seeds(), lambda account, **_: self.page(account, bad), from_slot=11, to_slot=30)
            self.assertEqual(out["amounts_raw"]["unknown"], "100")
            self.assertIn("receipt_seed_slot_boundary_unverified", out["issues"])

    def test_verified_component_sale_and_rebuy_never_replace_component_inventory(self):
        sold, witness, _ = sale(amount=150, before=150, slot=12)
        rebuy, _ = buy(amount=500, before=0, slot=13)

        def fetch(account, **kwargs):
            page = self.page(account, self.proof())
            page["transactions"] = [rebuy, sold]
            return page

        out = resolve(MINT, self.seeds(), fetch, from_slot=11, to_slot=30,
                      service_owners=[SERVICE], swap_program_ids=[PROGRAM],
                      decode_transaction=lambda tx: {"swap_witnesses": [witness] if tx == sold else []})
        self.assertEqual(out["bought_raw"], "100")
        self.assertEqual(out["observed_verified_sale_raw"], "150")
        self.assertEqual(out["amounts_raw"], {"original": "0", "transferred": "0", "sold": "100", "unknown": "0"})
        self.assertEqual(out["original_position_bounds_raw"]["original"]["upper_raw"], "0")
        self.assertEqual(out["scope"], "receipt_component_not_entire_cohort")

    def test_wrong_component_seed_slot_raises_before_network_access(self):
        calls = []
        with self.assertRaises(ValueError):
            resolve(MINT, self.seeds(), lambda *args, **_: calls.append(args), from_slot=12, to_slot=30)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
