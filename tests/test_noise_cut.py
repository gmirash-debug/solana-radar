import copy
import unittest
from unittest.mock import patch

import gmgn_context as gm
import noise_cut
import scanner as s

NOW = s.parse_timestamp("2026-10-15T12:00:00Z")
TOKEN = "So11111111111111111111111111111111111111112"
# Shape of a real Solana `gmgn-cli token info --chain sol` payload (values are strings).
SOLANA_INFO = {
    "address": TOKEN,
    "holder_count": 1682,
    "stat": {"holder_count": 1682, "top_10_holder_rate": "0.62", "dev_team_hold_rate": "0",
             "fresh_wallet_rate": "0.0359", "top70_sniper_hold_rate": "0.25"},
    "wallet_tags_stat": {"smart_wallets": 117, "fresh_wallets": 1000, "sniper_wallets": 3,
                         "bundler_wallets": 894, "renowned_wallets": 6},
}


def reactivation_config(**overrides):
    config = s.apply_lane(s.load_json(s.DEFAULT_CONFIG_PATH, {}), "reactivation")
    config.update(overrides)
    return config


def funded_fresh_buys(owners=("a", "b", "c"), sol=3):
    return [{"signer": owner, "token_recipient": owner, "kind": "buy", "wallet_class": "fresh",
             "sol_amount": sol, "token_amount": 1_000, "block_time": NOW - 120 + index,
             "signature": f"sig-{owner}", "funding_source": "funder", "funding_verified": True,
             "funding_sol": 2} for index, owner in enumerate(owners)]


