import copy
import json
import unittest

from coordinated_activity import analyze_coordinated_activity, compact_coordinated_activity


NOW = 1_759_320_000
OWNERS = ["wallet-a", "wallet-b", "wallet-c"]


def buys(owners=OWNERS, span=20):
    return [{"owner": owner, "transaction": "tx-" + owner, "timestamp": NOW - 60 + index * span,
        "bought_tokens": 50, "amount_native": 1, "executor": "public-router"}
        for index, owner in enumerate(owners)]


def profiles(owners=OWNERS, source=None, kind="unknown"):
    return {owner: {"previous_tx_count": 2, "history_complete": True, "first_activity_at": NOW - 86400,
        "funding_source": source, "funding_verified": source is not None,
        "funding_at": NOW - 300 + index * 20, "funding_amount_native": 0.2,
        "source_kind": kind} for index, owner in enumerate(owners)}


def positions(owners=OWNERS, balance=50):
    return [{"owner": owner, "current_balance": balance, "attributed_tokens": 50,
        "balance_verified": True, "checked_at": NOW} for owner in owners]


def analyze(rows=None, **kwargs):
    values = {"profiles": profiles(), "positions": positions(), "supply": 1000,
        "observed_at": NOW, "coverage": {"history_status": "complete", "expected_buyers": 3,
            "owner_resolution_pct": 100, "balance_coverage_pct": 100}}
    values.update(kwargs)
    return analyze_coordinated_activity(buys() if rows is None else rows, **values)


def codes(result):
    return {signal["code"] for signal in result["signals"]}


