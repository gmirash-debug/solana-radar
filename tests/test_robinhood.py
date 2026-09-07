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


class RobinhoodTests(unittest.TestCase):
    def test_chain_scoped_key_and_address_validation(self):
        self.assertEqual(rh.token_key(TOKEN.upper().replace("0X", "0x")), f"4663:{TOKEN}")
        with self.assertRaises(ValueError):
            rh.token_key("SolanaMintpump")

    def test_direct_buy(self):
        self.assertEqual(rh.attributed_buy(fixture(), {"pool": POOL, "token": TOKEN}, 0), (WALLET, 100))

    def test_router_not_treated_as_wallet(self):
        self.assertIsNone(rh.attributed_buy(fixture(ROUTER), {"pool": POOL, "token": TOKEN}, 0))

    def test_fee_on_transfer_not_full_buy(self):
        self.assertIsNone(rh.attributed_buy(fixture(received=90), {"pool": POOL, "token": TOKEN}, 0))

    def test_failed_transaction(self):
        self.assertIsNone(rh.attributed_buy(fixture(status="0x0"), {"pool": POOL, "token": TOKEN}, 0))

    def test_transfer_alone_is_not_buy(self):
        receipt = fixture()
        receipt["logs"] = receipt["logs"][1:]
        self.assertIsNone(rh.attributed_buy(receipt, {"pool": POOL, "token": TOKEN}, 0))

    def test_buy_and_forward_excluded(self):
        receipt = fixture()
        receipt["logs"].append({"address": TOKEN, "topics": [rh.TRANSFER, topic(WALLET), topic(ROUTER)],
            "data": "0x" + encode(["uint256"], [100]).hex()})
        self.assertIsNone(rh.attributed_buy(receipt, {"pool": POOL, "token": TOKEN}, 0))

    def test_multi_swap_ambiguous(self):
        receipt = fixture()
        receipt["logs"].append(receipt["logs"][0])
        self.assertIsNone(rh.attributed_buy(receipt, {"pool": POOL, "token": TOKEN}, 0))

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
                rh.inspect_pool(rpc, {"pool": POOL, "token": TOKEN}, 1, 100, rh.CONFIG)

    def test_publication_does_not_roll_back_or_mix_network(self):
        old = {"chain_id":4663,"tokens":[],"attempted_at":"2026-09-06T00:00:00Z"}
        new = dict(old, attempted_at="2026-09-07T00:00:00Z")
        self.assertIs(choose_robinhood(old, new), new)
        self.assertIsNone(choose_robinhood(dict(old, chain_id=1), None))

    def test_same_block_balance_and_retention_bound(self):
        receipt = fixture()
        swap = dict(receipt["logs"][0], blockNumber="0x64", transactionHash="tx", logIndex="0x1")
        rpc = Mock()
        rpc.contract.side_effect = [(6,), (1000,), (30,)]
        rpc.call.side_effect = [[swap, swap], receipt]
        with patch.object(rh, "verify_pool", return_value=0):
            result = rh.inspect_pool(rpc, {"pool": POOL, "token": TOKEN}, 1, 100, rh.CONFIG)
        self.assertEqual(result["retained_supply_upper_bound_pct"], 3)
        self.assertEqual(result["wallets"][0]["retention_upper_bound_pct"], 30)
        self.assertEqual(result["swap_transactions"], 1)
        self.assertTrue(result["history_complete"])
        self.assertEqual(result["status"], "observed")
        self.assertEqual(rpc.contract.call_args.args[-1], "0x64")

    def test_malformed_swap_is_not_silent_no_buy(self):
        with self.assertRaisesRegex(rh.RpcError, "Invalid swap"):
            rh.swap_amounts({"data":"0x01"})


if __name__ == "__main__":
    unittest.main()
