import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import base58

from wallet_activity import (AuditBudget, PUMP_AMM, SELL, TOKEN_PROGRAMS, advance_wallet_history,
                             decode_wallet_activity, recover_sale_history_status, summarize_wallet_activity)
from tests.test_solana_position_lineage import A, B, MINT, SERVICE, move
from scanner import public_signal_thesis, run_wallet_activity_tasks

TOKEN = next(program for program in TOKEN_PROGRAMS if program.startswith("Tokenkeg"))


def transfer(amount=40, *, timestamp=101, recipient=B):
    tx = move(amount, before=100, timestamp=timestamp, recipient=recipient)
    for group in tx["meta"]["innerInstructions"]:
        for ins in group["instructions"]:
            ins["programId"] = TOKEN
    return tx


def pump_sale(amount=40, *, timestamp=101):
    tx = transfer(amount, timestamp=timestamp, recipient=SERVICE)
    addresses = [SERVICE, A, "config", MINT, "quote", "a", "user-quote", "b", "vault-quote",
                 "fee", "fee-account", TOKEN, TOKEN, "system", "associated", "authority", PUMP_AMM]
    tx["transaction"]["message"]["instructions"] = [{"programId": PUMP_AMM, "accounts": addresses,
        "data": base58.b58encode(SELL + amount.to_bytes(8, "little") + bytes(8)).decode()}]
    return tx


def thesis():
    return {"token_address": MINT, "signal_at": 100, "cohort": [{"owner": A, "attributed_tokens": 100}],
            "supply": 1000, "cohort_wallet_coverage_pct": 100, "cohort_token_coverage_pct": 100,
            "original_sale_history_status": "unknown", "original_sale_history_issues": ["history_gap"],
            "signal_confirmation": {"status": "candidate"}}


class DecodeWalletActivityTests(unittest.TestCase):
    def test_plain_transfer_and_public_destination_are_not_sales(self):
        tx = transfer()
        before = copy.deepcopy(tx)
        facts = decode_wallet_activity(tx, MINT, A)
        self.assertEqual(facts["events"][0]["kind"], "transferred")
        self.assertEqual(facts["events"][0]["amount_raw"], "40")
        self.assertEqual(facts["delta_raw"], "-40")
        self.assertEqual(facts["issues"], [])
        self.assertEqual(tx, before)
        self.assertEqual(decode_wallet_activity(tx, MINT, A, services=[B])["events"][0]["kind"], "service")

    def test_successful_exact_pumpswap_sell_is_recognized(self):
        facts = decode_wallet_activity(pump_sale(), MINT, A)
        self.assertEqual(facts["events"][0]["kind"], "sold")
        self.assertEqual(facts["events"][0]["protocol"], "PumpSwap")
        self.assertEqual(facts["issues"], [])

    def test_lp_deposit_unknown_program_wrong_owner_or_amount_cannot_be_sale(self):
        for mutation in ("program", "discriminator", "amount", "owner", "mint", "vault"):
            tx = pump_sale()
            ins = tx["transaction"]["message"]["instructions"][0]
            if mutation == "program": ins["programId"] = "untrusted"
            elif mutation == "discriminator": ins["data"] = base58.b58encode(bytes(24)).decode()
            elif mutation == "amount": ins["data"] = base58.b58encode(SELL + (41).to_bytes(8, "little") + bytes(8)).decode()
            elif mutation == "owner": ins["accounts"][1] = B
            elif mutation == "mint": ins["accounts"][3] = "other-mint"
            else: ins["accounts"][7] = "other-vault"
            with self.subTest(mutation=mutation):
                self.assertNotIn("sold", [event["kind"] for event in decode_wallet_activity(tx, MINT, A)["events"]])

    def test_internal_transfer_failed_transaction_and_wrong_mint(self):
        self.assertEqual(decode_wallet_activity(transfer(recipient=A), MINT, A)["events"][0]["kind"], "internal")
        tx = pump_sale(); tx["meta"]["err"] = {"InstructionError": [0, "failed"]}
        self.assertEqual(decode_wallet_activity(tx, MINT, A)["events"], [])
        self.assertEqual(decode_wallet_activity(transfer(), "wrong-mint", A)["events"], [])

    def test_unknown_debit_is_not_a_sale(self):
        tx = transfer(); tx["meta"]["innerInstructions"] = []
        facts = decode_wallet_activity(tx, MINT, A)
        self.assertEqual(facts["events"][0]["kind"], "unclassified")
        self.assertIn("unreconciled_owner_debit", facts["issues"])
        tx["meta"].pop("preTokenBalances")
        self.assertEqual(decode_wallet_activity(tx, MINT, A)["issues"], ["missing_token_snapshot"])

    def test_malformed_raw_amount_or_owner_change_blocks_completeness(self):
        for value in ("NaN", "-1", True):
            tx = transfer(); tx["meta"]["preTokenBalances"][0]["uiTokenAmount"]["amount"] = value
            self.assertIn("invalid_token_snapshot", decode_wallet_activity(tx, MINT, A)["issues"])
        tx = transfer(); tx["meta"]["postTokenBalances"][0]["owner"] = B
        self.assertIn("token_account_identity_changed", decode_wallet_activity(tx, MINT, A)["issues"])


