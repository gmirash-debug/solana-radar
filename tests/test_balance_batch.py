import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from balance_batch import STATE_KEY, prime_known_balances, remember_enumeration


def account(owner="owner", mint="mint", amount="10", decimals=0):
    return {"owner": "token-program", "executable": False, "data": {"parsed": {
        "type": "account", "info": {"owner": owner, "mint": mint,
                                    "tokenAmount": {"amount": amount, "decimals": decimals}}}}}


def enumeration(rows=None, slot=100):
    return {"context": {"slot": slot}, "value": rows if rows is not None else [
        {"pubkey": "ata", "account": account()}]}


class BalanceBatchTests(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.rpc = SimpleNamespace(call=Mock(), token_balance_cache={})
        self.assertTrue(remember_enumeration(self.state, "owner", "mint", enumeration(), 10, 1000))

    def prime(self, owners=None, now=1001, config=None):
        return prime_known_balances(self.rpc, owners or ["owner"], "mint",
                                    self.state, config or {}, now)

    def test_complete_enumeration_and_json_checkpoint_preserve_real_accounts(self):
        restored = json.loads(json.dumps(self.state))
        entry = restored[STATE_KEY]["mint"]["owner"]
        self.assertEqual(entry["accounts"], [{"pubkey": "ata", "program": "token-program", "amount_raw": "10"}])
        self.assertEqual((entry["owner"], entry["mint"], entry["slot"], entry["total"], entry["enumerated_at"]),
                         ("owner", "mint", 100, 10, 1000))

    def test_safe_nondecreasing_sum_primes_router_cache_with_context_floor(self):
        self.rpc.call.return_value = {"context": {"slot": 101}, "value": [account(amount="11")]}
        stats = self.prime()
        self.assertEqual(stats["primed"], 1)
        self.assertEqual(self.rpc.token_balance_cache[("owner", "mint")], 11)
        self.rpc.call.assert_called_once_with("getMultipleAccounts", [["ata"], {
            "encoding": "jsonParsed", "minContextSlot": 100}])
        self.assertEqual(self.state[STATE_KEY]["mint"]["owner"]["enumerated_at"], 1000)
        self.prime(now=1002)
        self.assertEqual(self.rpc.call.call_args.args[1][1]["minContextSlot"], 101)

    def test_null_drop_owner_mint_program_or_decimal_change_forces_full_enumeration(self):
        changed_program = account()
        changed_program["owner"] = "unexpected-program"
        for value in (None, account(amount="0"), account(owner="other"), account(mint="other"),
                      account(decimals=1), changed_program, {}, {"data": None}):
            with self.subTest(value=value):
                state = copy.deepcopy(self.state)
                rpc = SimpleNamespace(token_balance_cache={("owner", "mint"): 99}, call=Mock(
                    return_value={"context": {"slot": 101}, "value": [value]}))
                stats = prime_known_balances(rpc, ["owner"], "mint", state, {}, 1001)
                self.assertEqual(stats["primed"], 0)
                self.assertEqual(stats["full_enumeration_required"], ["owner"])
                self.assertNotIn(("owner", "mint"), rpc.token_balance_cache)
                self.assertTrue(state[STATE_KEY]["mint"]["owner"]["needs_enumeration"])
                self.assertEqual(state[STATE_KEY]["mint"]["owner"]["total_raw"], "10")

    def test_internal_transfer_drop_forces_enumeration_even_if_total_is_equal(self):
        rows = [{"pubkey": "old", "account": account(amount="7")},
                {"pubkey": "known-new", "account": account(amount="3")}]
        self.assertTrue(remember_enumeration(self.state, "owner", "mint", enumeration(rows), 10, 1000))
        self.rpc.call.return_value = {"context": {"slot": 101}, "value": [
            account(amount="6"), account(amount="4")]}
        self.assertEqual(self.prime()["primed"], 0)
        # Full enumeration can reveal an owner transfer into an unknown new ATA.
        rows = [{"pubkey": "old", "account": account(amount="6")},
                {"pubkey": "known-new", "account": account(amount="3")},
                {"pubkey": "fresh-ata", "account": account(amount="1")}]
        self.assertTrue(remember_enumeration(self.state, "owner", "mint", enumeration(rows, 102), 10, 1002))
        self.rpc.call.return_value = {"context": {"slot": 103}, "value": [
            row["account"] for row in rows]}
        self.assertEqual(self.prime(now=1003)["primed"], 1)
        self.assertEqual(self.rpc.token_balance_cache[("owner", "mint")], 10)

    def test_missing_stale_or_partial_batches_are_never_zero(self):
        for result in (None, {}, {"value": [account()]}, {"context": {"slot": 99}, "value": [account()]},
                       {"context": {"slot": 101}, "value": []}):
            with self.subTest(result=result):
                state = copy.deepcopy(self.state)
                self.rpc.call.return_value = result
                self.assertEqual(prime_known_balances(self.rpc, ["owner"], "mint", state, {}, 1001)["primed"], 0)
                self.assertNotIn(("owner", "mint"), self.rpc.token_balance_cache)

    def test_expiry_is_bounded_by_six_hours_and_clock_rollback_reenumerates(self):
        for now in (1000 + 21600, 999):
            state = copy.deepcopy(self.state)
            stats = prime_known_balances(self.rpc, ["owner"], "mint", state,
                                         {"balance_batch_enumeration_seconds": 86400}, now)
            self.assertEqual(stats["full_enumeration_required"], ["owner"])
        self.rpc.call.assert_not_called()

    def test_chunks_up_to_100_and_never_prime_partial_owner(self):
        rows = [{"pubkey": f"account-{i}", "account": account(amount="1")} for i in range(101)]
        self.assertTrue(remember_enumeration(self.state, "owner", "mint", enumeration(rows, 200), 101, 1000))
        self.rpc.call.side_effect = [{"context": {"slot": 201}, "value": [account(amount="1")] * 100},
                                     {"context": {"slot": 202}, "value": [None]}]
        stats = self.prime()
        self.assertEqual(stats["batch_requests"], 2)
        self.assertEqual([len(c.args[1][0]) for c in self.rpc.call.call_args_list], [100, 1])
        self.assertEqual(stats["primed"], 0)
        self.assertNotIn(("owner", "mint"), self.rpc.token_balance_cache)

    def test_independent_owners_and_largest_enumeration_slot(self):
        self.assertTrue(remember_enumeration(self.state, "other", "mint", enumeration([
            {"pubkey": "other-ata", "account": account(owner="other")}], 200), 10, 1000))
        self.rpc.call.return_value = {"context": {"slot": 201}, "value": [None, account(owner="other")]}
        stats = self.prime(["owner", "other", "other"])
        self.assertEqual(stats["primed"], 1)
        self.assertEqual(self.rpc.call.call_args.args[1][1]["minContextSlot"], 200)
        self.assertEqual(self.rpc.token_balance_cache, {("other", "mint"): 10})

    def test_failed_enumeration_is_not_persisted_as_empty_and_empty_is_not_batched(self):
        for result in (None, {}, enumeration([{"pubkey": "ata", "account": None}]),
                       enumeration([{"account": account()}])):
            self.assertFalse(remember_enumeration(self.state, "owner", "mint", result, 10, 1001))
        self.assertEqual(self.state[STATE_KEY]["mint"]["owner"]["accounts"][0]["pubkey"], "ata")
        self.assertTrue(remember_enumeration(self.state, "owner", "mint", enumeration([], 101), 0, 1002))
        self.assertEqual(self.prime(now=1003)["primed"], 0)
        self.rpc.call.assert_not_called()

    def test_request_failure_is_unknown_and_diagnostics_redacted(self):
        self.rpc.call.side_effect = RuntimeError("https://provider.invalid/secret-key")
        stats = self.prime()
        self.assertEqual(stats["failures"], 1)
        self.assertEqual(stats["primed"], 0)
        self.assertNotIn("secret", json.dumps(stats))

    def test_owner_and_account_bounds_fall_back_without_partial_priming(self):
        stats = self.prime(["owner", "other"], config={"balance_batch_max_accounts": 0})
        self.assertEqual(stats["primed"], 0)
        self.assertEqual(stats["full_enumeration_required"], ["owner", "other"])
        self.rpc.call.assert_not_called()

    def test_corrupt_persisted_baseline_never_primes_zero(self):
        entry = self.state[STATE_KEY]["mint"]["owner"]
        entry["accounts"][0]["amount_raw"] = "-10"
        entry["total_raw"] = "-10"
        self.rpc.call.return_value = {"context": {"slot": 101}, "value": [account(amount="0")]}
        self.assertEqual(self.prime()["primed"], 0)
        self.rpc.call.assert_not_called()

    def test_valid_zero_requires_a_known_zero_account_not_null(self):
        result = enumeration([{"pubkey": "ata", "account": account(amount="0")}])
        self.assertTrue(remember_enumeration(self.state, "owner", "mint", result, 0, 1000))
        self.rpc.call.return_value = {"context": {"slot": 101}, "value": [account(amount="0")]}
        self.assertEqual(self.prime()["primed"], 1)
        self.assertEqual(self.rpc.token_balance_cache[("owner", "mint")], 0)


if __name__ == "__main__":
    unittest.main()