class CoordinationAsRiskTests(unittest.TestCase):
    def test_flag_off_keeps_legacy_coordination_bonuses(self):
        score, _events, funders, _recipients, evidence = s.score_events(funded_fresh_buys(), reactivation_config())
        self.assertEqual(len(funders), 1)
        self.assertEqual(score, 50)  # hard wallet cluster 25 + common funder 25
        self.assertEqual(evidence["evidence_score"], 50)
        self.assertEqual(evidence["coordination_risk"], [])

    def test_common_funder_and_hard_cluster_become_penalties(self):
        config = reactivation_config(noise_cut_coordination_as_risk=True)
        score, _events, funders, _recipients, evidence = s.score_events(funded_fresh_buys(), config)
        self.assertEqual(len(funders), 1)
        self.assertEqual(score, 0)
        self.assertEqual(evidence["evidence_score"], 50)
        self.assertEqual({item["code"] for item in evidence["coordination_risk"]},
                         {"hard_wallet_cluster", "common_funder"})

    def test_dormant_flow_is_still_scored_but_linked_wallets_subtract(self):
        events = funded_fresh_buys(sol=8)
        events[0]["wallet_class"] = "dormant"
        legacy = s.score_events(events, reactivation_config())[0]
        flipped = s.score_events(events, reactivation_config(noise_cut_coordination_as_risk=True))[0]
        self.assertEqual(legacy, 100)
        self.assertEqual(flipped, 35 + 25 - 25 - 25)

    def tier(self, alert, evidence, **flags):
        pool = s.Pool(pool_address="pool", token_address="token", mcap_usd=50_000, volume_1h_usd=1_000)
        pool.ath_current_ratio = 0.1
        return s.classify_alert_tier(pool, alert, evidence, reactivation_config(**flags))

    def test_linked_hard_cluster_moves_from_reasons_to_penalties(self):
        alert = {"common_funders": [{"source": "funder", "wallets": 3}]}
        evidence = {"hard_wallets": 3, "hard_sol": 9, "hard_classes": {"fresh": 3}}
        tier, reasons, penalties, _ = self.tier(alert, evidence)
        self.assertIn("linked wallets", reasons)
        self.assertIn("hard wallet cluster", reasons)
        self.assertNotEqual(tier, "noise")
        tier, reasons, penalties, _ = self.tier(alert, evidence, noise_cut_coordination_as_risk=True)
        self.assertNotIn("linked wallets", reasons)
        self.assertNotIn("hard wallet cluster", reasons)
        self.assertIn("linked wallets (risk)", penalties)
        self.assertIn("hard wallet cluster (risk)", penalties)
        self.assertEqual(tier, "noise")

    def test_coordination_cannot_make_classic_alert_actionable(self):
        alert = {"common_funders": [{"source": "funder", "wallets": 3}],
                 "reactivation_baseline": {"status": "ready", "reactivation_confirmed": True}}
        evidence = {"hard_wallets": 3, "hard_sol": 30, "hard_classes": {"dormant": 1, "fresh": 2}}
        self.assertEqual(self.tier(alert, evidence)[0], "actionable")
        self.assertEqual(self.tier(alert, evidence, noise_cut_coordination_as_risk=True)[0], "watch")

    def test_wave_score_pays_for_linked_wallets_only_under_flag(self):
        alert = {"signal_family": "reactivation_wave", "wave": {"net_buy_sol": 40, "unique_buyers": 20,
                 "balance_coverage_pct": 100}, "common_funders": [{"source": "f"}], "common_executors": [{"executor": "x"}]}
        legacy = s.wave_quality_score(alert, reactivation_config())
        flipped = s.wave_quality_score(alert, reactivation_config(noise_cut_coordination_as_risk=True))
        self.assertEqual(flipped, max(0, legacy - 25 - 15))
        funders_only = {**alert, "common_executors": []}
        self.assertEqual(s.wave_quality_score(funders_only, reactivation_config()) - s.wave_quality_score(
            funders_only, reactivation_config(noise_cut_coordination_as_risk=True)), 25)

    def test_synchronous_buys_become_a_penalty(self):
        alert = {"lane": "reactivation", "signal_family": "reactivation_wave", "action_tier": "watch",
                 "coordinated_activity": {"signals": [{"code": "synchronous_buys", "wallet_count": 6}]}}
        noise_cut.apply_noise_cut(copy.deepcopy(alert), {})
        flagged = noise_cut.apply_noise_cut(copy.deepcopy(alert), {"noise_cut_coordination_as_risk": True})
        self.assertIn("synchronous buys (risk)", flagged["quality_penalties"])
        self.assertEqual(flagged["noise_cut"]["risk_flags"], ["synchronous_buys"])
        small = copy.deepcopy(alert)
        small["coordinated_activity"]["signals"][0]["wallet_count"] = 3
        small = noise_cut.apply_noise_cut(small, {"noise_cut_coordination_as_risk": True})
        self.assertNotIn("synchronous buys (risk)", small["quality_penalties"])
        self.assertEqual(small["action_tier"], "watch")

    def test_flags_off_leave_alert_untouched(self):
        alert = {"action_tier": "candidate", "supply_integrity": {"status": "concentrated"},
                 "coordinated_activity": {"signals": [{"code": "young_low_activity", "wallet_count": 4}]}}
        before = copy.deepcopy(alert)
        self.assertEqual(noise_cut.apply_noise_cut(alert, {}), before)


