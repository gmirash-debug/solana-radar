import copy
import unittest
from unittest.mock import Mock, patch

import robinhood as rh
from position_evidence import seed_position, replay_position, position_summary, cross_chain_summary, preparation_summary
from robinhood_store import Store
from tests.test_robinhood import TOKEN, QUOTE, POOL, WALLET, ROUTER, fixture, topic

B = "0x" + "b" * 40
C = "0x" + "c" * 40


def seed(balance=100, known=100):
    return seed_position([{"address": WALLET, "balance_raw": str(balance), "bought_raw": "100", "retained_lower_bound_raw": str(known)}], 10, "h10", 1000)


def event(sender=WALLET, recipient=B, amount=40, index=0):
    return dict(sender=sender, recipient=recipient, amount=amount, tx="tx", index=index, block=11)


class PositionTests(unittest.TestCase):
    def test_transfer_not_sale_and_no_double_count(self):
        s = seed()
        nodes = dict(s["nodes"], **{B: {"balance":"0", "known":"0", "depth":1}})
        e = event()
        out = replay_position(s, [e, e], nodes, {WALLET:60, B:40}, set(), 11, "h11")
        self.assertEqual(position_summary(out)["amounts_raw"], {"original":"60", "transferred":"40", "sold":"0", "unknown":"0"})
        self.assertEqual(s["block"], 10)

    def test_mixing_uses_guaranteed_not_assumed_provenance(self):
        s = seed(balance=150)
        nodes = dict(s["nodes"], **{B: {"balance":"0", "known":"0", "depth":1}})
        out = replay_position(s, [event(amount=70)], nodes, {WALLET:80, B:70}, set(), 11, "h11")
        self.assertEqual(position_summary(out)["amounts_raw"], {"original":"30", "transferred":"20", "sold":"0", "unknown":"50"})

    def test_second_hop_return_and_confirmed_sale(self):
        s = seed()
        nodes = dict(s["nodes"], **{B: {"balance":"0", "known":"0", "depth":1}, C: {"balance":"0", "known":"0", "depth":2}})
        events = [event(amount=100), event(B,C,60,1), event(C,WALLET,20,2), event(B,POOL,40,3)]
        out = replay_position(s, events, nodes, {WALLET:20, B:0, C:40}, {("tx",3)}, 11, "h11")
        self.assertEqual(position_summary(out)["amounts_raw"], {"original":"20", "transferred":"40", "sold":"40", "unknown":"0"})

    def test_unknown_destination_and_initial_unresolved(self):
        s = seed(80,60)
        out = replay_position(s, [event(amount=80)], s["nodes"], {WALLET:0}, set(), 11,"h11")
        self.assertEqual(position_summary(out)["amounts_raw"]["unknown"], "100")
        self.assertEqual(position_summary(out)["amounts_raw"]["sold"], "0")

    def test_balance_mismatch_and_overspend_fail_closed(self):
        s = seed()
        for amount, end in [(40,61),(101,0)]:
            with self.assertRaises(ValueError):
                replay_position(s,[event(amount=amount)],s["nodes"],{WALLET:end},set(),11,"h11")

    def test_topup_does_not_restore_provenance(self):
        s = seed()
        out = replay_position(s, [event(recipient=POOL,amount=100),event(ROUTER,WALLET,100,1)],s["nodes"],{WALLET:100},{("tx",0)},11,"h11")
        self.assertEqual(position_summary(out)["amounts_raw"], {"original":"0","transferred":"0","sold":"100","unknown":"0"})

    def test_position_rpc_checkpoint_and_atomic_failure(self):
        store = Store()
        state = seed()
        store.put("position:"+rh.token_key(TOKEN), state)
        rpc = Mock()
        rpc.call.side_effect = lambda m,p: {"hash": "h"+str(int(p[0],16))} if m == "eth_getBlockByNumber" else "0x"
        rpc.contract.return_value = (100,)
        p = {"token":TOKEN,"quote":QUOTE,"pool":POOL,"key":rh.token_key(TOKEN)}
        with patch.object(rh,"get_logs",return_value=[]):
            out = rh.position_check(rpc,p,{},11,1000,store,0)
        self.assertEqual(out["checked_block"],11)
        rpc.contract.return_value = (99,)
        with patch.object(rh,"get_logs",return_value=[]), self.assertRaises(ValueError):
            rh.position_check(rpc,p,{},12,1000,store,0)
        self.assertEqual(store.get("position:"+p["key"])["block"],11)

    def test_supply_change_invalidates_trace(self):
        store = Store()
        store.put("position:"+rh.token_key(TOKEN),seed())
        p = {"token":TOKEN,"quote":QUOTE,"pool":POOL,"key":rh.token_key(TOKEN)}
        self.assertEqual(rh.position_check(Mock(),p,{},11,2000,store,0)["status"],"unavailable")

    def test_large_cohort_is_bounded_without_losing_denominator(self):
        wallets = [{"address":f"0x{i:040x}","balance_raw":"10","retained_lower_bound_raw":"10","bought_raw":"10"} for i in range(40)]
        rpc = Mock()
        rpc.call.return_value = {"hash":"h11"}
        store = Store()
        p = {"token":TOKEN,"quote":QUOTE,"pool":POOL,"key":rh.token_key(TOKEN)}
        out = rh.position_check(rpc,p,{"wallets":wallets},11,1000,store,0)
        self.assertEqual(out["amounts_raw"]["original"],"120")
        self.assertEqual(out["amounts_raw"]["unknown"],"280")
        self.assertEqual(len(store.get("position:"+p["key"])["nodes"]),12)

    def test_router_beneficiary_must_reconcile_and_be_eoa(self):
        r = fixture()
        r.update(blockNumber="0xb", **{"from": ROUTER})
        rpc = Mock()
        rpc.call.return_value = "0x"
        self.assertEqual(rh.beneficiary_buy(r,{"pool":POOL,"token":TOKEN},0,rpc), (WALLET,100))
        rpc.call.return_value = "0x6000"
        self.assertIsNone(rh.beneficiary_buy(r,{"pool":POOL,"token":TOKEN},0,rpc))

    def test_cross_chain_excludes_same_chain_and_deduplicates(self):
        e = {"transaction":"tx", "recipient":WALLET,"bought_raw":"100","source_chain_id":1,"source_address":B}
        s = cross_chain_summary([e,e,dict(e,transaction="same",source_chain_id=4663)],4663,1000,"partial")
        self.assertEqual((s["verified_buys"],s["gross_bought_supply_pct"]),(1,10))
        self.assertEqual(s["baseline_status"],"not_established")

    def test_preparation_requires_funding_before_three_timed_buys(self):
        buys = [{"recipient":w,"block":20+i,"timestamp":2000+i} for i,w in enumerate([WALLET,B,C])]
        funds = [dict(event(ROUTER,w,50,i),block=12+i,timestamp=1900+i) for i,w in enumerate([WALLET,B,C])]
        s = preparation_summary(buys,funds,set())
        self.assertEqual(s["groups"][0]["wallet_count"],3)
        self.assertEqual(s["ownership"],"not_established")
        self.assertFalse(preparation_summary(buys,funds,{ROUTER})["groups"])
        self.assertFalse(preparation_summary(buys,funds[:2],set())["groups"])
        self.assertFalse(preparation_summary(buys,[dict(f,block=100) for f in funds],set())["groups"])

    def test_preparation_is_not_common_service_ownership(self):
        buys = [{"recipient":w,"block":20+i,"timestamp":2000+i*1000} for i,w in enumerate([WALLET,B,C])]
        funds = [dict(event(ROUTER,w,50,i),block=12+i,timestamp=1900+i) for i,w in enumerate([WALLET,B,C])]
        self.assertEqual(preparation_summary(buys,funds,set())["status"],"no_match_in_checked_subset")


if __name__ == "__main__":
    unittest.main()