class CoordinatedActivityTests(unittest.TestCase):
    def test_young_cluster_is_one_family_and_same_cohort_material(self):
        result = analyze()
        self.assertEqual(result["status"], "pattern")
        self.assertTrue(result["metrics"]["material_pattern"])
        self.assertEqual(result["metrics"]["held_supply_pct"], 15)
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertEqual({signal["family"] for signal in result["signals"]}, {"temporal", "age_activity"})
        self.assertEqual((result["ownership"], result["bundle"]), ("not_established", "not_established"))

    def test_material_direct_funding_and_temporal_can_only_watch(self):
        result = analyze(profiles=profiles(source="unclassified-funder"))
        self.assertEqual(result["status"], "coordination_watch")
        self.assertIn("synchronized_preparation", codes(result))
        self.assertNotIn("action_tier", result)
        self.assertNotIn("signal_confirmation", result)
        for signal in result["signals"]:
            if signal["family"] == "funding":
                self.assertEqual(signal["detail"]["source_identity"], "unknown")

    def test_public_cex_relay_and_router_sources_are_not_links(self):
        for kind in ("service", "cex", "bridge", "router", "exchange", "Relay", "LI.FI"):
            with self.subTest(kind=kind):
                result = analyze(profiles=profiles(source="public-source", kind=kind))
                self.assertFalse(any(signal["family"] == "funding" for signal in result["signals"]))
                self.assertNotEqual(result["status"], "coordination_watch")
        result = analyze(profiles=profiles(source="public-source"), infrastructure_addresses={"public-source"})
        self.assertFalse(any(signal["family"] == "funding" for signal in result["signals"]))

    def test_service_label_on_any_profile_excludes_shared_source(self):
        info = profiles(source="public-source")
        info[OWNERS[0]]["source_kind"] = "cex"
        result = analyze(profiles=info)
        self.assertNotIn("common_direct_funding", codes(result))

    def test_same_slot_fees_amounts_and_executor_do_not_make_a_signal(self):
        rows = buys(span=500)
        for row in rows:
            row.update(slot=123, priority_fee=1000)
        result = analyze(rows, profiles={}, observed_at=NOW + 1000)
        self.assertEqual(result["status"], "no_pattern_in_checked_subset")
        self.assertEqual(result["bundle"], "not_established")
        same = analyze([dict(row, slot=123) for row in buys()], profiles={})
        self.assertEqual(codes(same), {"synchronous_buys"})
        self.assertFalse(same["metrics"]["material_pattern"])
        self.assertTrue(same["signals"][0]["supporting_only"])

    def test_duplicate_owner_transactions_do_not_add_tokens_or_holdings(self):
        rows = buys()
        result = analyze(rows + copy.deepcopy(rows), positions=positions() * 2)
        self.assertEqual(result["metrics"]["buyer_count"], 3)
        self.assertEqual(result["metrics"]["buy_transactions"], 3)
        self.assertEqual(result["metrics"]["observed_bought_tokens"], 150)
        self.assertEqual(result["metrics"]["held_tokens"], 150)
        self.assertEqual(result, analyze(rows))

    def test_one_transaction_with_many_owners_is_not_synchronous_buys(self):
        result = analyze([dict(row, transaction="one-tx") for row in buys()], profiles={})
        self.assertNotIn("synchronous_buys", codes(result))
        self.assertFalse(result["metrics"]["material_pattern"])

    def test_repeated_single_owner_cannot_manufacture_three_wallets(self):
        rows = [dict(buys()[0], transaction="tx-" + str(index)) for index in range(3)]
        result = analyze(rows)
        self.assertFalse(result["signals"])
        self.assertEqual(result["metrics"]["buyer_count"], 1)
        self.assertEqual(result["metrics"]["held_tokens"], 50)

    def test_unknown_balances_and_supply_are_not_zero_or_material(self):
        for changes in ({"positions": []}, {"supply": None}, {"supply": 0}, {"supply": float("nan")}):
            with self.subTest(changes=changes):
                result = analyze(**changes)
                self.assertIsNone(result["metrics"]["held_supply_pct"])
                self.assertFalse(result["metrics"]["material_pattern"])
                self.assertEqual(result["coverage"]["status"], "partial")
        result = analyze(positions=[])
        self.assertIsNone(result["metrics"]["held_tokens"])
        self.assertIsNone(result["metrics"]["verified_subset_held_tokens"])
        self.assertTrue(all(signal["held_supply_pct"] is None for signal in result["signals"]))

    def test_explicit_zero_verified_balance_remains_zero(self):
        result = analyze(positions=positions(balance=0))
        self.assertEqual(result["metrics"]["held_supply_pct"], 0)
        self.assertFalse(result["metrics"]["material_pattern"])

    def test_partial_history_cannot_assert_wallet_age(self):
        for complete in (False, None, 1, "true"):
            info = profiles()
            for row in info.values():
                row["history_complete"] = complete
            result = analyze(profiles=info)
            self.assertNotIn("young_low_activity", codes(result))
            self.assertEqual(result["coverage"]["profile_coverage_pct"], 0)
            self.assertFalse(result["metrics"]["material_pattern"])

    def test_young_age_activity_and_utc_day_boundaries(self):
        for field, value in (("previous_tx_count", 4), ("previous_tx_count", True),
                ("first_activity_at", NOW - 15 * 86400), ("first_activity_at", NOW + 1)):
            info = profiles()
            info[OWNERS[0]][field] = value
            self.assertNotIn("young_low_activity", codes(analyze(profiles=info)))
        info = profiles()
        info[OWNERS[0]]["first_activity_at"] -= 86400
        self.assertNotIn("young_low_activity", codes(analyze(profiles=info)))

    def test_under_one_percent_concentration_is_not_material(self):
        result = analyze(positions=positions(balance=3))
        self.assertAlmostEqual(result["metrics"]["held_supply_pct"], 0.9)
        self.assertFalse(result["metrics"]["material_pattern"])
        self.assertTrue(all(signal["supporting_only"] for signal in result["signals"]))
        self.assertTrue(analyze(positions=positions(balance=4))["metrics"]["material_pattern"])

    def test_one_whale_and_two_empty_wallets_are_not_material(self):
        held = positions(balance=0)
        held[0].update(current_balance=500, attributed_tokens=500)
        result = analyze(positions=held)
        self.assertEqual(result["metrics"]["held_supply_pct"], 50)
        self.assertFalse(result["metrics"]["material_pattern"])

    def test_retention_caps_by_attribution_observed_sells_and_explicit_retained(self):
        rows = [dict(row, sold_tokens=40) for row in buys()]
        held = positions(balance=500)
        held[0]["retained_tokens"] = 5
        result = analyze(rows * 2, positions=held)
        self.assertEqual(result["metrics"]["held_tokens"], 25)
        self.assertEqual(result["metrics"]["observed_bought_tokens"], 150)
        self.assertEqual(analyze([dict(row, sold_tokens=100) for row in buys()])["metrics"]["held_tokens"], 0)

    def test_unverified_stale_future_and_conflicting_balances_fail_closed(self):
        for changes in ({"balance_verified": False}, {"balance_verified": 1},
                {"checked_at": NOW - 3601}, {"checked_at": NOW + 1}, {"checked_at": None}):
            held = positions()
            held[0].update(changes)
            result = analyze(positions=held)
            self.assertIsNone(result["metrics"]["held_tokens"])
            self.assertFalse(result["metrics"]["material_pattern"])
            self.assertEqual(result["metrics"]["verified_subset_held_tokens"], 100)
        held = positions()
        held.append(dict(held[0], current_balance=49))
        self.assertIsNone(analyze(positions=held)["metrics"]["held_tokens"])

    def test_funding_requires_parsed_transfer_and_strict_pre_buy_order(self):
        for field, value in (("funding_verified", False), ("funding_verified", 1),
                ("funding_at", NOW + 1), ("funding_at", None)):
            info = profiles(source="funder")
            for row in info.values():
                row[field] = value
            result = analyze(profiles=info)
            self.assertNotIn("common_direct_funding", codes(result))
        info = profiles(source="funder")
        for row in buys():
            info[row["owner"]]["funding_at"] = row["timestamp"]
        self.assertNotIn("common_direct_funding", codes(analyze(profiles=info)))

    def test_funding_lag_and_both_cluster_windows_are_bounded(self):
        info = profiles(source="funder")
        for row in buys():
            info[row["owner"]]["funding_at"] = row["timestamp"] - 3601
        result = analyze(profiles=info)
        self.assertIn("common_direct_funding", codes(result))
        self.assertNotIn("synchronized_preparation", codes(result))
        info = profiles(source="funder")
        for index, owner in enumerate(OWNERS):
            info[owner]["funding_at"] = NOW - 2500 + index * 1000
        self.assertNotIn("synchronized_preparation", codes(analyze(profiles=info)))
        rows = buys(span=901)
        for owner in OWNERS:
            info[owner]["funding_at"] = NOW - 300
        self.assertNotIn("synchronized_preparation", codes(analyze(rows, profiles=info, observed_at=NOW + 2000)))

    def test_funding_and_funding_timing_are_one_family(self):
        info = profiles(source="funder")
        for row in info.values():
            row["history_complete"] = False
        result = analyze(buys(span=300), profiles=info, observed_at=NOW + 600)
        self.assertEqual(codes(result), {"common_direct_funding", "synchronized_preparation"})
        self.assertEqual({signal["family"] for signal in result["signals"]}, {"funding"})
        self.assertFalse(result["metrics"]["material_pattern"])
        self.assertEqual(result["status"], "pattern")

    def test_disjoint_or_tiny_overlapping_cohorts_cannot_combine_families(self):
        other = ["wallet-d", "wallet-e", "wallet-f"]
        rows = buys() + [dict(row, timestamp=NOW - 10000 + index * 500) for index, row in enumerate(buys(other))]
        info = profiles(other, source="funder")
        for owner in other:
            info[owner].update(history_complete=False, funding_at=NOW - 11000)
        result = analyze(rows, profiles=info, positions=positions(OWNERS + other), coverage={"status": "complete", "total_buyers": 6})
        self.assertFalse(result["metrics"]["material_pattern"])
        self.assertNotEqual(result["status"], "coordination_watch")
        info[OWNERS[0]] = dict(info[other[0]], funding_at=NOW - 300)
        result = analyze(rows, profiles=info, positions=positions(OWNERS + other))
        self.assertFalse(result["metrics"]["material_pattern"])

    def test_partial_coverage_remains_explicit_on_positive_and_negative_results(self):
        result = analyze(coverage={"status": "partial", "total_buyers": 10})
        self.assertEqual(result["coverage"]["buyer_coverage_pct"], 30)
        self.assertEqual(result["coverage"]["status"], "partial")
        negative = analyze(buys(span=500), profiles={}, coverage={})
        self.assertEqual(negative["status"], "no_pattern_in_checked_subset")
        self.assertIsNone(negative["coverage"]["buyer_coverage_pct"])
        self.assertEqual(negative["coverage"]["status"], "partial")

    def test_unknown_buy_amounts_do_not_become_zero(self):
        rows = buys()
        rows[0].pop("bought_tokens")
        result = analyze(rows)
        self.assertIsNone(result["metrics"]["representative_bought_tokens"])
        self.assertEqual(result["metrics"]["observed_bought_tokens"], 150)
        self.assertEqual(result["metrics"]["held_tokens"], 150)
        missing = analyze(rows, positions=[])
        self.assertIsNone(missing["metrics"]["observed_bought_tokens"])
        self.assertIsNone(missing["metrics"]["observed_supply_pct"])

    def test_output_is_deterministic_and_inputs_are_not_mutated(self):
        rows, info, held = buys(), profiles(source="funder"), positions()
        original = copy.deepcopy((rows, info, held))
        result = analyze(rows, profiles=info, positions=held)
        shuffled = analyze(list(reversed(rows)), profiles=dict(reversed(list(info.items()))), positions=list(reversed(held)))
        self.assertEqual(json.dumps(result, sort_keys=True, allow_nan=False), json.dumps(shuffled, sort_keys=True, allow_nan=False))
        self.assertEqual((rows, info, held), original)

    def test_missing_clock_is_explicit_and_no_buy_input_is_not_checked(self):
        result = analyze(observed_at=None)
        self.assertIsNone(result["checked_at"])
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(analyze([])["status"], "not_checked")
        self.assertEqual(analyze_coordinated_activity(None)["status"], "not_checked")

    def test_position_mapping_and_timezone_aware_iso_are_supported(self):
        held = {row["owner"]: {k: v for k, v in row.items() if k != "owner"} for row in positions()}
        stamp = analyze()["checked_at"]
        for row in held.values():
            row["checked_at"] = stamp
        self.assertEqual(analyze(positions=held, observed_at=stamp), analyze())

    def test_output_bounds_cannot_be_disabled_by_config(self):
        owners = [f"wallet-{index:03}" for index in range(30)]
        result = analyze(buys(owners, span=1), profiles=profiles(owners), positions=positions(owners),
            supply=5000, config={"max_members": 999, "max_signals": 999}, coverage={"status": "complete", "total_buyers": 30})
        self.assertTrue(result["signals"])
        self.assertTrue(all(len(signal["members"]) <= 20 and signal["wallet_count"] == 30 for signal in result["signals"]))
        self.assertEqual(result["metrics"]["material_union_held_supply_pct"], 30)
        self.assertEqual(result["coverage"]["status"], "partial")
        owners = [f"wallet-{index:03}" for index in range(60)]
        rows = [dict(row, timestamp=NOW - 10000 + index // 3 * 300) for index, row in enumerate(buys(owners, span=0))]
        result = analyze(rows, profiles={}, positions=positions(owners), supply=5000)
        self.assertEqual(len(result["signals"]), 12)
        self.assertIn("signals_truncated", result["coverage"]["reasons"])

    def test_large_group_economics_and_intersection_use_every_member(self):
        owners = [f"wallet-{index:03}" for index in range(40)]
        result = analyze(buys(owners, span=1), profiles=profiles(owners), positions=positions(owners),
            supply=5000, coverage={"history_status": "complete", "expected_buyers": 40,
                "owner_resolution_pct": 100, "balance_coverage_pct": 100})
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertEqual(result["metrics"]["material_union_held_supply_pct"], 40)
        self.assertEqual(result["metrics"]["max_material_group_wallets"], 40)
        self.assertEqual(result["signals"][0]["wallet_count"], 40)
        self.assertEqual(len(result["signals"][0]["members"]), 20)

    def test_funding_linked_sell_and_rebuy_wave_is_rotation_not_new_family(self):
        sellers = ["old-a", "old-b", "old-c"]
        buyers = ["new-a", "new-b", "new-c"]
        owners = sellers + buyers
        rows = [dict(row, timestamp=NOW - 200 + index) for index, row in enumerate(buys(sellers))]
        rows += [{"owner": owner, "transaction": "sale-" + owner, "kind": "sell",
            "timestamp": NOW - 100 + index, "sold_tokens": 50, "amount_native": 1}
            for index, owner in enumerate(sellers)]
        rows += [dict(row, timestamp=NOW - 40 + index) for index, row in enumerate(buys(buyers))]
        info = profiles(owners, source="unclassified-funder")
        for profile in info.values():
            profile["funding_at"] = NOW - 300
        held = positions(buyers) + positions(sellers, balance=0)
        result = analyze(rows, profiles=info, positions=held)
        rotation = next(signal for signal in result["signals"] if signal["code"] == "market_rotation")
        self.assertTrue(rotation["supporting_only"])
        self.assertEqual(rotation["detail"]["net_observed_tokens"], 0)
        self.assertEqual(rotation["detail"]["matched_turnover_supply_pct"], 15)
        self.assertEqual(result["metrics"]["market_rotation_observations"], 1)
        excluded = analyze(rows, profiles=profiles(owners, source="unclassified-funder", kind="cex"), positions=held)
        self.assertNotIn("market_rotation", codes(excluded))
        no_sales = analyze([row for row in rows if row.get("kind") != "sell"], profiles=info, positions=held)
        self.assertNotIn("market_rotation", codes(no_sales))

    def test_compact_strips_members_and_does_not_mutate_full_evidence(self):
        result = analyze(profiles=profiles(source="funder"))
        original = copy.deepcopy(result)
        compact = compact_coordinated_activity(result)
        self.assertEqual(result, original)
        self.assertEqual(compact["metrics"], result["metrics"])
        self.assertTrue(all("members" not in signal and "source" not in signal["detail"] for signal in compact["signals"]))
        compact["limitations"].append("changed")
        self.assertEqual(result, original)
        self.assertEqual(compact_coordinated_activity(None), {})

    def test_authoritative_window_attribution_overrides_representative_buys(self):
        held = positions()
        for row in held:
            row.update(current_balance=250, attributed_tokens=300, retained_tokens=240)
        rows = [dict(row, bought_tokens=1, sold_tokens=250) for row in buys()]
        result = analyze(rows, positions=held)
        self.assertEqual(result["metrics"]["representative_bought_tokens"], 3)
        self.assertEqual(result["metrics"]["observed_bought_tokens"], 900)
        self.assertEqual(result["metrics"]["held_tokens"], 720)
        self.assertTrue(result["metrics"]["material_pattern"])

    def test_adapter_coverage_names_are_preserved_without_inflation(self):
        result = analyze(coverage={"history_status": "partial", "owner_resolution_pct": 75,
            "balance_coverage_pct": 80, "expected_buyers": 10})
        self.assertEqual(result["coverage"]["history_status"], "partial")
        self.assertEqual(result["coverage"]["owner_resolution_pct"], 75)
        self.assertEqual(result["coverage"]["balance_coverage_pct"], 80)
        self.assertEqual(result["coverage"]["buyer_coverage_pct"], 30)
        self.assertEqual(result["coverage"]["status"], "partial")

    def test_roundtrip_inventory_churn_is_a_supporting_warning_not_proof(self):
        owner = OWNERS[0]
        rows = [{"owner": owner, "transaction": "b1", "kind": "buy", "timestamp": NOW - 100, "bought_tokens": 50},
            {"owner": owner, "transaction": "b2", "kind": "buy", "timestamp": NOW - 80, "bought_tokens": 50},
            {"owner": owner, "transaction": "s1", "kind": "sell", "timestamp": NOW - 60, "sold_tokens": 40},
            {"owner": owner, "transaction": "s2", "kind": "sell", "timestamp": NOW - 40, "sold_tokens": 40}]
        held = [{"owner": owner, "current_balance": 20, "attributed_tokens": 100,
            "retained_tokens": 20, "balance_verified": True, "checked_at": NOW}]
        result = analyze(rows + rows, profiles={}, positions=held)
        self.assertEqual(codes(result), {"inventory_churn"})
        self.assertEqual(result["status"], "pattern")
        self.assertFalse(result["metrics"]["material_pattern"])
        self.assertEqual(result["metrics"]["inventory_churn_wallets"], 1)
        signal = result["signals"][0]
        self.assertTrue(signal["supporting_only"])
        self.assertEqual(signal["detail"]["turnover_pct"], 80)
        self.assertEqual(result["metrics"]["held_tokens"], 20)
        self.assertEqual((result["ownership"], result["bundle"]), ("not_established", "not_established"))

    def test_legitimate_arbitrage_has_no_ownership_or_provider_attribution(self):
        owner = OWNERS[0]
        rows = [{"owner": owner, "transaction": "arbitrage-1", "kind": "buy", "timestamp": NOW - 100, "bought_tokens": 50},
            {"owner": owner, "transaction": "arbitrage-2", "kind": "buy", "timestamp": NOW - 80, "bought_tokens": 50},
            {"owner": owner, "transaction": "exit-1", "kind": "sell", "timestamp": NOW - 60, "sold_tokens": 100},
            {"owner": owner, "transaction": "exit-2", "kind": "sell", "timestamp": NOW - 40, "sold_tokens": 100}]
        result = analyze(rows, profiles={}, positions=positions(balance=0))
        self.assertEqual(result["status"], "pattern")
        signal = result["signals"][0]
        self.assertEqual(signal["detail"]["turnover_pct"], 100)
        self.assertEqual(signal["detail"]["sold_tokens_capped_to_buys"], 100)
        self.assertEqual(signal["detail"]["observed_net_tokens"], 0)
        self.assertIn("arbitrage", signal["detail"]["interpretation"])
        self.assertEqual(result["ownership"], "not_established")
        self.assertNotIn("Proxima", json.dumps(result))

    def test_churn_needs_two_distinct_legs_each_and_verified_small_retention(self):
        owner = OWNERS[0]
        base = [{"owner": owner, "transaction": f"b{i}", "timestamp": NOW - 100 + i, "bought_tokens": 50} for i in range(2)]
        sales = [{"owner": owner, "transaction": f"s{i}", "kind": "sell", "timestamp": NOW - 50 + i, "sold_tokens": 50} for i in range(2)]
        for rows, held in ((base + sales[:1] * 2, positions(balance=0)),
                (base[:1] * 2 + sales, positions(balance=0)), (base + sales, positions(balance=30)),
                (base + sales, []), (base + [dict(row, timestamp=NOW + 1) for row in sales], positions(balance=0)),
                (base + [dict(row, timestamp=NOW - 200) for row in sales], positions(balance=0))):
            result = analyze(rows, profiles={}, positions=held)
            self.assertNotIn("inventory_churn", codes(result))

    def test_sell_rows_do_not_enter_buyer_or_temporal_cohort(self):
        extra = {"owner": "seller-only", "transaction": "exit", "kind": "sell", "timestamp": NOW - 10, "sold_tokens": 1000}
        self.assertEqual(analyze(buys() + [extra]), analyze())

    def test_buy_and_sell_same_transaction_are_deduplicated_separately(self):
        owner = OWNERS[0]
        rows = [{"owner": owner, "transaction": f"t{i}", "timestamp": NOW - 100 + i * 10, "bought_tokens": 50} for i in range(2)]
        rows += [dict(row, kind="sell", timestamp=row["timestamp"] + 1, sold_tokens=50) for row in rows]
        result = analyze(rows * 2, profiles={}, positions=positions(balance=0))
        self.assertIn("inventory_churn", codes(result))
        self.assertEqual(result["metrics"]["buy_transactions"], 2)
        self.assertEqual(result["metrics"]["sell_transactions"], 2)

    def test_buy_window_config_and_inclusive_boundary(self):
        rows = buys(span=60)
        self.assertIn("synchronous_buys", codes(analyze(rows, observed_at=NOW + 100)))
        self.assertNotIn("synchronous_buys", codes(analyze(rows, observed_at=NOW + 100,
            config={"buy_window_seconds": 119})))
        self.assertIn("synchronous_buys", codes(analyze(buys(span=100), observed_at=NOW + 200,
            config={"buy_window_seconds": 200})))

    def test_preparation_window_and_funding_lag_are_configurable(self):
        info = profiles(source="funder")
        self.assertIn("synchronized_preparation", codes(analyze(profiles=info)))
        self.assertNotIn("synchronized_preparation", codes(analyze(profiles=info,
            config={"preparation_window_seconds": 39})))
        self.assertNotIn("synchronized_preparation", codes(analyze(profiles=info,
            config={"max_funding_lag_seconds": 239})))
        self.assertIn("synchronized_preparation", codes(analyze(profiles=info,
            config={"max_funding_lag_seconds": 240})))
        rows = buys(span=500)
        self.assertIn("synchronized_preparation", codes(analyze(rows, profiles=info, observed_at=NOW + 1200,
            config={"preparation_window_seconds": 1000})))

    def test_young_config_refers_to_available_first_activity_not_creation(self):
        self.assertNotIn("young_low_activity", codes(analyze(config={"young_max_previous_tx_count": 1})))
        self.assertNotIn("young_low_activity", codes(analyze(config={"young_max_age_days": 0.5})))
        info = profiles()
        for row in info.values():
            row.update(previous_tx_count=4, first_activity_at=NOW - 15 * 86400)
        result = analyze(profiles=info, config={"young_max_previous_tx_count": 4, "young_max_age_days": 16})
        self.assertIn("young_low_activity", codes(result))
        signal = next(signal for signal in result["signals"] if signal["code"] == "young_low_activity")
        self.assertIn("first_activity_utc_day", signal["detail"])
        self.assertNotIn("creation", signal["label"].lower())

    def test_material_union_excludes_unrelated_holders_and_deduplicates_groups(self):
        unrelated = "unrelated-holder"
        rows = buys() + [{"owner": unrelated, "transaction": "old-buy", "timestamp": NOW - 10000, "bought_tokens": 600}]
        held = positions() + [{"owner": unrelated, "attributed_tokens": 600, "current_balance": 600,
            "balance_verified": True, "checked_at": NOW}]
        result = analyze(rows, profiles=profiles(source="funder"), positions=held,
            coverage={"history_status": "complete", "expected_buyers": 4, "owner_resolution_pct": 100, "balance_coverage_pct": 100})
        self.assertEqual(result["metrics"]["held_supply_pct"], 75)
        self.assertEqual(result["metrics"]["material_union_held_supply_pct"], 15)
        self.assertEqual(result["metrics"]["max_material_group_held_supply_pct"], 15)
        self.assertNotIn(unrelated, {member for signal in result["signals"] for member in signal["members"]})

    def test_impossible_total_balances_are_not_material(self):
        held = positions(balance=600)
        for row in held:
            row["attributed_tokens"] = 600
        result = analyze(positions=held)
        self.assertFalse(result["metrics"]["material_pattern"])
        self.assertIsNone(result["metrics"]["held_supply_pct"])
        self.assertIsNone(result["metrics"]["material_union_held_supply_pct"])
        self.assertIn("held_amount_exceeds_total_supply", result["coverage"]["reasons"])

    def test_unsolicited_dust_cannot_create_funding_family_or_watch(self):
        for amount in (0, 0.001, 0.049, 0.05, 0.099):
            with self.subTest(amount=amount):
                info = profiles(source="dust-sender")
                for row in info.values():
                    row.update(funding_amount_native=amount, history_complete=False)
                result = analyze(profiles=info)
                self.assertFalse(any(signal["family"] == "funding" for signal in result["signals"]))
                self.assertFalse(result["metrics"]["material_pattern"])
                self.assertNotEqual(result["status"], "coordination_watch")

    def test_unknown_or_invalid_funding_amount_abstains(self):
        for amount in (None, False, -1, float("nan"), float("inf"), "invalid"):
            with self.subTest(amount=amount):
                info = profiles(source="funder")
                for row in info.values():
                    row["funding_amount_native"] = amount
                result = analyze(profiles=info)
                self.assertNotEqual(result["status"], "coordination_watch")
                self.assertFalse(any(signal["family"] == "funding" for signal in result["signals"]))
                self.assertEqual(result["coverage"]["status"], "partial")
        info = profiles(source="funder")
        for row in info.values():
            row.pop("funding_amount_native")
        self.assertNotIn("common_direct_funding", codes(analyze(profiles=info)))

    def test_both_native_and_relative_minimums_are_required_and_configurable(self):
        info = profiles(source="funder")
        for row in info.values():
            row["funding_amount_native"] = 0.05
        rows = [dict(row, amount_native=0.5) for row in buys()]
        self.assertEqual(analyze(rows, profiles=info)["status"], "coordination_watch")
        for config in ({"min_funding_native": 0.051}, {"min_funding_buy_fraction": 0.101}):
            result = analyze(rows, profiles=info, config=config)
            self.assertFalse(any(signal["family"] == "funding" for signal in result["signals"]))
        for row in info.values():
            row["funding_amount_native"] = 0.02
        self.assertEqual(analyze(profiles=info,
            config={"min_funding_native": 0.01, "min_funding_buy_fraction": 0.02})["status"], "coordination_watch")
        below_native = analyze([dict(row, amount_native=0.1) for row in buys()], profiles=info)
        self.assertFalse(any(signal["family"] == "funding" for signal in below_native["signals"]))

    def test_unknown_first_buy_cost_keeps_funding_supporting_only(self):
        for cost in (None, 0, float("nan"), float("inf"), True):
            with self.subTest(cost=cost):
                rows = [dict(row, amount_native=cost) for row in buys()]
                result = analyze(rows, profiles=profiles(source="funder"))
                funding = [signal for signal in result["signals"] if signal["family"] == "funding"]
                self.assertTrue(funding)
                self.assertNotEqual(result["status"], "coordination_watch")
                self.assertTrue(all(signal["supporting_only"] for signal in funding))
                self.assertTrue(all(signal["detail"]["buy_fraction_checked_wallets"] == 0 for signal in funding))
                self.assertEqual(result["coverage"]["status"], "partial")

    def test_unknown_cost_owner_cannot_complete_material_funding_group(self):
        rows = buys()
        rows[0].pop("amount_native")
        result = analyze(rows, profiles=profiles(source="funder"))
        self.assertNotEqual(result["status"], "coordination_watch")
        funding = [signal for signal in result["signals"] if signal["family"] == "funding"]
        self.assertTrue(all(signal["supporting_only"] for signal in funding))
        self.assertTrue(all(signal["detail"]["buy_fraction_checked_wallets"] == 2 for signal in funding))
        info = profiles(source="funder")
        info[OWNERS[0]]["funding_amount_native"] = 0.001
        self.assertNotIn("common_direct_funding", codes(analyze(profiles=info)))

    def test_later_or_duplicate_buy_cost_does_not_rescue_unknown_first_cost(self):
        rows = buys()
        for row in rows:
            row.pop("amount_native")
        later = [dict(row, transaction=row["transaction"] + "-later", timestamp=row["timestamp"] + 1,
            amount_native=1) for row in rows]
        result = analyze(rows + later, profiles=profiles(source="funder"))
        self.assertNotEqual(result["status"], "coordination_watch")
        duplicates = [dict(row, amount_native=1) for row in rows]
        self.assertNotEqual(analyze(rows + duplicates, profiles=profiles(source="funder"))["status"], "coordination_watch")


if __name__ == "__main__":
    unittest.main()
