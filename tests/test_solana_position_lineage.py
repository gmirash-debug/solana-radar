import copy
import itertools
import unittest

from position_evidence import (
    annotate_solana_pool_position_activity as annotate,
    parse_solana_position_transaction as parse,
    solana_position_summary as summarize,
)


MINT, QUOTE, PROGRAM = "exact-Mint", "quote-Mint", "decoded-dex-program"
A, B, C, SERVICE = "owner-A", "owner-B", "owner-C", "public-service"


def transaction(rows, transfers=(), *, slot=11, signature=None, timestamp=101):
    """rows: account -> (owner, mint, before_raw, after_raw)."""
    keys = list(rows)
    def balances(side):
        return [{"accountIndex": i, "owner": owner, "mint": mint,
                 "uiTokenAmount": {"amount": str(before if side == 0 else after), "decimals": 0}}
                for i, (_, (owner, mint, before, after)) in enumerate(rows.items())]
    instructions = [{"program": "spl-token", "parsed": {"type": "transfer", "info": {
        "source": source, "destination": destination, "amount": str(amount)}}}
        for source, destination, amount in transfers]
    return {"slot": slot, "blockTime": timestamp,
            "transaction": {"signatures": [signature or "tx-" + str(slot)], "message": {
                "accountKeys": [{"pubkey": key} for key in keys], "instructions": [{"programId": PROGRAM}]}},
            "meta": {"err": None, "preTokenBalances": balances(0), "postTokenBalances": balances(1),
                     "innerInstructions": [{"index": 0, "instructions": instructions}]}}


def move(amount=100, *, source="a", destination="b", owner=A, recipient=B,
         before=100, recipient_before=0, slot=11, signature=None, timestamp=101):
    return transaction({source: (owner, MINT, before, before - amount),
                        destination: (recipient, MINT, recipient_before, recipient_before + amount)},
                       [(source, destination, amount)], slot=slot, signature=signature, timestamp=timestamp)


def sale(amount=100, *, source="a", owner=A, before=100, slot=11, timestamp=101):
    tx = transaction({source: (owner, MINT, before, before - amount),
                      "vault": (SERVICE, MINT, 1000, 1000 + amount),
                      "quote": (owner, QUOTE, 0, amount), "quote-vault": (SERVICE, QUOTE, 1000, 1000 - amount)},
                     [(source, "vault", amount), ("quote-vault", "quote", amount)], slot=slot, timestamp=timestamp)
    witness = {"signature": tx["transaction"]["signatures"][0], "program_id": PROGRAM,
               "outer_index": 0, "mint": MINT, "owner": owner, "input_account": source,
               "input_vault": "vault", "input_amount_raw": str(amount), "output_mint": QUOTE,
               "output_account": "quote", "output_vault": "quote-vault", "output_amount_raw": str(amount)}
    swap = {"signature": witness["signature"], "kind": "sell", "token_address": MINT,
            "pool_address": SERVICE, "coordination_sale_owner": owner, "coordination_sale_amount": amount}
    return tx, witness, swap


def seed(*, balance=100, bought=100, lower=100, upper=100, account="a", owner=A):
    return {"mint": MINT, "account": account, "owner": owner, "balance_raw": str(balance),
            "bought_raw": str(bought), "retained_lower_raw": str(lower), "retained_upper_raw": str(upper)}


def evidence(txs, *, seeds=None, complete=True, **kwargs):
    batches = [parse(tx, MINT) for tx in txs]
    return summarize(MINT, seeds or [seed()], batches, from_slot=10, to_slot=30,
                     history_complete=complete, **kwargs)


