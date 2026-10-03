"""Bounded provider adapters are fixtures; no live RPC or ownership heuristics."""

import copy
import itertools
import unittest

from position_evidence import resolve_solana_position_history as resolve
from tests.test_solana_position_lineage import (
    A, B, C, MINT, PROGRAM, SERVICE, move, sale, seed, transaction,
)


def page(address, txs=(), *, complete=True, cursor=None, provider="fixture-rpc",
         closing=None, **coverage_changes):
    coverage = {"provider": provider, "account": address, "mint": MINT,
                "from_slot": 10, "to_slot": 30, "scope": "all_token_account_activity",
                "commitment": "finalized", "complete": complete}
    coverage.update(coverage_changes)
    result = {"transactions": list(txs), "next_cursor": cursor, "coverage": coverage}
    if closing is not None:
        result["closing_balance"] = closing
    return result


class History:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def history_page(self, account, **kwargs):
        self.calls.append((account, kwargs))
        result = self.pages.get((account, kwargs["cursor"]), page(account, complete=False))
        if isinstance(result, Exception):
            raise result
        return result


def lineage(history, seeds=None, **kwargs):
    return resolve(MINT, seeds if seeds is not None else [seed()], history,
                   from_slot=10, to_slot=30, **kwargs)


class BoundedHistoryTests(unittest.TestCase):
    def assert_conserved(self, out, total=100):
        self.assertEqual(int(out["bought_raw"]), total)
        self.assertEqual(sum(int(value) for value in out["amounts_raw"].values()), total)
        for kind, bound in out["original_position_bounds_raw"].items():
            self.assertEqual(bound["lower_raw"], out["amounts_raw"][kind])
            self.assertLessEqual(int(bound["lower_raw"]), int(bound["upper_raw"]))
            self.assertLessEqual(int(bound["upper_raw"]), total)
        self.assertEqual(out["ownership"], "not_established")
        self.assertFalse(out["affects_original_cohort_retention"])
        self.assertFalse(out["confirmation_eligible"])

    def test_empty_exhausted_history_requires_explicit_finalized_coverage(self):
        good = lineage(History({("a", None): page("a")}))
        self.assertEqual(good["status"], "checked")
        self.assertEqual(good["amounts_raw"]["original"], "100")
        self.assertTrue(good["coverage"]["complete"])
        self.assertEqual(good["coverage"]["providers"], ["fixture-rpc"])
        self.assertEqual(good["history_basis"], "provider_asserted_token_account_history")
        for changes in ({"complete": False}, {"complete": "true"}, {"provider": ""},
                        {"scope": "pool_swaps"}, {"account": "b"}, {"mint": MINT.lower()},
                        {"from_slot": 9}, {"to_slot": 31}, {"commitment": "confirmed"}):
            with self.subTest(changes=changes):
                out = lineage(History({("a", None): page("a", **changes)}))
                self.assertEqual(out["status"], "partial")
                self.assertEqual(out["amounts_raw"]["unknown"], "100")
                self.assertFalse(out["coverage"]["complete"])
                self.assert_conserved(out)

    def test_transfer_split_children_overlap_and_cycle_are_replayed_once(self):
        split = transaction({"a": (A, MINT, 100, 0), "b": (B, MINT, 0, 60), "c": (C, MINT, 0, 40)},
                            [("a", "b", 60), ("a", "c", 40)])
        back = move(20, source="b", destination="a", owner=B, recipient=A, before=60, slot=12)
        history = History({("a", None): page("a", [back, split, split]),
                           ("b", None): page("b", [split, back]),
                           ("c", None): page("c", [split])})
        out = lineage(history)
        self.assertEqual(out["amounts_raw"], {"original": "20", "transferred": "80", "sold": "0", "unknown": "0"})
        self.assertEqual(out["coverage"]["unique_transactions"], 2)
        self.assertEqual(out["coverage"]["transactions_received"], 6)
        self.assertEqual([account for account, _ in history.calls], ["a", "b", "c"])
        self.assertEqual(len(out["edges"]), 3)
        self.assertTrue(all(edge["common_control"] == "not_established" for edge in out["edges"]))
        self.assertTrue(all(edge["disposition"] == "transfer" for edge in out["edges"]))
        self.assert_conserved(out)

    def test_child_sale_and_new_buy_do_not_restore_original_position(self):
        ingress = move()
        sell, witness, _ = sale(source="b", owner=B, slot=12)
        buyback = move(100, source="vault", destination="a", owner=SERVICE,
                       recipient=A, before=1100, slot=13)
        history = History({("a", None): page("a", [buyback, ingress]),
                           ("b", None): page("b", [sell, ingress])})
        calls = []

        def decode(tx):
            calls.append(tx["transaction"]["signatures"][0])
            return {"swap_witnesses": [witness] if tx == sell else []}

        out = lineage(history, decode_transaction=decode, swap_program_ids=[PROGRAM], service_owners=[SERVICE])
        self.assertEqual(out["amounts_raw"], {"original": "0", "transferred": "0", "sold": "100", "unknown": "0"})
        self.assertEqual(out["original_position_bounds_raw"]["original"]["upper_raw"], "0")
        self.assertEqual(len(calls), 3)
        self.assertEqual([account for account, _ in history.calls], ["a", "b"])
        self.assertEqual(out["edges"][-1]["disposition"], "sale")
        self.assertEqual(out["edges"][-1]["follow_status"], "sale_terminal")
        self.assert_conserved(out)

    def test_later_uninterpretable_buyback_does_not_reverse_proved_sale(self):
        sell, witness, _ = sale()
        buyback = move(100, source="vault", destination="a", owner=SERVICE,
                       recipient=A, before=1100, slot=12)
        buyback["meta"]["innerInstructions"][0]["instructions"].append(
            {"program": "spl-token", "data": "unsupported"})
        history = History({("a", None): page("a", [buyback, sell])})
        out = lineage(history, decode_transaction=lambda tx: {
            "swap_witnesses": [witness] if tx == sell else []},
            swap_program_ids=[PROGRAM], service_owners=[SERVICE])
        self.assertEqual(out["status"], "partial")
        self.assertTrue(out["coverage"]["provider_history_complete"])
        self.assertFalse(out["coverage"]["complete"])
        self.assertEqual(out["amounts_raw"]["sold"], "100")
        self.assertEqual(out["original_position_bounds_raw"]["original"]["upper_raw"], "0")
        self.assertIn("unsupported_or_unresolved_token_instruction", out["coverage"]["transaction_issues"][0]["issues"])
        self.assert_conserved(out)

    def test_later_decoder_failure_is_a_gap_not_reversal_of_proved_sale(self):
        sell, witness, _ = sale()
        buyback = move(100, source="vault", destination="a", owner=SERVICE,
                       recipient=A, before=1100, slot=12)

        def decoder(tx):
            if tx == sell:
                return {"swap_witnesses": [witness]}
            raise ValueError("unreadable later transaction")

        out = lineage(History({("a", None): page("a", [sell, buyback])}),
                      decode_transaction=decoder, swap_program_ids=[PROGRAM], service_owners=[SERVICE])
        self.assertEqual(out["amounts_raw"]["sold"], "100")
        self.assertEqual(out["amounts_raw"]["original"], "0")
        self.assertIn("transaction_decode_error", out["issues"])
        self.assert_conserved(out)

    def test_unwitnessed_sell_label_is_not_sale(self):
        tx, _, _ = sale()
        history = History({("a", None): page("a", [tx])})
        out = lineage(history, decode_transaction=lambda _: {"kind": "sell"}, service_owners=[SERVICE])
        self.assertEqual(out["amounts_raw"]["sold"], "0")
        self.assertEqual(out["amounts_raw"]["unknown"], "100")
        self.assertEqual(out["edges"][0]["disposition"], "custody")
        self.assertEqual(out["edges"][0]["follow_status"], "public_service_terminal_beneficiary_unresolved")

    def test_cex_router_relay_are_terminal_services_not_owned_children(self):
        for name in ("public-CEX", "public-router", "public-Relay"):
            with self.subTest(service=name):
                tx = move(40, recipient=name)
                outflow = History({("a", None): page("a", [tx])})
                out = lineage(outflow, service_owners=[name])
                self.assertEqual(out["amounts_raw"], {"original": "60", "transferred": "0", "sold": "0", "unknown": "40"})
                self.assertEqual(len(outflow.calls), 1)
                self.assertEqual(out["edges"][0]["resolution"], "service_destination_unresolved")
                self.assertEqual(out["edges"][0]["disposition"], "custody")
                self.assertEqual(out["edges"][0]["common_control"], "not_established")
                self.assert_conserved(out)
        history = History({("a", None): page("a", [move(40)])})
        out = lineage(history, service_accounts=["b"])
        self.assertEqual(len(history.calls), 1)
        self.assertEqual(out["edges"][0]["disposition"], "custody")

    def test_new_funds_do_not_spawn_original_position_descendants(self):
        sell, witness, _ = sale()
        buy = move(100, source="vault", destination="a", owner=SERVICE,
                   recipient=A, before=1100, slot=12)
        moved = move(100, destination="new-child", before=100, slot=13)
        history = History({("a", None): page("a", [sell, buy, moved])})
        out = lineage(history, service_owners=[SERVICE], swap_program_ids=[PROGRAM],
                      decode_transaction=lambda tx: {"swap_witnesses": [witness] if tx == sell else []})
        self.assertEqual([account for account, _ in history.calls], ["a"])
        self.assertEqual(out["amounts_raw"]["sold"], "100")
        self.assertEqual(out["edges"][-1]["attributed_upper_raw"], "0")
        self.assert_conserved(out)

    def test_disjoint_seed_accounts_same_owner_are_not_counted_as_extra_purchases(self):
        tx = move(40, recipient=A, recipient_before=30)
        history = History({("a", None): page("a", [tx]), ("b", None): page("b", [tx])})
        out = lineage(history, [seed(), seed(account="b", owner=A, balance=30, bought=30, lower=30, upper=30)])
        self.assertEqual(out["amounts_raw"], {"original": "130", "transferred": "0", "sold": "0", "unknown": "0"})
        self.assertEqual(out["coverage"]["unique_transactions"], 1)
        self.assert_conserved(out, 130)

    def test_two_parents_merge_into_one_child_without_double_counting(self):
        one = move(40, before=60)
        two = move(30, source="c", before=40, recipient_before=40, slot=12)
        seeds = [seed(balance=60, bought=60, lower=60, upper=60),
                 seed(account="c", balance=40, bought=40, lower=40, upper=40)]
        history = History({("a", None): page("a", [one]), ("c", None): page("c", [two]),
                           ("b", None): page("b", [two, one])})
        out = lineage(history, seeds)
        self.assertEqual(out["amounts_raw"], {"original": "30", "transferred": "70", "sold": "0", "unknown": "0"})
        self.assertEqual([account for account, _ in history.calls].count("b"), 1)
        self.assert_conserved(out)

    def test_child_activity_before_ingress_is_not_original_position_activity(self):
        earlier = move(10, source="b", destination="c", owner=B, recipient=C, before=10, slot=11)
        ingress = move(slot=12)
        history = History({("a", None): page("a", [ingress]), ("b", None): page("b", [earlier, ingress])})
        out = lineage(history)
        self.assertEqual(out["amounts_raw"]["transferred"], "100")
        self.assertEqual([account for account, _ in history.calls], ["a", "b"])
        self.assertEqual(len(out["edges"]), 1)
        self.assert_conserved(out)

    def test_pagination_supplies_exact_scope_and_remaining_transaction_budget(self):
        tx = move(40)
        history = History({("a", None): page("a", complete=False, cursor="older"),
                           ("a", "older"): page("a", [tx]), ("b", None): page("b", [tx])})
        out = lineage(history.history_page, max_transactions=3, page_size=2)
        self.assertEqual(out["status"], "checked")
        self.assertEqual(len(history.calls), 3)
        self.assertEqual([kwargs["limit"] for _, kwargs in history.calls], [2, 2, 2])
        for _, kwargs in history.calls:
            self.assertEqual((kwargs["from_slot"], kwargs["to_slot"], kwargs["mint"]), (10, 30, MINT))
        self.assertEqual(out["coverage"]["pages_requested"], 3)
        self.assert_conserved(out)

    def test_transaction_address_depth_and_page_caps_fail_closed(self):
        tx = move()
        for limits, issue in (({"max_transactions": 1}, "transaction_limit"),
                              ({"max_addresses": 1}, "account_limit"),
                              ({"max_depth": 0}, "depth_limit"),
                              ({"max_pages": 1}, "page_limit"),
                              ({"max_events": 0}, "event_limit")):
            with self.subTest(limits=limits):
                history = History({("a", None): page("a", [tx]), ("b", None): page("b", [tx])})
                out = lineage(history, **limits)
                self.assertIn(issue, out["issues"])
                self.assertEqual(out["amounts_raw"]["sold"], "0")
                self.assertFalse(out["coverage"]["complete"])
                self.assertLessEqual(out["coverage"]["transactions_received"], out["limits"]["transactions"])
                self.assertLessEqual(out["coverage"]["addresses_discovered"], out["limits"]["addresses"])
                self.assertLessEqual(len(history.calls), out["limits"]["pages"])
                self.assert_conserved(out)

    def test_zero_work_limits_and_truncated_seeds_keep_full_denominator(self):
        for limits in ({"max_addresses": 0}, {"max_pages": 0}, {"max_transactions": 0}):
            history = History({})
            out = lineage(history, **limits)
            self.assertEqual(history.calls, [])
            self.assertEqual(out["amounts_raw"]["unknown"], "100")
            self.assert_conserved(out)
        history = History({("a", None): page("a")})
        out = lineage(history, [seed(), seed(account="b", owner=B)], max_addresses=1)
        self.assertEqual([account for account, _ in history.calls], ["a"])
        self.assert_conserved(out, 200)

    def test_page_overproduction_is_rejected_not_silently_truncated(self):
        history = History({("a", None): page("a", [move(40), move(40)])})
        out = lineage(history, page_size=1)
        self.assertIn("history_page_transaction_limit", out["issues"])
        self.assertEqual(out["coverage"]["unique_transactions"], 0)
        self.assertEqual(out["edges"], [])
        self.assert_conserved(out)

    def test_repeated_cursor_and_empty_infinite_pagination_are_bounded(self):
        history = History({("a", None): page("a", complete=False, cursor="repeat"),
                           ("a", "repeat"): page("a", complete=False, cursor="repeat")})
        out = lineage(history)
        self.assertEqual(len(history.calls), 2)
        self.assertIn("history_cursor_cycle", out["issues"])
        calls = []

        def endless(account, **kwargs):
            calls.append(account)
            return page(account, complete=False, cursor=str(len(calls)))

        out = lineage(endless, max_pages=3)
        self.assertEqual(len(calls), 3)
        self.assertIn("page_limit", out["issues"])

    def test_nonterminal_complete_claim_and_provider_change_poison_coverage(self):
        for first, last, issue in ((page("a", cursor="more"), page("a"), "premature_history_complete"),
                                  (page("a", complete=False, cursor="more"), page("a", provider="other"), "history_provider_changed")):
            out = lineage(History({("a", None): first, ("a", "more"): last}))
            self.assertIn(issue, out["issues"])
            self.assertFalse(out["history_complete"])
            self.assert_conserved(out)

    def test_reported_retention_floor_or_gaps_override_complete_claim(self):
        for changes, issue in (({"available_from_slot": 15}, "provider_history_pruned"),
                               ({"available_from_slot": True}, "invalid_provider_history_floor"),
                               ({"gaps": ["pruned_transaction"]}, "provider_reported_history_gaps"),
                               ({"from_slot": True}, "invalid_history_coverage_scope")):
            out = lineage(History({("a", None): page("a", **changes)}))
            self.assertFalse(out["coverage"]["provider_history_complete"])
            self.assertIn(issue, out["issues"])
            self.assert_conserved(out)
        out = lineage(History({("a", None): page("a", available_from_slot=11)}))
        self.assertTrue(out["coverage"]["complete"])
        self.assertEqual(out["coverage"]["accounts"][0]["available_from_slot"], 11)

    def test_closed_or_empty_account_with_incomplete_history_is_not_sold(self):
        closing = {"mint": MINT, "account": "a", "owner": A, "slot": 30, "balance_raw": "0"}
        for complete in (True, False):
            out = lineage(History({("a", None): page("a", complete=complete, closing=closing)}))
            self.assertEqual(out["amounts_raw"]["sold"], "0")
            self.assertEqual(out["amounts_raw"]["unknown"], "100")
            self.assertFalse(out["history_complete"])
            self.assertEqual(out["locations"][0]["current_balance_raw"], "0")
            self.assert_conserved(out)
        tx = move()
        tx["meta"]["postTokenBalances"].pop(0)
        tx["meta"]["innerInstructions"][0]["instructions"].append(
            {"program": "spl-token", "parsed": {"type": "closeAccount", "info": {"account": "a"}}})
        out = lineage(History({("a", None): page("a", [tx])}))
        self.assertEqual(out["amounts_raw"]["sold"], "0")
        self.assertIn("incomplete_transaction_data", out["issues"])
        self.assert_conserved(out)

    def test_missing_closing_balance_is_not_zero_and_stale_balance_is_rejected(self):
        out = lineage(History({("a", None): page("a", complete=False)}))
        self.assertIsNone(out["locations"][0]["current_balance_raw"])
        stale = {"mint": MINT, "account": "a", "owner": A, "slot": 29, "balance_raw": "0"}
        out = lineage(History({("a", None): page("a", closing=stale)}))
        self.assertIn("invalid_closing_balance_scope", out["issues"])
        self.assertEqual(out["amounts_raw"]["sold"], "0")

    def test_missing_body_fetch_failure_and_bad_page_never_confirm(self):
        for response in (page("a", [None]), {}, {"transactions": []},
                         RuntimeError("secret provider credential must not be exported")):
            with self.subTest(response=response):
                out = lineage(History({("a", None): response}))
                self.assertEqual(out["status"], "partial")
                self.assertEqual(out["amounts_raw"]["unknown"], "100")
                self.assertNotIn("secret provider", str(out))
                self.assert_conserved(out)

    def test_child_history_gap_degrades_whole_position_not_false_current_hold(self):
        tx = move(40)
        out = lineage(History({("a", None): page("a", [tx]), ("b", None): page("b", [tx], complete=False)}))
        self.assertFalse(out["history_complete"])
        self.assertEqual(out["amounts_raw"]["unknown"], "100")
        self.assertTrue(all(location["current_balance_raw"] is None for location in out["locations"]))
        self.assert_conserved(out)

    def test_conflicting_overlapping_transactions_are_not_replayed(self):
        history = History({("a", None): page("a", [move(40)]), ("b", None): page("b", [move(41)])})
        out = lineage(history)
        self.assertIn("conflicting_duplicate_transaction", out["issues"])
        self.assertEqual(out["coverage"]["conflicting_signatures"], ["tx-11"])
        self.assertEqual(out["amounts_raw"]["unknown"], "100")
        self.assertEqual(out["edges"], [])

    def test_same_slot_order_comes_from_decoder_not_signature_or_page_order(self):
        one = move(signature="z")
        two = move(source="b", destination="a", owner=B, recipient=A, signature="a")
        history = History({("a", None): page("a", [two, one]), ("b", None): page("b", [one, two])})
        out = lineage(history)
        self.assertIn("ambiguous_same_slot_order", out["issues"])
        self.assertEqual(out["amounts_raw"]["unknown"], "100")
        history = History({("a", None): page("a", [two, one]), ("b", None): page("b", [one, two])})
        out = lineage(history, decode_transaction=lambda tx: {
            "transaction_index": 0 if tx["transaction"]["signatures"][0] == "z" else 1})
        self.assertEqual(out["amounts_raw"]["original"], "100")
        self.assertEqual(out["coverage"]["unique_transactions"], 2)
        self.assert_conserved(out)

    def test_unrelated_account_and_outside_horizon_data_are_not_coverage(self):
        for tx in (move(source="x", destination="y"), move(slot=10), move(slot=31)):
            out = lineage(History({("a", None): page("a", [tx])}))
            self.assertFalse(out["history_complete"])
            self.assertEqual(out["amounts_raw"]["unknown"], "100")

    def test_decoder_failure_and_invalid_witnesses_fail_closed(self):
        def failure(_):
            raise RuntimeError("decoder secret")

        for decoder in (failure, lambda _: None, lambda _: {"swap_witnesses": "bad"},
                        lambda _: {"transaction_index": True}):
            out = lineage(History({("a", None): page("a", [move(40)])}), decode_transaction=decoder)
            self.assertEqual(out["status"], "partial")
            self.assertEqual(out["amounts_raw"]["sold"], "0")
            self.assertNotIn("decoder secret", str(out))
            self.assert_conserved(out)

    def test_invalid_caller_inputs_fail_before_provider_calls(self):
        for seeds, options in (([], {}), ([seed(), seed()], {}), ([dict(seed(), mint="other")], {}),
                               ([seed()], {"max_addresses": -1}), ([seed()], {"max_transactions": True}),
                               ([seed()], {"max_pages": 1.5}), ([seed()], {"page_size": 0}),
                               ([seed()], {"decode_transaction": "bad"}),
                               ([seed()], {"service_owners": [A]})):
            history = History({})
            with self.subTest(seeds=seeds, options=options), self.assertRaises(ValueError):
                lineage(history, seeds, **options)
            self.assertEqual(history.calls, [])

    def test_parser_limits_do_not_accept_a_transaction_prefix(self):
        for options in ({"max_instructions": 1}, {"max_transaction_accounts": 1}):
            history = History({("a", None): page("a", [move()])})
            out = lineage(history, **options)
            self.assertIn("incomplete_transaction_data", out["issues"])
            self.assertEqual(out["edges"], [])
            self.assert_conserved(out)

    def test_inputs_and_provider_transactions_are_not_mutated_even_by_decoder(self):
        tx = move(40)
        response = page("a", [tx])
        seeds = [seed()]
        before = copy.deepcopy((response, seeds))

        def decode(tx):
            tx["meta"]["err"] = "mutated"
            return {}

        lineage(History({("a", None): response, ("b", None): page("b", [tx])}), seeds,
                decode_transaction=decode)
        self.assertEqual((response, seeds), before)

    def test_generated_mixed_chains_and_repeated_pages_preserve_original_bounds(self):
        for extra, first, second in itertools.product((0, 50, 100), (10, 60, 100), (5, 10)):
            one = move(first, before=100 + extra)
            two = move(second, source="b", destination="c", owner=B, recipient=C,
                       before=first, slot=12)
            history = History({("a", None): page("a", [one, one]),
                               ("b", None): page("b", [two, one]), ("c", None): page("c", [two])})
            out = lineage(history, [seed(balance=100 + extra)])
            self.assert_conserved(out)
            self.assertEqual(out["status"], "checked")


if __name__ == "__main__":
    unittest.main()
