import copy
import time
import unittest
from unittest.mock import Mock
from lifi_context import completed_output, LifiClient
from robinhood_store import Store
import robinhood as rh
from tests.test_relay_context import relay_fixture
from tests.test_robinhood import WALLET, TOKEN, POOL


def status():
    return {"status":"DONE","substatus":"COMPLETED","fromAddress":"origin","toAddress":WALLET,
        "sending":{"txHash":"source","chainId":1},
        "receiving":{"txHash":"0xabc","chainId":4663,"amount":"100","token":{"address":TOKEN,"chainId":4663}}}


class LifiTests(unittest.TestCase):
    def test_executed_output_must_reconcile_with_destination_receipt(self):
        receipt, _ = relay_fixture()
        match = completed_output(status(),"0xabc",TOKEN,4663)
        self.assertEqual(match["service"],"LI.FI")
        self.assertEqual(rh.verified_route_buy(receipt,{"pool":POOL,"token":TOKEN},0,match),match)
        self.assertIsNone(rh.verified_route_buy(receipt,{"pool":POOL,"token":TOKEN},0,dict(match,bought_raw="101")))

    def test_refund_partial_same_chain_and_wrong_identity_are_not_buys(self):
        for mutate in [lambda p:p.update(status="PENDING"),lambda p:p.update(substatus="REFUNDED"),
                lambda p:p.update(substatus="PARTIAL"),lambda p:p["receiving"].update(txHash="wrong"),
                lambda p:p["receiving"].update(chainId=1),lambda p:p["receiving"].update(amount="0"),
                lambda p:p["receiving"]["token"].update(address=WALLET),lambda p:p["sending"].update(chainId=4663)]:
            p = status()
            mutate(p)
            self.assertIsNone(completed_output(p,"0xabc",TOKEN,4663))
        self.assertIsNone(completed_output({},"0xabc",TOKEN,4663))

    def test_client_positive_cache_and_hard_lookup_budget(self):
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = status()
        client = LifiClient(session,Store(),time.monotonic()+100,max_calls=1)
        self.assertIsNotNone(client.lookup("0xabc",TOKEN,4663))
        self.assertIsNotNone(client.lookup("0xabc",TOKEN,4663))
        self.assertIsNone(client.lookup("0xdef",TOKEN,4663))
        self.assertEqual(client.calls,1)

    def test_404_is_not_persisted_and_429_stops_requests(self):
        session, store = Mock(), Store()
        session.get.return_value.status_code = 404
        client = LifiClient(session,store,time.monotonic()+100)
        self.assertIsNone(client.lookup("0xabc",TOKEN,4663))
        self.assertIsNone(store.get(f"lifi:4663:{TOKEN}:0xabc"))
        session.get.return_value.status_code = 429
        client.next_at = 0
        self.assertIsNone(client.lookup("0xabc",TOKEN,4663))
        self.assertIsNone(client.lookup("0xabc",TOKEN,4663))
        self.assertEqual(client.calls,2)


if __name__ == "__main__":
    unittest.main()