class ParseTests(unittest.TestCase):
    def test_raw_transfer_is_exact_and_not_sale(self):
        tx = move(amount=2**60, before=2**60)
        original = copy.deepcopy(tx)
        batch = parse(tx, MINT)
        self.assertEqual(batch["status"], "parsed")
        self.assertEqual(batch["transfers"][0]["amount_raw"], str(2**60))
        self.assertEqual(batch["transfers"][0]["source_owner"], A)
        self.assertEqual(batch["transfers"][0]["kind"], "transfer")
        self.assertEqual(tx, original)

    def test_true_decoded_swap_requires_exact_transfer_and_proceeds(self):
        tx, witness, _ = sale()
        batch = parse(tx, MINT, swap_witnesses=[witness], swap_program_ids=[PROGRAM])
        self.assertEqual(batch["transfers"][0]["kind"], "verified_sale")
        for change in ({"signature": "other"}, {"owner": B}, {"mint": "other-Mint"},
                       {"input_amount_raw": "99"}, {"output_amount_raw": "99"},
                       {"output_mint": MINT}, {"outer_index": 2}, {"program_id": "unknown"}):
            with self.subTest(change=change):
                bad = parse(tx, MINT, swap_witnesses=[dict(witness, **change)], swap_program_ids=[PROGRAM])
                self.assertEqual(bad["transfers"][0]["kind"], "transfer")
        self.assertEqual(parse(tx, MINT, swap_witnesses=[witness])["transfers"][0]["kind"], "transfer")

    def test_quote_proceeds_to_other_owner_is_not_sale(self):
        tx, witness, _ = sale()
        for field in ("preTokenBalances", "postTokenBalances"):
            tx["meta"][field][2]["owner"] = B
        self.assertEqual(parse(tx, MINT, swap_witnesses=[witness], swap_program_ids=[PROGRAM])["transfers"][0]["kind"], "transfer")

    def test_changed_identity_or_missing_lifecycle_is_partial(self):
        tx = move()
        tx["meta"]["postTokenBalances"][1]["owner"] = C
        batch = parse(tx, MINT)
        self.assertEqual(batch["status"], "partial")
        self.assertFalse(batch["accounts"]["b"]["identity_resolved"])
        tx = move()
        tx["meta"]["preTokenBalances"].pop()
        self.assertFalse(parse(tx, MINT)["accounts"]["b"]["complete"])

    def test_ui_only_or_bad_raw_values_do_not_become_evidence(self):
        for raw in (None, -1, True, 1.2, "1e2", "-1", "NaN"):
            tx = move()
            tx["meta"]["innerInstructions"][0]["instructions"][0]["parsed"]["info"]["amount"] = raw
            batch = parse(tx, MINT)
            self.assertEqual(batch["transfers"], [])
            self.assertEqual(batch["status"], "partial")
        tx = move()
        tx["meta"]["preTokenBalances"][0]["uiTokenAmount"] = {"uiAmount": 100, "decimals": 0}
        self.assertEqual(parse(tx, MINT)["status"], "partial")

    def test_wrong_mint_and_checked_decimals_fail_closed(self):
        tx = move()
        instruction = tx["meta"]["innerInstructions"][0]["instructions"][0]
        instruction["parsed"]["type"] = "transferChecked"
        instruction["parsed"]["info"].update(mint="different", tokenAmount={"amount": "100", "decimals": 0})
        self.assertFalse(parse(tx, MINT)["transfers"])
        instruction["parsed"]["info"].update(mint=MINT, tokenAmount={"amount": "100", "decimals": 1})
        self.assertFalse(parse(tx, MINT)["transfers"])

    def test_unparsed_token_instruction_invalidates_even_net_zero_history(self):
        tx = move(amount=0)
        tx["meta"]["innerInstructions"][0]["instructions"] = [{"program": "spl-token", "data": "unknown"}]
        batch = parse(tx, MINT)
        self.assertEqual(batch["status"], "partial")
        self.assertFalse(batch["accounts"]["a"]["complete"])

    def test_missing_inner_coverage_and_limit_not_silent_prefix(self):
        tx = move()
        tx["meta"]["innerInstructions"] = None
        self.assertEqual(parse(tx, MINT)["status"], "partial")
        self.assertEqual(parse(move(), MINT, max_instructions=1)["status"], "unavailable")
        self.assertFalse(parse(move(), MINT, max_accounts=1)["transfers"])

    def test_failed_missing_and_malformed_transactions(self):
        tx = move()
        tx["meta"]["err"] = {"InstructionError": [0, "failed"]}
        self.assertEqual(parse(tx, MINT)["status"], "failed")
        self.assertFalse(parse(tx, MINT)["transfers"])
        for tx in (None, {}, {"transaction": []}, {"transaction": {"message": "bad"}},
                   {"transaction": {"signatures": "bad"}}, dict(move(), slot=None)):
            with self.subTest(tx=tx):
                self.assertEqual(parse(tx, MINT)["status"], "unavailable")

    def test_malformed_nested_instruction_is_partial_not_exception(self):
        for change in ({"parsed": "bad"}, {"parsed": {"type": "transfer", "info": "bad"}},
                       {"parsed": {"type": []}},
                       {"parsed": {"type": "transfer", "info": {"source": [], "destination": "b"}}},
                       {"parsed": {"type": "transferChecked", "info": {"source": "a", "destination": "b", "tokenAmount": "bad"}}},
                       {"programId": ["bad"]}):
            tx = move()
            tx["meta"]["innerInstructions"][0]["instructions"][0].update(change)
            batch = parse(tx, MINT)
            self.assertEqual(batch["status"], "partial")
            self.assertFalse(batch["transfers"])

    def test_impossible_instruction_order_cannot_verify_sale(self):
        tx = transaction({"a": (A, MINT, 0, 0), "b": (B, MINT, 100, 100)},
                         [("a", "b", 100), ("b", "a", 100)])
        batch = parse(tx, MINT)
        self.assertIn("unreconciled_intermediate_balance", batch["issues"])
        self.assertFalse(batch["accounts"]["a"]["complete"])


