import copy
import os
import time
import unittest
from unittest.mock import Mock, patch
from eth_abi import encode

import robinhood as rh
from relay_context import RelayClient, buy_wave, request_output, SOLVER
from robinhood_store import Store
from tests.test_robinhood import TOKEN, QUOTE, POOL, WALLET, ROUTER, fixture, topic


def relay_fixture():
    receipt = fixture()
    receipt.update(transactionHash="0xabc", blockHash="hash", **{"from": ROUTER})
    receipt["logs"].append({"address": QUOTE, "topics": [rh.TRANSFER, topic(SOLVER), topic(POOL)], "data": hex(5)})
    request = {"id": "request", "status": "success", "user": "SolanaSourceCaseSensitive", "recipient": WALLET,
        "data": {"inTxs": [{"chainId": 792703809, "hash": "source", "status": "success"}],
            "outTxs": [{"chainId": 4663, "hash": "0xabc", "status": "success"}],
            "metadata": {"currencyOut": {"currency": {"chainId": 4663, "address": TOKEN}, "amount": "100"}}}}
    return receipt, request


def events(n=5, amount=200, step=40):
    return [{"transaction": f"tx{i}", "recipient": f"0x{i+20:040x}", "bought_raw": str(amount),
        "timestamp": 1000 + i * step, "block": 100 + i} for i in range(n)]