class ClassifiedWalletsRiskOnlyTests(unittest.TestCase):
    def setUp(self):
        self.pool = s.Pool(pool_address="pool", token_address="token", mcap_usd=50_000,
                           liquidity_usd=10_000, volume_1h_usd=1_000)

    def build(self, **flags):
        config = reactivation_config(**flags)
        with patch.object(s.time, "time", return_value=NOW):
            alerts = s.build_alerts(self.pool, funded_fresh_buys(), config)
        return s.apply_alert_data_quality(alerts, {}, 3, 3, 0, config), config

    def test_legacy_classified_alert_becomes_a_thesis(self):
        alerts, config = self.build()
        self.assertNotIn("signal_family", alerts[0], "classic alerts are classified_wallets by convention")
        self.assertEqual(alerts[0]["action_tier"], "candidate")
        thesis, created = s.capture_signal_thesis({}, alerts, config, captured_at=s.iso(NOW))
        self.assertTrue(created)
        self.assertEqual(thesis["signal_family"], "classified_wallets")

    def test_risk_only_classified_alert_is_evidence_not_a_candidate(self):
        alerts, config = self.build(noise_cut_coordination_as_risk=True, noise_cut_classified_wallets_risk_only=True)
        self.assertTrue(alerts, "risk evidence is still emitted on evidence strength")
        for alert in alerts:
            self.assertEqual(alert["action_tier"], "noise")
            self.assertEqual(alert["score"], 0)
            self.assertEqual(alert["noise_cut"]["gate"]["reasons"], ["classified_wallets is risk evidence only"])
            self.assertIn("linked wallets (risk)", alert["quality_penalties"])
        pool_state = {}
        thesis, created = s.capture_signal_thesis(pool_state, alerts, config, captured_at=s.iso(NOW))
        self.assertIsNone(thesis)
        self.assertFalse(created)
        self.assertNotIn("signal_thesis", pool_state)

    def test_coordination_only_classic_alert_is_not_emitted_without_risk_only_mode(self):
        alerts, _config = self.build(noise_cut_coordination_as_risk=True)
        self.assertEqual(alerts, [])

    def test_classified_evidence_flags_wave_alerts_of_the_same_pool(self):
        config = reactivation_config(noise_cut_classified_wallets_risk_only=True)
        classic = {"hard_wallets": 4, "hard_sol": 12,
                   "common_funders": [{"source": "f"}], "lane": "reactivation"}
        wave = {"signal_family": "reactivation_wave", "lane": "reactivation", "action_tier": "watch",
                "wave": {"balance_coverage_pct": 100}}
        s.apply_alert_data_quality([classic, wave], {}, 1, 1, 0, config)
        self.assertEqual(wave["classified_wallet_evidence"]["hard_wallets"], 4)
        self.assertIn("fresh-wallet cluster (risk)", wave["quality_penalties"])
        self.assertNotIn("gate", wave["noise_cut"])
        self.assertEqual(classic["action_tier"], "noise")