class ReplayTests(unittest.TestCase):
    def test_transfer_chain_cycle_and_split_are_not_ownership(self):
        txs = [move(100), move(60, source="b", destination="c", owner=B, recipient=C, before=100, slot=12),
               move(20, source="c", destination="a", owner=C, recipient=A, before=60, slot=13)]
        out = evidence(txs)
        self.assertEqual(out["amounts_raw"], {"original": "20", "transferred": "80", "sold": "0", "unknown": "0"})
        self.assertEqual(out["ownership"], "not_established")
        self.assertTrue(all(e["common_control"] == "not_established" for e in out["edges"]))
        cycle = evidence([move(), move(100, source="b", destination="a", owner=B, recipient=A, slot=12)])
        self.assertEqual(cycle["amounts_raw"]["original"], "100")
        split = transaction({"a": (A, MINT, 100, 0), "b": (B, MINT, 0, 60), "c": (C, MINT, 0, 40)},
                            [("a", "b", 60), ("a", "c", 40)])
        out = evidence([split])
        self.assertEqual(out["amounts_raw"]["transferred"], "100")
        self.assertEqual(out["amounts_raw"]["sold"], "0")

    def test_sale_and_buyback_does_not_restore_original_inventory(self):
        tx, witness, _ = sale()
        batches = [parse(tx, MINT, swap_witnesses=[witness], swap_program_ids=[PROGRAM]),
                   parse(move(100, source="vault", destination="a", owner=SERVICE, recipient=A,
                              before=1100, slot=12), MINT)]
        out = summarize(MINT, [seed()], batches, from_slot=10, to_slot=30, history_complete=True,
                        service_owners=[SERVICE])
        self.assertEqual(out["amounts_raw"], {"original": "0", "transferred": "0", "sold": "100", "unknown": "0"})
        self.assertEqual(out["upper_bounds_raw"]["original"], "0")
        self.assertEqual(out["locations"][0]["current_balance_raw"], "100")
        self.assertFalse(out["confirmation_eligible"])

    def test_buyback_after_direct_transfer_cannot_inflate_original(self):
        out = evidence([move(), move(100, source="vault", destination="a", owner=SERVICE,
                                    recipient=A, before=1000, slot=12)], service_owners=[SERVICE])
        self.assertEqual(out["amounts_raw"]["original"], "0")
        self.assertEqual(out["amounts_raw"]["transferred"], "100")
        self.assertEqual(out["upper_bounds_raw"]["original"], "0")

    def test_mixing_has_conservative_non_additive_bounds(self):
        out = evidence([move(70, before=150)], seeds=[seed(balance=150)])
        self.assertEqual(out["amounts_raw"], {"original": "30", "transferred": "20", "sold": "0", "unknown": "50"})
        self.assertEqual(out["upper_bounds_raw"]["original"], "80")
        self.assertEqual(out["upper_bounds_raw"]["transferred"], "70")
        self.assertTrue(out["upper_bounds_are_non_additive"])

    def test_service_destination_is_unresolved_and_not_a_location(self):
        out = evidence([move(40, recipient=SERVICE)], service_owners=[SERVICE])
        self.assertEqual(out["amounts_raw"], {"original": "60", "transferred": "0", "sold": "0", "unknown": "40"})
        self.assertEqual(len(out["locations"]), 1)
        self.assertEqual(out["edges"][0]["resolution"], "service_destination_unresolved")

    def test_default_partial_history_never_confirms_current_holdings(self):
        out = summarize(MINT, [seed()], [parse(move(40), MINT)], from_slot=10, to_slot=30)
        self.assertEqual(out["amounts_raw"], {"original": "0", "transferred": "0", "sold": "0", "unknown": "100"})
        self.assertEqual(out["status"], "partial")
        self.assertTrue(all(row["current_balance_raw"] is None for row in out["locations"]))
        self.assertEqual(out["upper_bounds_raw"]["original"], "100")

    def test_balance_drop_or_incomplete_transaction_not_sale(self):
        tx = transaction({"a": (A, MINT, 100, 0)})
        out = evidence([tx])
        self.assertEqual(out["amounts_raw"]["sold"], "0")
        self.assertEqual(out["amounts_raw"]["unknown"], "100")
        self.assertEqual(out["status"], "partial")

    def test_closing_balance_gap_erases_retention_not_invents_sale(self):
        closing = [{"mint": MINT, "account": "a", "owner": A, "slot": 30, "balance_raw": "0"}]
        out = evidence([], closing_balances=closing)
        self.assertEqual(out["amounts_raw"]["sold"], "0")
        self.assertEqual(out["amounts_raw"]["unknown"], "100")
        self.assertIn("closing_balance_mismatch", out["issues"])

    def test_duplicate_and_conflicting_batches(self):
        tx = move(40)
        self.assertEqual(evidence([tx, tx])["amounts_raw"]["transferred"], "40")
        out = evidence([tx, move(41)])
        self.assertIn("conflicting_duplicate_transaction", out["issues"])
        self.assertEqual(out["amounts_raw"]["unknown"], "100")

    def test_same_slot_requires_canonical_order_not_signature_sort(self):
        one = parse(move(100, signature="z"), MINT)
        two = parse(move(100, source="b", destination="a", owner=B, recipient=A, signature="a"), MINT)
        out = summarize(MINT, [seed()], [one, two], from_slot=10, to_slot=30, history_complete=True)
        self.assertIn("ambiguous_same_slot_order", out["issues"])
        self.assertEqual(out["amounts_raw"]["original"], "0")
        one["transaction_index"], two["transaction_index"] = 0, 1
        out = summarize(MINT, [seed()], [two, one], from_slot=10, to_slot=30, history_complete=True)
        self.assertEqual(out["amounts_raw"]["original"], "100")

    def test_account_depth_and_event_limits_do_not_silently_confirm(self):
        out = evidence([move()], max_accounts=1)
        self.assertEqual(out["amounts_raw"]["unknown"], "100")
        self.assertIn("account_limit", out["issues"])
        out = evidence([move()], max_depth=0)
        self.assertIn("depth_limit", out["issues"])
        self.assertEqual(out["amounts_raw"]["sold"], "0")
        out = evidence([move()], max_events=0)
        self.assertIn("event_limit", out["issues"])
        self.assertEqual(out["amounts_raw"]["original"], "0")

    def test_exact_seed_mint_and_account_scope(self):
        for seeds in ([seed(), seed()], [dict(seed(), mint="other")], [dict(seed(), retained_lower_raw="101")]):
            with self.assertRaises(ValueError):
                evidence([], seeds=seeds)
        with self.assertRaises(ValueError):
            evidence([], service_owners=[A])

    def test_disjoint_seed_accounts_conserve_total_and_same_owner_transfers(self):
        tx = move(40, recipient=A)
        out = evidence([tx])
        self.assertEqual(out["amounts_raw"]["original"], "100")
        self.assertEqual(out["amounts_raw"]["transferred"], "0")

    def test_generated_split_chains_always_conserve_lower_bounds(self):
        for initial_extra, first, second in itertools.product((0, 10, 100), (0, 10, 50, 100), (0, 5, 10)):
            if second > first:
                continue
            out = evidence([move(first, before=100 + initial_extra),
                            move(second, source="b", destination="c", owner=B, recipient=C,
                                 before=first, slot=12)], seeds=[seed(balance=100 + initial_extra)])
            self.assertEqual(sum(map(int, out["amounts_raw"].values())), 100)
            self.assertTrue(all(int(v) >= 0 for v in out["amounts_raw"].values()))

    def test_partial_swap_reports_observation_not_original_sale_or_hold(self):
        tx, witness, _ = sale()
        out = summarize(MINT, [seed()], [parse(tx, MINT, swap_witnesses=[witness], swap_program_ids=[PROGRAM])],
                        from_slot=10, to_slot=30)
        self.assertEqual(out["observed_verified_sale_raw"], "100")
        self.assertEqual(out["amounts_raw"]["sold"], "0")
        self.assertEqual(out["amounts_raw"]["unknown"], "100")
        self.assertFalse(out["history_complete"])

    def test_service_return_does_not_restore_guaranteed_provenance(self):
        out = evidence([move(40, recipient=SERVICE),
                        move(40, source="b", destination="a", owner=SERVICE, recipient=A,
                             before=40, recipient_before=60, slot=12)], service_owners=[SERVICE])
        self.assertEqual(out["amounts_raw"]["original"], "60")
        self.assertEqual(out["amounts_raw"]["unknown"], "40")
        self.assertEqual(out["upper_bounds_raw"]["original"], "100")


