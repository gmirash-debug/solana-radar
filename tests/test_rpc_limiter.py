import unittest

from rpc_limiter import (NativeUnitLimiter, ProviderLimiters, RateLimitDeadline,
                         gmgn_weight, wait_for_gmgn_slot, wait_for_provider_slot)


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class RpcLimiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiters = ProviderLimiters(clock=self.clock, sleep=self.clock.sleep)

    def test_alchemy_native_weights_share_all_methods_and_chains(self):
        wait_for_provider_slot("alchemy", "getTransactionsForAddress", limiters=self.limiters)
        self.limiters.acquire("alchemy", "eth_getTransactionReceipt")
        self.assertAlmostEqual(self.clock.waits[-1], 100 / 300)
        self.limiters.acquire("alchemy", "eth_getLogs")
        self.assertAlmostEqual(self.clock.waits[-1], 20 / 300)
        self.limiters.acquire("alchemy", "getAccountInfo")
        self.assertAlmostEqual(self.clock.waits[-1], 60 / 300)

    def test_zero_billing_probe_still_uses_throughput(self):
        self.limiters.acquire("alchemy", "eth_chainId")
        self.limiters.acquire("alchemy", "eth_chainId")
        self.assertAlmostEqual(self.clock.waits[-1], 5 / 300)

    def test_helius_standard_and_enhanced_do_not_share_fixed_sleep(self):
        self.limiters.acquire("helius", "getAsset")
        self.limiters.acquire("helius", "getTransaction")
        self.assertEqual(self.clock.waits, [])
        self.limiters.acquire("helius", "getSignaturesForAddress")
        self.assertAlmostEqual(self.clock.waits[-1], 0.1)
        self.limiters.acquire("helius", "getAsset")
        self.assertAlmostEqual(self.clock.waits[-1], 0.4)

    def test_helius_indexed_lane_uses_half_second_not_billing_credit_weight(self):
        self.limiters.acquire("helius", "getTransactionsForAddress")
        self.limiters.acquire("helius", "getTransactionsForAddress")
        self.assertAlmostEqual(self.clock.waits[-1], 0.5)

    def test_chainstack_rps_not_archive_ru_weight(self):
        self.limiters.acquire("chainstack", "getTransaction")
        self.limiters.acquire("chainstack", "getMultipleAccounts")
        self.assertAlmostEqual(self.clock.waits[-1], 1 / 25)

    def test_exact_deadline_and_oversleep_do_not_consume_slot(self):
        bucket = NativeUnitLimiter(10, clock=self.clock, sleep=self.clock.sleep)
        bucket.acquire()
        with self.assertRaises(RateLimitDeadline):
            bucket.acquire(deadline=100.1)
        self.assertEqual(self.clock.waits, [])
        self.assertAlmostEqual(bucket.next_at, 100.1)
        bucket.sleep = lambda seconds: self.clock.sleep(seconds + 1)
        with self.assertRaises(RateLimitDeadline):
            bucket.acquire(deadline=100.5)
        self.assertAlmostEqual(bucket.next_at, 100.1)

    def test_gmgn_weighted_native_leakage_and_shared_command_bucket(self):
        bucket = NativeUnitLimiter(5, capacity=5, clock=self.clock, sleep=self.clock.sleep)
        wait_for_gmgn_slot(["market", "trending"], limiter=bucket)
        wait_for_gmgn_slot(["market", "trenches"], limiter=bucket)
        wait_for_gmgn_slot(["token", "holders"], limiter=bucket)
        self.assertEqual(self.clock.waits, [1.0])
        wait_for_gmgn_slot(["token", "info"], limiter=bucket)
        self.assertAlmostEqual(self.clock.waits[-1], 0.2)

    def test_gmgn_capacity_deadline_unknown_and_pro_only_commands(self):
        bucket = NativeUnitLimiter(5, capacity=5, clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(gmgn_weight(["token", "security"]), 1)
        self.assertEqual(gmgn_weight(["market", "kline", "--resolution", "1m"]), 2)
        for args in (["swap", "buy"], ["token", "unpriced"],
                     ["market", "kline", "--resolution=1s"]):
            with self.assertRaises(ValueError):
                gmgn_weight(args)
        bucket.acquire(5)
        with self.assertRaises(RateLimitDeadline):
            bucket.acquire(5, deadline=101)
        self.assertEqual(bucket.level, 5)
        with self.assertRaises(ValueError):
            bucket.acquire(6)


if __name__ == "__main__":
    unittest.main()
