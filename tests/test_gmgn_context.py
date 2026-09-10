import copy
import json
import unittest
from unittest.mock import Mock, patch

import gmgn_context as gm
import scanner
from robinhood_store import Store

TOKEN = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
WALLET = "0x" + "3" * 40


def info(best=TOKEN):
    return {"address": TOKEN, "ath_price": 2, "total_supply": 1000,
            "price": {"price": "1", "buys_1h": 0},
            "dev": {"ath_token_info": {"ath_token": best, "ath_mc": "5000"}}}


class GmgnTests(unittest.TestCase):
    def test_foreign_creator_peak_is_never_current_token_ath(self):
        ath = gm.token_ath(info(OTHER), TOKEN, "robinhood")
        self.assertIsNone(ath["highest_market_cap"])
        self.assertEqual(ath["highest_price"], 2)
        self.assertEqual(ath["status"], "price_only")

    def test_matching_token_cap_and_case_sensitive_solana(self):
        self.assertEqual(gm.token_ath(info(), TOKEN, "robinhood")["highest_market_cap"], 5000)
        self.assertFalse(gm.same_token("Abcpump", "abcpump", "sol"))
        with self.assertRaises(ValueError):
            gm.token_ath(info(), OTHER, "robinhood")

    def test_no_current_supply_multiplication_for_historical_cap(self):
        data = info(None)
        self.assertIsNone(gm.token_ath(data, TOKEN, "robinhood")["highest_market_cap"])

    def test_invalid_numbers_and_missing_are_not_zero(self):
        for value in (None, "", "NaN", float("inf"), -1, True):
            self.assertIsNone(gm.number(value))
        self.assertEqual(gm.number("0"), 0)
        data = gm.normalize_info(info(), TOKEN)
        self.assertEqual(data["activity"]["1h"]["buys"], 0)
        self.assertIsNone(data["activity"]["1h"]["sells"])

    def test_holders_exclude_infrastructure_and_do_not_invent_buys(self):
        rows = [{"address": gm.POOL_MANAGER, "addr_type": 0},
                {"address": OTHER, "addr_type": 2},
                {"address": TOKEN, "addr_type": 0, "exchange": "uniswap_v3"},
                {"address": WALLET, "addr_type": 0, "balance": 20, "current_transfer_in_amount": 20, "buy_amount_cur": 0}]
        sample = gm.normalize_holders({"list": rows}, TOKEN, {gm.POOL_MANAGER})
        self.assertEqual(sample["excluded"], 3)
        self.assertEqual(len(sample["wallets"]), 1)
        self.assertFalse(sample["wallets"][0]["has_reported_buys"])
        self.assertIsNone(sample["wallets"][0]["supply_fraction"])

    def test_security_unknown_not_safe(self):
        self.assertIsNone(gm.normalize_security({"address": TOKEN}, TOKEN)["flags"]["is_honeypot"])
        with self.assertRaises(ValueError):
            gm.normalize_security({"address": OTHER}, TOKEN)

    def test_cache_reuse_does_not_touch_signal_or_balances(self):
        store = Store()
        client = Mock(enabled=True, calls=1, stopped=None)
        client.query.return_value = info()
        rows = [{"token": TOKEN, "pool": OTHER, "wallets": [], "status": "queued"}]
        original = copy.deepcopy(rows)
        gm.enrich(rows, store, client)
        gm.enrich(rows, store, client)
        self.assertEqual(client.query.call_count, 1)
        self.assertEqual({k:v for k,v in rows[0].items() if k != "gmgn"}, original[0])
        store.close()

    def test_failed_refresh_keeps_timestamp_and_marks_stale(self):
        store = Store()
        key = f"gmgn:v2:info:{TOKEN}"
        store.put(key, {"status": "ok", "checked_at": "2026-01-01T00:00:00Z", **gm.normalize_info(info(), TOKEN)})
        store.db.execute("UPDATE cache SET updated=0 WHERE key=?", (key,))
        client = Mock(enabled=True, calls=1, stopped=None)
        client.query.side_effect = gm.Unavailable("timeout")
        rows = [{"token": TOKEN, "pool": OTHER}]
        gm.enrich(rows, store, client)
        self.assertEqual(rows[0]["gmgn"]["info"]["status"], "stale")
        self.assertEqual(rows[0]["gmgn"]["info"]["checked_at"], "2026-01-01T00:00:00Z")
        store.close()

    def test_cli_budget_is_hard_and_read_only(self):
        with patch.dict("os.environ", {"GMGN_API_KEY": "test"}), patch.object(gm.subprocess, "run") as run:
            client = gm.Client(max_calls=0)
            with self.assertRaises(gm.Unavailable):
                client.query("info", TOKEN)
            with self.assertRaises(ValueError):
                client.query("swap", TOKEN)
            run.assert_not_called()

    def test_throttle_stops_requests_and_redacts(self):
        with patch.dict("os.environ", {"GMGN_API_KEY": "secret-test"}), patch.object(gm.subprocess, "run") as run:
            run.return_value = Mock(returncode=1, stderr="429 secret-test", stdout="")
            client = gm.Client()
            for _ in range(2):
                with self.assertRaisesRegex(gm.Unavailable, "^rate_limited$"):
                    client.query("info", TOKEN)
            self.assertEqual(run.call_count, 1)

    def test_cli_spaces_requests_without_exceeding_deadline(self):
        with patch.dict("os.environ", {"GMGN_API_KEY": "test"}), patch.object(gm.subprocess, "run") as run, patch.object(gm.time, "monotonic", return_value=100), patch.object(gm.time, "sleep") as sleep:
            run.return_value = Mock(returncode=0, stdout=json.dumps(info()))
            client = gm.Client()
            client.query("info", TOKEN)
            client.query("info", TOKEN)
            self.assertAlmostEqual(sleep.call_args.args[0], 1.1)
            client.deadline = 101
            with self.assertRaisesRegex(gm.Unavailable, "budget_exhausted"):
                client.query("holders", TOKEN)
            self.assertEqual(run.call_count, 2)

    def test_legacy_cache_is_quarantined_losslessly_for_other_data(self):
        state = {"market": {TOKEN: {"token_address": TOKEN, "ath_source": "gmgn", "ath_mcap_usd": 5000, "ath_checked_at": 99, "latest_mcap_usd": 1000}}}
        self.assertEqual(scanner.trusted_ath_mcap(state["market"][TOKEN]), 0)
        result = scanner.migrate_scanner_state(state)["market"][TOKEN]
        self.assertNotIn("ath_mcap_usd", result)
        self.assertEqual(result["latest_mcap_usd"], 1000)

    def test_sol_ath_identity_and_price_only_do_not_preserve_wrong_cap(self):
        with patch.object(scanner, "fetch_gmgn_raw_token_info", return_value=info(OTHER)):
            ath = scanner.fetch_gmgn_ath({}, TOKEN, include_timestamp=False)
        entry = {"token_address": TOKEN, "ath_source": "gmgn", "ath_mcap_usd": 1e9, "ath_mcap_at": "old"}
        scanner.apply_gmgn_ath(entry, ath, "2026-09-10T00:00:00Z")
        self.assertEqual(entry["ath_status"], "price_only")
        self.assertNotIn("ath_mcap_usd", entry)
        self.assertNotIn("ath_mcap_at", entry)
        self.assertEqual(scanner.trusted_ath_mcap(entry), 0)

    def test_valid_matching_cap_survives_compaction_and_export(self):
        entry = {"token_address": TOKEN}
        scanner.apply_gmgn_ath(entry, gm.token_ath(info(), TOKEN, "sol"), "2026-09-10T00:00:00Z")
        self.assertEqual(scanner.trusted_ath_mcap(entry), 5000)
        exported = scanner.apply_market_meta({"token_address": TOKEN}, {"market": {TOKEN: entry}})
        self.assertEqual(scanner.trusted_ath_mcap(exported), 5000)

    def test_window_peak_cannot_be_called_lifetime_ath_date(self):
        with patch.object(scanner, "fetch_gmgn_kline", return_value=[{"high": 1, "time": 1700000000000}]):
            self.assertEqual(scanner.fetch_gmgn_ath_timestamp({}, TOKEN, expected_price=100), 0)

    def test_rejected_new_peak_cannot_certify_legacy_cached_cap(self):
        entry = {"token_address": TOKEN, "ath_source": "gmgn", "ath_mcap_usd": 5000}
        ath = gm.token_ath(info(), TOKEN, "sol")
        ath["highest_market_cap"] = 1e20
        scanner.apply_gmgn_ath(entry, ath, "2026-09-10T00:00:00Z")
        self.assertEqual(scanner.trusted_ath_mcap(entry), 0)

    def test_mismatched_application_is_not_trusted(self):
        entry = {"token_address": OTHER}
        self.assertFalse(scanner.apply_gmgn_ath(entry, gm.token_ath(info(), TOKEN, "sol"), "2026-09-10T00:00:00Z"))
        self.assertEqual(scanner.trusted_ath_mcap(entry), 0)


if __name__ == "__main__":
    unittest.main()