class PartialPoolAnnotationTests(unittest.TestCase):
    def test_pool_only_annotation_does_not_change_retention_or_claim_all_held(self):
        tx = move(40)
        out = annotate(MINT, [tx, tx], [], [A], signal_timestamp=100)
        self.assertEqual(out["owners"][A]["direct_transfer_raw"], "40")
        self.assertEqual(out["owners"][A]["verified_sale_raw"], "0")
        self.assertFalse(out["affects_original_cohort_retention"])
        self.assertTrue(out["unresolved_outflows_possible"])
        self.assertIsNone(out["retention_bounds_raw"])
        self.assertIsNone(out["original_position_sold_raw"])
        self.assertEqual(out["status"], "partial")

    def test_existing_swap_rows_are_candidates_not_verified_sales(self):
        tx, witness, swap = sale()
        out = annotate(MINT, [tx], [swap, swap], [A], signal_timestamp=100)
        self.assertEqual(out["owners"][A]["reconciled_pool_sale_candidate_raw"], "100")
        self.assertEqual(out["owners"][A]["verified_sale_raw"], "0")
        self.assertEqual(out["observations"][0]["original_position_attribution"], "unknown")
        out = annotate(MINT, [tx], [swap], [A], signal_timestamp=100,
                       swap_witnesses=[witness], swap_program_ids=[PROGRAM])
        self.assertEqual(out["owners"][A]["verified_sale_raw"], "100")
        self.assertEqual(out["owners"][A]["reconciled_pool_sale_candidate_raw"], "0")

    def test_balance_drop_and_fake_kind_sell_not_verified_sale(self):
        tx = transaction({"a": (A, MINT, 100, 0)})
        swap = {"signature": "tx-11", "kind": "sell", "token_address": MINT,
                "pool_address": SERVICE, "coordination_sale_owner": A, "coordination_sale_amount": 100}
        out = annotate(MINT, [tx], [swap], [A], signal_timestamp=100)
        self.assertEqual(out["owners"][A]["unknown_outflow_raw"], "100")
        self.assertEqual(out["owners"][A]["verified_sale_raw"], "0")
        self.assertEqual(out["owners"][A]["reconciled_pool_sale_candidate_raw"], "0")

    def test_transfer_plus_unknown_debit_keeps_both_without_double_count(self):
        tx = move(40)
        tx["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = "50"
        out = annotate(MINT, [tx], [], [A], signal_timestamp=100)
        self.assertEqual(out["owners"][A]["direct_transfer_raw"], "40")
        self.assertEqual(out["owners"][A]["unknown_outflow_raw"], "10")

    def test_services_are_not_owned_recipients(self):
        out = annotate(MINT, [move(50, recipient=SERVICE)], [], [A], signal_timestamp=100, service_owners=[SERVICE])
        self.assertEqual(out["owners"][A]["service_outflow_raw"], "50")
        self.assertEqual(out["owners"][A]["direct_transfer_raw"], "0")
        self.assertEqual(out["observations"][0]["common_control"], "not_established")

    def test_equal_signal_seconds_prior_and_future_are_not_assumed_post_signal(self):
        out = annotate(MINT, [move(timestamp=99), move(timestamp=100), move(timestamp=102)], [], [A],
                       signal_timestamp=100, checked_timestamp=101)
        self.assertEqual(out["owners"][A]["direct_transfer_raw"], "0")
        self.assertIn("same_timestamp_as_signal", out["issues"])
        self.assertIn("transaction_after_checked_timestamp", out["issues"])
        iso = annotate(MINT, [move(timestamp=101)], [], [A], signal_timestamp="1970-01-01T00:01:40Z")
        self.assertEqual(iso["owners"][A]["direct_transfer_raw"], "100")

    def test_conflicts_wrong_mint_missing_history_and_limits_remain_partial(self):
        out = annotate(MINT, [move(40), move(50)], [], [A], signal_timestamp=100)
        self.assertEqual(out["owners"][A]["direct_transfer_raw"], "0")
        self.assertIn("conflicting_duplicate_transaction", out["issues"])
        out = annotate("Other-Mint", [move(40)], [], [A], signal_timestamp=100)
        self.assertEqual(out["owners"][A]["direct_transfer_raw"], "0")
        out = annotate(MINT, [move(40)], [], [A], signal_timestamp=100, max_events=0)
        self.assertIn("event_limit", out["issues"])
        out = annotate(MINT, [], [], [A], signal_timestamp=100)
        self.assertTrue(out["unresolved_outflows_possible"])
        self.assertFalse(out["confirmation_eligible"])

    def test_case_sensitive_mint_and_owner_are_never_merged(self):
        out = annotate(MINT.lower(), [move()], [], [A, A.lower()], signal_timestamp=100)
        self.assertEqual(out["owners"][A]["direct_transfer_raw"], "0")
        out = annotate(MINT, [move()], [], [A.lower()], signal_timestamp=100)
        self.assertEqual(out["owners"][A.lower()]["direct_transfer_raw"], "0")

    def test_fractional_signal_iso_is_supported_and_same_second_is_ambiguous(self):
        out = annotate(MINT, [move(timestamp=100), move(timestamp=101, signature="later")], [], [A],
                       signal_timestamp="1970-01-01T00:01:40.500Z", checked_timestamp="1970-01-01T00:01:41.250Z")
        self.assertEqual(out["owners"][A]["direct_transfer_raw"], "100")
        self.assertIn("same_timestamp_as_signal", out["issues"])

    def test_unmatched_balance_observations_respect_event_limit(self):
        tx = transaction({"a": (A, MINT, 100, 0), "b": (A, MINT, 100, 0)})
        out = annotate(MINT, [tx], [], [A], signal_timestamp=100, max_events=1)
        self.assertIn("event_limit", out["issues"])
        self.assertFalse(out["observations"])

    def test_failed_quote_or_swap_conflict_is_service_outflow_not_sale(self):
        tx, _, swap = sale()
        tx["meta"]["innerInstructions"][0]["instructions"].pop()
        out = annotate(MINT, [tx], [swap], [A], signal_timestamp=100)
        self.assertEqual(out["owners"][A]["reconciled_pool_sale_candidate_raw"], "0")
        self.assertEqual(out["owners"][A]["service_outflow_raw"], "100")
        tx, _, swap = sale()
        out = annotate(MINT, [tx], [swap, dict(swap, coordination_sale_owner=B)], [A], signal_timestamp=100)
        self.assertIn("conflicting_pool_swap_rows", out["issues"])
        self.assertEqual(out["owners"][A]["reconciled_pool_sale_candidate_raw"], "0")

    def test_annotation_does_not_mutate_inputs_or_count_unresolved_owner(self):
        tx, _, swap = sale()
        before = copy.deepcopy((tx, swap))
        annotate(MINT, [tx], [swap], [A], signal_timestamp=100)
        self.assertEqual((tx, swap), before)
        tx = move()
        tx["meta"]["postTokenBalances"][0]["owner"] = B
        out = annotate(MINT, [tx], [], [A], signal_timestamp=100)
        self.assertEqual(out["owners"][A]["direct_transfer_raw"], "0")
        self.assertIn("unresolved_transfer_owner", out["issues"])


if __name__ == "__main__":
    unittest.main()