class RiskGateTests(unittest.TestCase):
    def wave_alert(self, **fields):
        alert = {"lane": "reactivation", "signal_family": "reactivation_wave", "action_tier": "watch",
                 "created_at": s.iso(NOW), "data_quality": {"status": "complete", "reasons": []},
                 "wave": {"hold_age_minutes": 0, "min_hold_minutes": 30}}
        alert.update(fields)
        return alert

    def test_concentrated_and_unverified_supply_are_not_candidates(self):
        config = reactivation_config(noise_cut_risk_gates_enabled=True)
        for status in ("concentrated", "unverified"):
            alert = s.apply_signal_confirmation(self.wave_alert(supply_integrity={"status": status}), config)
            self.assertEqual(alert["action_tier"], "noise", status)
            self.assertEqual(alert["noise_cut"]["gate"]["reasons"], [f"supply integrity {status}"])
            self.assertTrue(noise_cut.excluded_from_candidates(alert))
        for status in ("distributed", "watch", None):
            alert = self.wave_alert(**({"supply_integrity": {"status": status}} if status else {}))
            alert = s.apply_signal_confirmation(alert, config)
            self.assertEqual(alert["action_tier"], "candidate", status)
            self.assertFalse(noise_cut.excluded_from_candidates(alert))

    def test_young_low_activity_cohort_is_not_a_candidate(self):
        signals = [{"code": "young_low_activity", "wallet_count": 4}]
        alert = self.wave_alert(coordinated_activity={"signals": signals})
        self.assertEqual(s.apply_signal_confirmation(copy.deepcopy(alert), reactivation_config())["action_tier"],
                         "candidate")
        gated = s.apply_signal_confirmation(alert, reactivation_config(noise_cut_risk_gates_enabled=True))
        self.assertEqual(gated["action_tier"], "noise")
        self.assertEqual(gated["noise_cut"]["gate"]["reasons"], ["young_low_activity cohort"])

    def test_gate_statuses_are_configurable(self):
        config = reactivation_config(noise_cut_risk_gates_enabled=True, noise_cut_gate_supply_statuses=["concentrated"])
        alert = s.apply_signal_confirmation(self.wave_alert(supply_integrity={"status": "unverified"}), config)
        self.assertEqual(alert["action_tier"], "candidate")

    def test_gate_is_idempotent_on_report_replay(self):
        config = reactivation_config(noise_cut_risk_gates_enabled=True)
        alert = s.apply_signal_confirmation(self.wave_alert(supply_integrity={"status": "concentrated"}), config)
        again = s.apply_signal_confirmation(copy.deepcopy(alert), config)
        self.assertEqual(again["quality_penalties"].count("risk gate"), 1)
        self.assertEqual(again["noise_cut"], alert["noise_cut"])

    def test_existing_thesis_is_marked_excluded_in_report(self):
        thesis = {"version": 2, "token_address": "token", "pool_address": "pool", "signal_family": "reactivation_wave",
                  "signal_at": s.iso(NOW), "supply_integrity": {"status": "concentrated"}, "cohort": []}
        state = {"pools": {"pool": {"signal_thesis": thesis}}}
        plain = s.signal_theses_for_report(state, config=reactivation_config())
        gated = s.signal_theses_for_report(state, config=reactivation_config(noise_cut_risk_gates_enabled=True))
        self.assertEqual((len(plain), len(gated)), (1, 1), "excluded theses stay tracked for outcome measurement")
        self.assertNotIn("noise_cut", plain[0])
        self.assertEqual(gated[0]["noise_cut"]["gate"]["reasons"], ["supply integrity concentrated"])
        self.assertEqual(noise_cut.thesis_gate(thesis, reactivation_config(noise_cut_risk_gates_enabled=True))["status"],
                         "excluded")
        self.assertIsNone(noise_cut.thesis_gate({**thesis, "supply_integrity": {"status": "distributed"}},
                                                reactivation_config(noise_cut_risk_gates_enabled=True)))


class ReactivationAgeTests(unittest.TestCase):
    def pool(self, age_hours):
        return s.Pool("pool", token_address=TOKEN, mcap_usd=100_000, pair_created_at=int(NOW - age_hours * 3600),
                      gmgn_attention={"sources": ["trending"]})

    def test_attention_mode_honours_minimum_age_only(self):
        config = {"discovery_source_mode": "gmgn_attention", "mcap_min_usd": 30_000, "mcap_max_usd": 500_000}
        with patch.object(s, "utc_now", return_value=s.datetime.fromtimestamp(NOW, s.timezone.utc)):
            self.assertTrue(s.pool_matches_config(self.pool(2), config))
            gated = {**config, "age_min_hours": 24}
            self.assertFalse(s.pool_matches_config(self.pool(2), gated))
            self.assertTrue(s.pool_matches_config(self.pool(30), gated))
            self.assertTrue(s.pool_matches_config(self.pool(30 * 24), {**gated, "age_max_hours": 360}))
            unknown = s.Pool("pool", token_address=TOKEN, mcap_usd=100_000, gmgn_attention={"sources": ["trending"]})
            self.assertFalse(s.pool_matches_config(unknown, gated))


