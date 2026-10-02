import unittest
from unittest.mock import Mock

from wallet_links import infrastructure_sources, normalize_link
import scanner as s


class WalletLinkPolicyTests(unittest.TestCase):
    def group(self):
        return {"source": "funder", "members": ["a", "b"], "transfer_verified": True}

    def test_services_are_excluded_even_for_verified_transfers(self):
        for kind in ("cex", "Relay", "LI.FI", "router", "terminal"):
            config = {"wallet_source_labels": {"funder": {"kind": kind}}}
            self.assertTrue(normalize_link(self.group(), "common_funder", config)["supporting_only"])
            self.assertIn("funder", infrastructure_sources(config))

    def test_unclassified_executor_and_old_unverified_groups_are_supporting(self):
        self.assertTrue(normalize_link({"executor": "terminal", "members": ["a", "b"]},
            "common_executor")["supporting_only"])
        group = self.group()
        group.pop("transfer_verified")
        group["supporting_only"] = False
        self.assertTrue(normalize_link(group, "common_funder")["supporting_only"])

    def test_any_known_service_profile_excludes_whole_source(self):
        profile = {"a": {"source_kind": "cex"}, "b": {}}
        self.assertTrue(normalize_link(self.group(), "common_funder", profiles=profile)["supporting_only"])

    def test_verified_unknown_funder_is_a_direct_transfer_not_an_owner_identity(self):
        result = normalize_link(self.group(), "common_funder")
        self.assertFalse(result["supporting_only"])
        self.assertEqual(result["ownership"], "not_established")

    def test_supporting_fees_cannot_turn_one_link_family_into_two(self):
        rows = [{"owner": owner, "current_balance": 10, "current_retained_tokens": 10} for owner in ("a", "b")]
        groups = [normalize_link(self.group(), "common_funder"),
            normalize_link({"key": "1000", "members": ["a", "b"]}, "priority_fee")]
        self.assertEqual(s.supply_integrity_linked_clusters(rows, groups, 100)[0]["families"], ["common_funder"])

    def test_aged_wallet_funding_checks_are_bounded_and_pre_buy(self):
        rpc = Mock()
        rpc.signatures_for_address.return_value = [{"signature": str(i), "blockTime": 999 - i} for i in range(50)]
        rpc.transaction.return_value = {"transaction": {"message": {"instructions": [{"program": "system",
            "parsed": {"type": "transfer", "info": {"source": "funder", "destination": "a", "lamports": 1_000_000_000}}}]}}, "meta": {"err": None}}
        config = {"freshish_max_previous_txs": 5, "dormant_gap_days": 30,
            "low_tx_max_previous_txs": 10, "wallet_funding_transaction_limit": 3}
        result = s.classify_wallet(rpc, "a", "buy", 1000, config, {})
        self.assertEqual(result["wallet_class"], "normal")
        self.assertTrue(result["funding_verified"])
        self.assertEqual(rpc.transaction.call_count, 3)
        self.assertEqual(result["funding_history_status"], "bounded")
        self.assertIsNone(result["first_activity_at"])

    def test_old_cohort_cannot_keep_unverified_link_confirmation_on_recheck(self):
        thesis = {"cohort": [{"owner": "a", "common_funder": "funder"}, {"owner": "b", "common_funder": "funder"}],
            "supply_integrity": {"linkage_groups": [{**self.group(), "transfer_verified": False, "supporting_only": False}]}}
        groups = s.supply_integrity_linkage_groups(None, thesis, {})
        self.assertTrue(all(group["supporting_only"] for group in groups))

    def test_dust_funding_or_repeated_buys_do_not_create_material_funder_bonus(self):
        config = s.apply_lane(s.load_json(s.DEFAULT_CONFIG_PATH, {}), "reactivation")
        rows = [{"signer": owner, "wallet_class":"fresh", "sol_amount":10,
                 "funding_source":"funder", "funding_verified":True, "funding_sol":0.05}
                for owner in ("a", "b")]
        self.assertEqual(s.score_events(rows, config)[2], [])
        for row in rows: row["funding_sol"] = 2
        self.assertEqual(len(s.score_events(rows, config)[2]), 1)
        self.assertEqual(s.score_events([rows[0], rows[0]], config)[2], [])