class ResumableHistoryTests(unittest.TestCase):
    def rpc(self, data, cursor=None):
        rpc = Mock(archive_order=["alchemy", "helius"])
        rpc._route_call.return_value = ({"data": data, "paginationToken": cursor}, "alchemy")
        return rpc

    def test_wallet_ata_history_and_durable_cursor_are_pinned(self):
        t = thesis(); budget = AuditBudget(2)
        rpc = self.rpc([transfer()], "opaque-cursor")
        self.assertTrue(advance_wallet_history(rpc, t, t["cohort"][0], budget, 500))
        opts = rpc._route_call.call_args.args[1][1]
        self.assertEqual(opts["filters"], {"status": "any", "tokenAccounts": "all", "blockTime": {"gte":100, "lte":380}})
        self.assertEqual(opts["commitment"], "finalized")
        rpc._route_call.return_value = ({"data": [transfer()], "paginationToken": None}, "alchemy")
        self.assertTrue(advance_wallet_history(rpc, t, t["cohort"][0], budget, 600))
        self.assertEqual(rpc._route_call.call_args.kwargs["order"], ["alchemy"])
        self.assertEqual(rpc._route_call.call_args.args[1][1]["paginationToken"], "opaque-cursor")
        self.assertEqual(len(t["wallet_activity_checks"][A]["events"]), 1)
        self.assertEqual(t["wallet_activity_checks"][A]["complete_through"], 380)
        self.assertEqual(budget.used, 2)
        summary = summarize_wallet_activity(t, 600)
        self.assertEqual(summary["amounts_tokens"]["transferred"], 40)
        self.assertEqual(summary["amounts_supply_pct"]["transferred"], 4)
        self.assertTrue(recover_sale_history_status(t, summary))
        self.assertEqual(t["original_sale_history_status"], "reconstructed_from_capture")
        self.assertEqual(t["signal_confirmation"]["status"], "candidate")

    def test_failure_looping_cursor_and_provider_change_do_not_advance(self):
        for response in (RuntimeError("private URL must never leak"),
                         ({"data":[], "paginationToken":"opaque"}, "alchemy"),
                         ({"data":[], "paginationToken":None}, "helius")):
            t = thesis(); t["wallet_activity_checks"] = {A:{"events":{}, "pages":0,
                "query":{"from":100,"to":380,"cursor":"opaque","provider":"alchemy","issues":[]}}}
            rpc = self.rpc([])
            if isinstance(response, Exception): rpc._route_call.side_effect = response
            else: rpc._route_call.return_value = response
            self.assertFalse(advance_wallet_history(rpc, t, t["cohort"][0], AuditBudget(1), 500))
            audit = t["wallet_activity_checks"][A]
            self.assertEqual(audit["query"]["cursor"], "opaque")
            self.assertEqual(audit["pages"], 0)
            self.assertNotIn("private", str(audit))

    def test_partial_history_missing_buyers_and_service_destinations_never_clear_unknown(self):
        for mode in ("page", "cohort", "service", "debit"):
            t = thesis(); tx = transfer(recipient=SERVICE) if mode == "service" else transfer()
            if mode == "debit": tx["meta"]["innerInstructions"] = []
            rpc = self.rpc([tx], "pending" if mode == "page" else None)
            if mode == "cohort": t["cohort_wallet_coverage_pct"] = 50
            advance_wallet_history(rpc, t, t["cohort"][0], AuditBudget(1), 500, services=[SERVICE])
            self.assertFalse(recover_sale_history_status(t, summarize_wallet_activity(t, 500)))
            self.assertEqual(t["original_sale_history_status"], "unknown")

    def test_incremental_window_does_not_fetch_already_completed_history(self):
        t = thesis(); rpc = self.rpc([pump_sale()])
        advance_wallet_history(rpc, t, t["cohort"][0], AuditBudget(1), 500)
        rpc._route_call.return_value = ({"data":[], "paginationToken":None}, "alchemy")
        advance_wallet_history(rpc, t, t["cohort"][0], AuditBudget(1), 1500)
        self.assertEqual(rpc._route_call.call_args.args[1][1]["filters"]["blockTime"], {"gte":381,"lte":1380})
        self.assertEqual(summarize_wallet_activity(t, 1500)["amounts_tokens"]["sold"], 40)

    def test_global_budget_is_hard_and_empty_history_is_not_zero_sale_proof(self):
        t = thesis(); rpc = self.rpc([]); budget = AuditBudget(0)
        self.assertFalse(advance_wallet_history(rpc, t, t["cohort"][0], budget, 500))
        rpc._route_call.assert_not_called()
        summary = summarize_wallet_activity(t, 500)
        self.assertEqual(summary["status"], "backfilling")
        self.assertEqual(summary["wallets_checked"], 0)

    def test_missing_receipts_replay_without_advancing_and_can_recover(self):
        t = thesis(); tx = transfer(); tx["meta"].pop("preTokenBalances")
        rpc = self.rpc([tx])
        advance_wallet_history(rpc, t, t["cohort"][0], AuditBudget(1), 500)
        audit = t["wallet_activity_checks"][A]
        self.assertNotIn("complete_through", audit)
        self.assertEqual(audit["status"], "partial")
        self.assertEqual(audit["query"]["from"], 100)
        rpc._route_call.return_value = ({"data":[transfer()],"paginationToken":None},"alchemy")
        advance_wallet_history(rpc, t, t["cohort"][0], AuditBudget(1), 4200)
        self.assertEqual(t["wallet_activity_checks"][A]["issues"], [])
        self.assertEqual(t["wallet_activity_checks"][A]["complete_through"], 380)

    def test_complete_post_catch_history_does_not_repair_legacy_inventory(self):
        t = thesis(); t["original_sale_history_issues"] = ["legacy_original_sale_history_not_reconstructed"]
        advance_wallet_history(self.rpc([pump_sale()]), t, t["cohort"][0], AuditBudget(1), 500)
        summary = summarize_wallet_activity(t, 500)
        self.assertEqual(summary["status"], "checked")
        self.assertFalse(recover_sale_history_status(t, summary))
        self.assertEqual(t["original_sale_history_status"], "unknown")

    def test_scheduler_global_budget_fairness_and_unattempted_progress(self):
        pools, state = [], {"pools":{}}
        for index in range(8):
            t = thesis(); t["signal_at"] = 300000 if index < 6 else 100
            t["cohort"][0]["owner"] = str(index)
            pool = SimpleNamespace(pool_address=str(index))
            pools.append(pool); state["pools"][str(index)] = {"signal_thesis":t}
        rpc = self.rpc([])
        stats = run_wallet_activity_tasks(rpc, pools, state, {"wallet_activity_pages":4}, 400000)
        calls = [call.args[1][0] for call in rpc._route_call.call_args_list]
        self.assertEqual(calls, ["0", "1", "2", "6"])
        self.assertEqual(stats["pages"], 4)
        self.assertEqual(stats["checked"], 4)
        self.assertEqual(state["pools"]["7"]["signal_thesis"]["wallet_activity"]["wallets_total"], 1)
        self.assertEqual(state["pools"]["7"]["signal_thesis"]["wallet_activity"]["wallets_checked"], 0)
        run_wallet_activity_tasks(rpc, pools, state, {"wallet_activity_pages":4}, 400100)
        self.assertIn("7", [call.args[1][0] for call in rpc._route_call.call_args_list])

    def test_public_summary_keeps_receipts_in_details_without_private_cursors(self):
        t = thesis()
        self.assertNotIn("wallet_activity", public_signal_thesis(t))
        advance_wallet_history(self.rpc([pump_sale()]), t, t["cohort"][0], AuditBudget(1), 500)
        summarize_wallet_activity(t, 500)
        public = public_signal_thesis(t)
        self.assertNotIn("events", public["wallet_activity"])
        self.assertNotIn("owners", public["wallet_activity"])
        self.assertNotIn("wallet_activity_checks", public)
        self.assertEqual(public["cohort_wallets"][0]["activity"]["events"][0]["kind"], "sold")


if __name__ == "__main__": unittest.main()