class GmgnPrefilterTests(unittest.TestCase):
    def test_solana_token_info_has_the_robinhood_holder_schema(self):
        stats = noise_cut.solana_holder_stats(SOLANA_INFO)
        self.assertEqual(stats["top_10_holder_rate"], 0.62)
        self.assertEqual(stats["fresh_wallet_rate"], 0.0359)
        self.assertEqual(stats["bundler_wallets"], 894)
        self.assertAlmostEqual(stats["bundler_wallet_share"], 894 / 1682)
        self.assertEqual(gm.holder_stats(SOLANA_INFO)["concentration"]["top70_sniper_hold_rate"], 0.25)
        self.assertIsNone(noise_cut.solana_holder_stats({"address": TOKEN}))

    def test_thresholds_from_config(self):
        stats = noise_cut.solana_holder_stats(SOLANA_INFO)
        self.assertEqual(noise_cut.gmgn_prefilter_reasons(stats, {}), [])
        reasons = noise_cut.gmgn_prefilter_reasons(stats, {"gmgn_prefilter_max": {
            "top_10_holder_rate": 0.5, "fresh_wallet_rate": 0.3, "top70_sniper_hold_rate": None}})
        self.assertEqual(reasons, ["top_10_holder_rate 0.62 > 0.5"])

    def run_filter(self, state, cache, **flags):
        config = reactivation_config(gmgn_prefilter_enabled=True,
                                     gmgn_prefilter_max={"top_10_holder_rate": 0.5}, **flags)
        config["_gmgn_token_info_cache"] = cache
        pools = [s.Pool("pool-a", token_address=TOKEN), s.Pool("pool-b", token_address="other")]
        with patch.object(s.time, "time", return_value=NOW):
            kept = s.filter_reactivation_by_gmgn_holder_stats(state, pools, config, s.iso(NOW))
        return [pool.pool_address for pool in kept], config["_gmgn_prefilter_stats"]

    def test_prefilter_uses_only_already_fetched_token_info(self):
        state = {}
        with patch.object(s, "run_gmgn_cli") as cli:
            kept, stats = self.run_filter(state, {TOKEN: SOLANA_INFO})
        cli.assert_not_called()
        self.assertEqual(kept, ["pool-b"])
        self.assertEqual(stats["excluded"], 1)
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(stats["excluded_top_10_holder_rate"], 1)
        # A later scan without fresh token info reuses the stored snapshot.
        kept, stats = self.run_filter(state, {})
        self.assertEqual(kept, ["pool-b"])
        self.assertEqual(stats.get("fresh_stats", 0), 0)

    def test_prefilter_keeps_tracked_theses_and_expires_snapshots(self):
        state = {"pools": {"pool-a": {"signal_thesis": {"version": 2}}}}
        kept, stats = self.run_filter(state, {TOKEN: SOLANA_INFO})
        self.assertEqual(kept, ["pool-a", "pool-b"])
        self.assertEqual(stats["kept_tracked"], 1)
        state = {"gmgn_holder_stats": {TOKEN: {"top_10_holder_rate": 0.9, "checked_at": s.iso(NOW - 7 * 3600)}}}
        kept, _ = self.run_filter(state, {})
        self.assertEqual(kept, ["pool-a", "pool-b"])
        self.assertNotIn(TOKEN, state["gmgn_holder_stats"])

    def test_prefilter_is_off_by_default(self):
        pools = [s.Pool("pool-a", token_address=TOKEN)]
        config = reactivation_config()
        config["_gmgn_token_info_cache"] = {TOKEN: SOLANA_INFO}
        self.assertEqual(s.filter_reactivation_by_gmgn_holder_stats({}, pools, config, s.iso(NOW)), pools)
        self.assertFalse(config["gmgn_prefilter_enabled"])

    def test_shipped_config_keeps_every_noise_cut_switch_off(self):
        config = reactivation_config()
        self.assertFalse(noise_cut.coordination_as_risk(config))
        self.assertFalse(noise_cut.classified_wallets_risk_only(config))
        self.assertFalse(noise_cut.risk_gates_enabled(config))
        self.assertFalse(noise_cut.gmgn_prefilter_enabled(config))
        self.assertIsNone(config.get("age_min_hours"))


if __name__ == "__main__":
    unittest.main()
