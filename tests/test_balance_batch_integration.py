import unittest
from unittest.mock import Mock

import scanner
from balance_batch import STATE_KEY, prime_known_balances


def account(amount="10"):
    return {"owner": "token-program", "data": {"parsed": {"type": "account", "info": {
        "owner": "owner", "mint": "mint", "tokenAmount": {"amount": amount, "decimals": 0}}}}}


def enumeration(rows=None, slot=100):
    return {"context": {"slot": slot}, "value": rows if rows is not None else [
        {"pubkey": "old-account", "account": account()}]}


class BalanceBatchIntegrationTests(unittest.TestCase):
    def test_router_remembers_all_accounts_then_batch_avoids_owner_request(self):
        rpc = scanner.RoutedSolanaRpc([])
        rpc.token_account_state = {}
        rpc._route_call = Mock(return_value=(enumeration(), "helius"))
        self.assertEqual(rpc.token_balance("owner", "mint"), 10)
        remembered = rpc.token_account_state[STATE_KEY]["mint"]["owner"]
        rpc.call = Mock(return_value={"context": {"slot": 101}, "value": [account()]})
        stats = prime_known_balances(rpc, ["owner"], "mint", rpc.token_account_state, {}, remembered["enumerated_at"] + 1)
        self.assertEqual(stats["primed"], 1)
        self.assertEqual(rpc.token_balance("owner", "mint"), 10)
        self.assertEqual(rpc._route_call.call_count, 1)

    def test_closed_account_forces_complete_enumeration_including_new_account(self):
        rpc = scanner.RoutedSolanaRpc([])
        rpc.token_account_state = {}
        rpc._route_call = Mock(return_value=(enumeration(), "helius"))
        rpc.token_balance("owner", "mint")
        remembered = rpc.token_account_state[STATE_KEY]["mint"]["owner"]
        rpc.call = Mock(return_value={"context": {"slot": 101}, "value": [None]})
        prime_known_balances(rpc, ["owner"], "mint", rpc.token_account_state, {}, remembered["enumerated_at"] + 1)
        rpc._route_call.return_value = (enumeration([
            {"pubkey": "new-account", "account": account()}], slot=102), "helius")
        self.assertEqual(rpc.token_balance("owner", "mint"), 10)
        self.assertEqual(rpc._route_call.call_count, 2)
        self.assertEqual(rpc.token_account_state[STATE_KEY]["mint"]["owner"]["accounts"][0]["pubkey"], "new-account")

    def test_failed_fallback_never_becomes_a_zero_balance(self):
        rpc = scanner.RoutedSolanaRpc([])
        rpc.token_account_state = {}
        rpc._route_call = Mock(return_value=(enumeration(), "helius"))
        rpc.token_balance("owner", "mint")
        remembered = rpc.token_account_state[STATE_KEY]["mint"]["owner"]
        rpc.call = Mock(side_effect=RuntimeError("batch unavailable"))
        prime_known_balances(rpc, ["owner"], "mint", rpc.token_account_state, {}, remembered["enumerated_at"] + 1)
        rpc._route_call.side_effect = RuntimeError("owner unavailable")
        with self.assertRaisesRegex(RuntimeError, "owner unavailable"):
            rpc.token_balance("owner", "mint")
        self.assertNotIn(("owner", "mint"), rpc.token_balance_cache)