class RelayTests(unittest.TestCase):
    def test_verified_cross_chain_final_recipient_not_submitter(self):
        receipt, request = relay_fixture()
        relay = Mock()
        relay.lookup.return_value = [request]
        self.assertIsNone(rh.routed_buy(receipt, {"pool": POOL, "token": TOKEN}, 0))
        result = rh.relay_buy(receipt, {"pool": POOL, "token": TOKEN}, 0, relay)
        self.assertEqual(result["recipient"], WALLET)
        self.assertEqual(result["source_address"], "SolanaSourceCaseSensitive")

    def test_refund_wrong_chain_token_hash_and_ambiguous_order_rejected(self):
        _, request = relay_fixture()
        for mutate in [lambda r: r.update(status="refund"),
                       lambda r: r["data"]["outTxs"][0].update(chainId=1),
                       lambda r: r["data"]["outTxs"][0].update(hash="other"),
                       lambda r: r["data"]["metadata"]["currencyOut"]["currency"].update(address=QUOTE)]:
            r = copy.deepcopy(request)
            mutate(r)
            self.assertIsNone(request_output([r], "0xabc", TOKEN, 4663))
        self.assertIsNone(request_output([request, request], "0xabc", TOKEN, 4663))

    def test_v3_actual_only_never_quoted(self):
        _, r = relay_fixture()
        out = r["data"].pop("metadata")["currencyOut"]
        r["data"]["route"] = {"actual": {"destination": {"outputCurrency": out}}}
        for t in r["data"]["inTxs"] + r["data"]["outTxs"]:
            t["txHash"] = t.pop("hash")
        self.assertIsNotNone(request_output([r], "0xabc", TOKEN, 4663))
        r["data"]["route"]["quoted"] = r["data"]["route"].pop("actual")
        self.assertIsNone(request_output([r], "0xabc", TOKEN, 4663))

    def test_fee_adjusted_output_must_reconcile(self):
        receipt, request = relay_fixture()
        receipt["logs"][1]["data"] = "0x" + encode(["uint256"], [90]).hex()
        receipt["logs"].append({"address": TOKEN, "topics": [rh.TRANSFER, topic(POOL), topic(TOKEN)], "data": "0x" + encode(["uint256"], [10]).hex()})
        relay = Mock()
        relay.lookup.return_value = [request]
        p = {"pool": POOL, "token": TOKEN}
        self.assertIsNone(rh.relay_buy(receipt, p, 0, relay))
        request["data"]["metadata"]["currencyOut"]["amount"] = "90"
        self.assertEqual(rh.relay_buy(receipt, p, 0, relay)["bought_raw"], "90")
        receipt["logs"] = receipt["logs"][1:]
        self.assertIsNone(rh.relay_buy(receipt, p, 0, relay))

    def test_5m_wave_dedup_and_baseline_unknown(self):
        e = events()
        wave = buy_wave(e + e, 100000, rh.CONFIG)
        self.assertEqual((wave["buyers"], wave["buy_transactions"], wave["gross_bought_supply_pct"]), (5, 5, 1))
        self.assertEqual(wave["baseline_status"], "not_established")
        self.assertIsNone(buy_wave(e[:4], 100000, rh.CONFIG))

    def test_whale_dust_and_slow_flow_not_wave(self):
        e = events()
        e[0]["bought_raw"] = "100000"
        self.assertIsNone(buy_wave(e, 1000000, rh.CONFIG))
        for x in e[1:]:
            x["bought_raw"] = "1"
        self.assertIsNone(buy_wave(e, 1000000, rh.CONFIG))
        self.assertIsNone(buy_wave(events(step=1000), 100000, rh.CONFIG))

    def test_15m_and_hour_windows(self):
        self.assertEqual(buy_wave(events(8, 250, 100), 100000, rh.CONFIG)["window_seconds"], 900)
        self.assertEqual(buy_wave(events(15, 350, 200), 100000, rh.CONFIG)["window_seconds"], 3600)

    def test_client_rate_limit_hard_budget_cache_and_secret_not_exported(self):
        _, r = relay_fixture()
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"requests": [r]}
        with patch.dict(os.environ, {"RELAY_API_KEY": "test-secret"}):
            client = RelayClient(session, Store(), time.monotonic() + 100, max_calls=1)
        self.assertEqual(client.lookup("0xabc"), [r])
        self.assertEqual(client.lookup("0xabc"), [r])
        self.assertIsNone(client.lookup("0xdef"))
        self.assertEqual(client.calls, 1)
        self.assertNotIn("test-secret", str(client.summary()))
        self.assertEqual(session.get.call_args.kwargs["params"]["fillTxHash"], "0xabc")
        session.get.return_value.status_code = 429
        client = RelayClient(session, Store(), time.monotonic() + 100)
        self.assertIsNone(client.lookup("0x123"))
        self.assertIsNone(client.lookup("0x456"))
        self.assertEqual(client.calls, 1)

    def test_partial_verified_wave_freezes_and_rechecks_original_buyers(self):
        store = Store()
        p = {"pool": POOL, "token": TOKEN, "quote": QUOTE, "key": rh.token_key(TOKEN)}
        swaps, receipts, matches = [], {}, {}
        for i, e in enumerate(events(amount=200)):
            r, req = relay_fixture()
            tx = e["transaction"]
            r.update(transactionHash=tx, blockNumber=hex(e["block"]), blockHash=f"hash{e['block']}")
            r["logs"][1]["topics"][2] = topic(e["recipient"])
            receipts[tx] = r
            matches[tx] = dict(e, bought_raw="200")
            swaps.append(dict(r["logs"][0], transactionHash=tx, blockNumber=r["blockNumber"], blockHash=r["blockHash"], logIndex="0x1"))
        extra = dict(swaps[-1], transactionHash="unresolved", blockNumber=hex(106), blockHash="hash106")
        swaps.append(extra)
        receipts["unresolved"] = dict(receipts["tx4"], transactionHash="unresolved", blockNumber=hex(106), blockHash="hash106")
        rpc = Mock()
        rpc.call.side_effect = lambda method, params: receipts[params[0]] if method == "eth_getTransactionReceipt" else {
            "hash": f"hash{int(params[0],16)}", "timestamp": hex(1000 + (int(params[0],16)-100)*40)}
        balance = [200]
        rpc.contract.side_effect = lambda target, sig, *a, **kw: (6,) if sig == "decimals()" else (100000,) if sig == "totalSupply()" else (balance[0],)
        with patch.object(rh, "verify_pool", return_value=0), patch.object(rh, "security_check", return_value={"status":"no_flags", "flags":[]}), patch.object(rh, "relay_buy", side_effect=lambda r,*a,**kw: matches.get(r["transactionHash"])), patch.object(rh, "position_check", return_value={"status":"pending"}), patch.object(rh, "preparation_check", return_value={"status":"not_checked"}):
            with patch.object(rh, "get_logs", side_effect=[swaps, []]):
                row = rh.inspect_incremental(rpc, p, 100, 200, rh.CONFIG, store, Mock(), 2000, relay=Mock())
            self.assertFalse(row["attribution_complete"])
            self.assertEqual(row["cohort_reason"], "Relay buy wave")
            self.assertEqual(row["status"], "buy_wave")
            self.assertEqual(row["retained_supply_lower_bound_pct"], 1)
            self.assertEqual(row["relay"]["coverage"], "partial")
            outgoing = [{"topics":[rh.TRANSFER,topic(e["recipient"]),topic(ROUTER)],"data":hex(190)} for e in events()]
            balance[0] = 10
            with patch.object(rh, "get_logs", side_effect=[[], outgoing]):
                next_row = rh.inspect_incremental(rpc, p, 250, 300, rh.CONFIG, store, Mock(), 4000)
            self.assertEqual(next_row["status"], "reduced")
            self.assertEqual(next_row["relay"]["signal_wave"], row["relay"]["signal_wave"])

    def test_zero_age_uses_head_and_accepts_new_deployment(self):
        self.assertEqual(rh.CONFIG["min_pool_age_hours"], 0)
        rpc = Mock()
        rpc.call.side_effect = lambda m, p: ("0x6000" if int(p[1],16) >= 99 else "0x") if m == "eth_getCode" else {"timestamp":"0x64"}
        age = rh.token_age(rpc, TOKEN, 100, 20, 100, Store())
        self.assertEqual(age["token_creation_block"], 99)
        self.assertEqual(age["age_status"], "eligible")

    def test_new_pool_feed_accepts_only_supported_protocols_and_zero_age(self):
        pool = {"attributes": {"address": POOL, "pool_created_at":"2026-09-11T00:00:00Z", "fdv_usd":"20000", "reserve_in_usd":"5000", "name":"TEST / Q"},
            "relationships": {"dex":{"data":{"id":"uniswap-v3-robinhood"}},
                "base_token":{"data":{"id":"robinhood_"+TOKEN}}, "quote_token":{"data":{"id":"robinhood_"+QUOTE}}}}
        unsupported = copy.deepcopy(pool)
        unsupported["relationships"]["dex"]["data"]["id"] = "other-dex"
        session = Mock()
        session.get.return_value.json.return_value = {"data":[pool, unsupported]}
        with patch.object(rh.time, "sleep"):
            pools, errors = rh.discover(session, dict(rh.CONFIG, new_pool_pages=1, discovery_pages=0), rh.timestamp("2026-09-11T00:00:00Z"))
        self.assertFalse(errors)
        self.assertEqual(len(pools), 1)
        self.assertTrue(pools[0]["eligible"])
        self.assertIn("/new_pools", session.get.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
