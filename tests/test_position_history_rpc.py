import unittest
from unittest.mock import Mock

from position_history_rpc import PositionHistoryRpc, check_receipt_positions
from tests.test_solana_position_lineage import move
from tests.test_solana_receipt_lineage_seeds import buy, capture, MINT, A


def transaction(signature, slot, index=0):
    return {"slot": slot, "transactionIndex": index, "meta": {"err": None},
            "transaction": {"signatures": [signature]}}


class PositionHistoryRpcTests(unittest.TestCase):
    def adapter(self, rows, cursor=None):
        rpc = Mock(enhanced_order=["alchemy"])
        rpc._route_call.return_value = ({"data": rows, "paginationToken": cursor}, "alchemy")
        adapter = PositionHistoryRpc(rpc, max_pages=1)
        adapter.boundaries["account"] = {"signature": "seed"}
        return adapter, rpc

    def test_seed_tail_attested_only_with_original_receipt_and_no_later_activity(self):
        tx, swap = buy(slot=100, signature="seed")
        adapter, rpc = self.adapter([tx, transaction("later", 101)])
        adapter.boundaries["a"] = {**capture(tx, swap)["owners"][A]["seeds"][0], "signature": "seed"}
        page = adapter.history_page("a", mint=MINT, from_slot=100, to_slot=200, cursor=None, limit=32)
        self.assertEqual(len(page["transactions"]), 1)
        self.assertTrue(page["coverage"]["seed_boundary"]["no_later_successful_activity"])
        self.assertEqual(page["coverage"]["seed_boundary"]["bought_raw"], "100")
        self.assertEqual(page["coverage"]["seed_boundary"]["balance_raw"], "150")
        self.assertEqual(rpc._route_call.call_args.args[1][1]["filters"],
                         {"status": "any", "tokenAccounts": "none", "slot": {"gte": 100, "lte": 200}})
        with self.assertRaises(RuntimeError):
            adapter.history_page("account", mint="mint", from_slot=100, to_slot=200, cursor=None, limit=32)

    def test_frozen_receipt_finalized_history_replays_raw_transfer_or_remains_unknown(self):
        receipt, swap = buy(before=0, slot=100, signature="seed")
        frozen = capture(receipt, swap)
        transfer = move(40, before=100, slot=101, signature="transfer")
        for finalized_before, expected in (
                (0, {"original": "60", "transferred": "40", "sold": "0", "unknown": "0"}),
                (1, {"original": "0", "transferred": "0", "sold": "0", "unknown": "100"})):
            with self.subTest(finalized_receipt_before_raw=finalized_before):
                finalized_receipt, _ = buy(before=finalized_before, slot=100, signature="seed")
                rpc = Mock(enhanced_order=["alchemy"])

                def route(method, params, **kwargs):
                    if method == "getSlot":
                        self.assertEqual(params, [{"commitment": "finalized"}])
                        return 200, "alchemy"
                    self.assertEqual(method, "getTransactionsForAddress")
                    account, options = params
                    self.assertIn(account, ("a", "b"))
                    self.assertEqual(options["commitment"], "finalized")
                    self.assertEqual(options["filters"]["slot"], {"gte": 100, "lte": 200})
                    rows = [finalized_receipt, transfer] if account == "a" else [transfer]
                    return {"data": rows, "paginationToken": None}, "alchemy"

                rpc._route_call.side_effect = route
                thesis = {"receipt_position_seeds": frozen,
                          "cohort": [{"owner": A, "movement_status": "reduced_unresolved"}]}
                report = check_receipt_positions(rpc, thesis, MINT, max_pages=2, max_owners=1,
                                                checked_at="2026-10-03T12:00:00Z")
                result = report["results"][A]["result"]
                self.assertEqual(result["amounts_raw"], expected)
                self.assertEqual(sum(map(int, result["amounts_raw"].values())), 100)
                self.assertEqual(report["status"], "shadow")
                self.assertEqual(report["owners_checked"], 1)
                self.assertEqual(report["page_calls"], 2)
                self.assertEqual(report["scope"], "receipt_component_not_entire_cohort")
                self.assertFalse(report["confirmation_eligible"])
                self.assertFalse(report["affects_original_cohort_retention"])
                self.assertEqual(frozen["owners"][A]["seeds"][0]["balance_raw"], "100")
                if finalized_before:
                    self.assertIn("receipt_seed_slot_boundary_unverified", result["issues"])
                    self.assertFalse(result["coverage"]["complete"])
                else:
                    self.assertTrue(result["history_complete"])
                    self.assertTrue(result["coverage"]["complete"])
                    self.assertNotIn("receipt_seed_slot_boundary_unverified", result["issues"])

    def test_empty_history_and_missing_receipt_are_not_complete(self):
        for rows in ([], [transaction("later", 101)]):
            adapter, _ = self.adapter(rows)
            page = adapter.history_page("account", mint="mint", from_slot=100, to_slot=200, cursor=None, limit=32)
            self.assertFalse(page["coverage"]["complete"])
            self.assertNotIn("seed_boundary", page["coverage"])

    def test_later_same_slot_transfer_invalidates_receipt_slot_boundary(self):
        adapter, _ = self.adapter([transaction("seed", 100, 3), transaction("transfer", 100, 4)])
        page = adapter.history_page("account", mint="mint", from_slot=100, to_slot=200, cursor=None, limit=32)
        self.assertNotIn("seed_boundary", page["coverage"])

    def test_provider_is_pinned_and_incomplete_page_cannot_assert_boundary(self):
        adapter, rpc = self.adapter([transaction("seed", 100)], "opaque")
        page = adapter.history_page("account", mint="mint", from_slot=100, to_slot=200, cursor=None, limit=32)
        self.assertFalse(page["coverage"]["complete"])
        self.assertNotIn("seed_boundary", page["coverage"])
        adapter.remaining = 1
        rpc._route_call.return_value = ({"data": []}, "helius")
        with self.assertRaises(RuntimeError):
            adapter.history_page("account", mint="mint", from_slot=100, to_slot=200, cursor="opaque", limit=32)
        self.assertEqual(rpc._route_call.call_args.kwargs["order"], ["alchemy"])
