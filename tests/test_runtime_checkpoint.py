import base64
import copy
import gzip
import hashlib
import json
import random
import unittest
from unittest.mock import Mock, patch

import runtime_checkpoint as checkpoint


def canonical(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True,
                      sort_keys=True, allow_nan=False).encode("ascii")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def legacy_checkpoint(state, chunk_bytes=None):
    raw = canonical(state)
    encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    payload = {"schema_version": 1, "encoding": "gzip+base64", "sha256": digest(raw),
               "decoded_bytes": len(raw), "data": encoded, "runtime": state.get("_runtime", {})}
    if chunk_bytes is None:
        return payload, {}
    parts = []
    for start in range(0, len(encoded), chunk_bytes):
        data = encoded[start:start + chunk_bytes]
        parts.append({"schema_version": 1, "encoding": "gzip+base64-part", "sha256": digest(data.encode()),
                      "encoded_bytes": len(data), "data": data})
    root = {key: value for key, value in payload.items() if key != "data"}
    root.update(schema_version=2, encoding="gzip+base64+parts", encoded_bytes=len(encoded),
                parts=[{"id": part["sha256"], "bytes": part["encoded_bytes"]} for part in parts])
    return root, {part["sha256"]: part for part in parts}


def staged(state):
    payload = checkpoint.build_checkpoint(state)
    root, parts = checkpoint.checkpoint_documents(payload)
    return payload, root, {part["sha256"]: part for part in parts}


def reseal(root):
    root["manifest_sha256"] = digest(canonical({key: value for key, value in root.items()
                                               if key != "manifest_sha256"}))


class RuntimeCheckpointTests(unittest.TestCase):
    def state(self):
        return {"_runtime": {"revision": 4, "updated_at": "2026-10-07T10:00:00Z", "writer": "test"},
                "pools": {"pool-a": {"cursor": "opaque", "wave_swaps": [{"signature": "receipt"}],
                                     "signal_thesis": {"cohort": [{"owner": "holder", "raw_balance": "123"}],
                                                       "caught_at": "2026-09-02T00:00:00Z"}},
                          "qool-b": {"cursor": "other", "signal_thesis_history": [{"evidence": "retain"}]}},
                "market": {"A1": {"observed_at": "original"}, "B2": {"liquidity": 12}},
                "rpc_monthly_usage": {"2026-10": {"alchemy": {"estimated_units": 1234}}},
                "empty": {}, "values": [1, None, False, "unicode:\u03bb"],
                "unknown_evidence": {"new-field": {"retain_everything": True}},
                "wallet_cache": {"rebuildable": 1}, "social_cache": {"rebuildable": 2},
                "gmgn_cache": {"rebuildable": 3}, "enrichment_cache": {"rebuildable": 4}}

    def test_new_wire_remains_schema_two_with_existing_blob_contract_and_limits(self):
        _, root, parts = staged(self.state())
        self.assertEqual(root["schema_version"], 2)
        self.assertEqual(root["encoding"], "gzip+base64+parts")
        self.assertEqual(checkpoint.MAX_PARTS, 768)
        self.assertLessEqual(len(root["parts"]), 768)
        self.assertEqual(root["encoded_bytes"], sum(ref["bytes"] for ref in root["parts"]))
        for part in parts.values():
            self.assertEqual(part["schema_version"], 1)
            self.assertEqual(part["encoding"], "gzip+base64-part")
            self.assertLessEqual(part["encoded_bytes"], 256 * 1024)
            self.assertEqual(part["sha256"], digest(part["data"].encode("ascii")))
        self.assertFalse(any(key.startswith("_") for key in root))
        self.assertNotIn("data", root)

    def test_metadata_only_changes_reuse_every_part(self):
        state = self.state()
        _, first, first_parts = staged(state)
        state["_runtime"].update(revision=500, writer="different", updated_at="2026-10-07T12:01:59Z")
        _, second, second_parts = staged(state)
        self.assertEqual(first_parts, second_parts)
        self.assertEqual(first["parts"], second["parts"])
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(len(second["inline_members"]), 1)
        self.assertEqual(second["inline_members"][0]["path"], ["_runtime"])
        self.assertEqual(checkpoint.decode_checkpoint(checkpoint.hydrate_checkpoint(second, second_parts.__getitem__)),
                         {key: value for key, value in state.items() if key not in checkpoint.REBUILDABLE_KEYS})

    def test_one_pool_changes_only_its_bucket_even_when_it_grows(self):
        state = self.state()
        state["pools"]["pool-a"]["large"] = random.Random(17).randbytes(180000).hex()
        _, first, first_parts = staged(state)
        state["pools"]["pool-a"]["large"] += "more evidence" * 10000
        state["pools"]["pool-a"]["cursor"] = "new-cursor"
        _, second, second_parts = staged(state)
        own_first = {ref["id"] for ref in first["parts"] if ref["path"] == ["pools", "p"]}
        own_second = {ref["id"] for ref in second["parts"] if ref["path"] == ["pools", "p"]}
        self.assertGreater(len(own_first), 1)
        self.assertEqual(set(first_parts) - own_first, set(second_parts) - own_second)
        self.assertLessEqual(set(first_parts) ^ set(second_parts), own_first | own_second)
        self.assertNotEqual(own_first, own_second)

    def test_dictionary_order_and_cache_changes_do_not_change_members(self):
        state = self.state()
        first = checkpoint.build_checkpoint(state)
        reordered = json.loads(json.dumps(state, sort_keys=True))
        for key in checkpoint.REBUILDABLE_KEYS:
            reordered[key] = {"different": [1, 2, 3]}
        self.assertEqual(first, checkpoint.build_checkpoint(reordered))

    def test_large_discovery_map_uses_fixed_buckets_and_preserves_every_entry(self):
        state = {"market": {f"{letter}{index:05d}": {"value": index}
                            for letter in "ABCDEF" for index in range(600)}}
        _, root, parts = staged(state)
        self.assertLess(len(parts), 20)
        self.assertEqual(checkpoint.decode_checkpoint(checkpoint.hydrate_checkpoint(root, parts.__getitem__)), state)
        state["market"]["A00001"]["value"] = "changed"
        _, changed, new_parts = staged(state)
        own = {ref["id"] for ref in root["parts"] if ref["path"] == ["market", "A"]}
        new_own = {ref["id"] for ref in changed["parts"] if ref["path"] == ["market", "A"]}
        self.assertEqual(set(parts) - own, set(new_parts) - new_own)

    def test_thousands_of_small_pools_do_not_exhaust_reference_count(self):
        state={"pools":{f"{letter}{index:04d}":{"cursor":str(index)}
                        for letter in "ABCDEFGH" for index in range(400)}}
        _,root,parts=staged(state)
        self.assertLess(len(parts),32)
        self.assertEqual(checkpoint.decode_checkpoint(checkpoint.hydrate_checkpoint(root,parts.__getitem__)),state)

    def test_aligned_members_decode_to_identical_canonical_json_for_all_padding_residues(self):
        residues = set()
        for size in range(128):
            fragments = [b'{"value":', canonical("x" * size), b"}"]
            residues.add(len(gzip.compress(fragments[1], mtime=0)) % 3)
            members = [checkpoint._member(raw, [str(index)], "value", 0)
                       for index, raw in enumerate(fragments)]
            for encoded, raw in zip(members, fragments):
                self.assertNotIn("=", encoded)
                compressed = base64.b64decode(encoded, validate=True)
                self.assertEqual(len(compressed) % 3, 0)
                self.assertEqual(gzip.decompress(compressed), raw)
            self.assertEqual(gzip.decompress(base64.b64decode("".join(members), validate=True)), b"".join(fragments))
        self.assertEqual(residues, {0, 1, 2})

    def test_legacy_and_new_round_trip_and_restore_keep_all_noncache_evidence(self):
        state = self.state()
        expected = {key: value for key, value in state.items() if key not in checkpoint.REBUILDABLE_KEYS}
        legacy_inline, _ = legacy_checkpoint(expected)
        legacy_parts, legacy_store = legacy_checkpoint(expected, chunk_bytes=80)
        new_inline, new_parts, new_store = staged(state)
        self.assertEqual(new_inline["sha256"], legacy_inline["sha256"])
        for root, store in ((legacy_inline, {}), (legacy_parts, legacy_store),
                            (new_inline, {}), (new_parts, new_store)):
            with self.subTest(schema=root["schema_version"], aligned=root.get("part_layout")):
                hydrated = checkpoint.hydrate_checkpoint(root, store.__getitem__)
                self.assertEqual(checkpoint.decode_checkpoint(hydrated), expected)
                local = {"_runtime": {"revision": 1, "updated_at": "2026-10-06T00:00:00Z"},
                         "wallet_cache": {"keep_local": True}}
                before = copy.deepcopy(local)
                restored, changed = checkpoint.restore_checkpoint(local, hydrated)
                self.assertTrue(changed)
                self.assertEqual(restored["wallet_cache"], local["wallet_cache"])
                self.assertEqual({key: value for key, value in restored.items()
                                  if key not in checkpoint.REBUILDABLE_KEYS}, expected)
                self.assertEqual(local, before)
                self.assertEqual(checkpoint.restore_checkpoint(restored, hydrated), (restored, False))

    def test_empty_and_metadata_absent_round_trip(self):
        for state in ({}, {"_runtime": {}}, {"pools": {}}, {"": {"": None}},
                      {"_a": 1, "_runtime": {"revision": 1}, "z": 2}):
            with self.subTest(state=state):
                payload, root, store = staged(state)
                self.assertEqual(checkpoint.decode_checkpoint(payload), state)
                self.assertEqual(checkpoint.decode_checkpoint(checkpoint.hydrate_checkpoint(root, store.__getitem__)), state)

    def test_identical_values_at_different_paths_do_not_duplicate_part_ids(self):
        _, root, store = staged({"pools": {"a": None, "b": None}, "map": {"a": {}, "b": {}}})
        self.assertEqual(len(root["parts"]), len(store))
        self.assertEqual(len({ref["id"] for ref in root["parts"]}), len(root["parts"]))

    def test_size_count_path_and_metadata_limits_fail_without_truncation(self):
        state = self.state()
        before = copy.deepcopy(state)
        for name, value in (("MAX_DECODED_BYTES", 100), ("MAX_ENCODED_BYTES", 100),
                            ("MAX_PARTS", 2), ("MAX_MANIFEST_BYTES", 50),
                            ("MAX_PATH_BYTES", 2), ("PART_BYTES", 20), ("INLINE_BYTES", 10)):
            with self.subTest(limit=name), patch.object(checkpoint, name, value):
                with self.assertRaises(ValueError):
                    checkpoint.build_checkpoint(state)
                self.assertEqual(state, before)
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                checkpoint.build_checkpoint({"evidence": value})

    def test_manifest_tampering_fails_before_any_fetch(self):
        _, root, _ = staged(self.state())
        for field, value in (("sha256", "0" * 64), ("decoded_bytes", root["decoded_bytes"] + 1),
                             ("runtime", {"revision": 999}), ("parts", list(reversed(root["parts"]))),
                             ("inline_members", [])):
            bad = copy.deepcopy(root)
            bad[field] = value
            fetch = Mock()
            with self.subTest(field=field), self.assertRaises(ValueError):
                checkpoint.hydrate_checkpoint(bad, fetch)
            fetch.assert_not_called()

    def test_resealed_oversized_missing_duplicate_and_reordered_references_fail_closed(self):
        state = self.state()
        state["pools"]["pool-a"]["large"] = "x" * (checkpoint.RECORD_BYTES * 3)
        _, root, store = staged(state)
        cases = []
        bad = copy.deepcopy(root)
        bad["parts"].pop()
        cases.append(bad)
        bad = copy.deepcopy(root)
        bad["parts"][1] = copy.deepcopy(bad["parts"][0])
        cases.append(bad)
        bad = copy.deepcopy(root)
        own = [index for index, ref in enumerate(bad["parts"]) if ref["path"] == ["pools", "p"]]
        bad["parts"][own[0]], bad["parts"][own[1]] = bad["parts"][own[1]], bad["parts"][own[0]]
        cases.append(bad)
        for field, value in (("bytes", checkpoint.PART_BYTES + 1),
                             ("decoded_bytes", checkpoint.RECORD_BYTES + 1), ("index", True)):
            bad = copy.deepcopy(root)
            bad["parts"][0][field] = value
            cases.append(bad)
        bad = copy.deepcopy(root)
        bad["parts"] *= checkpoint.MAX_PARTS
        cases.append(bad)
        for index, bad in enumerate(cases):
            reseal(bad)
            fetch = Mock(side_effect=store.__getitem__)
            with self.subTest(case=index), self.assertRaises(ValueError):
                checkpoint.hydrate_checkpoint(bad, fetch)
            fetch.assert_not_called()

    def test_missing_corrupt_or_mismatched_member_is_not_partially_restored(self):
        _, root, store = staged(self.state())
        fetch = Mock(return_value=None)
        with self.assertRaises(ValueError):
            checkpoint.hydrate_checkpoint(root, fetch)
        with self.assertRaises(KeyError):
            checkpoint.hydrate_checkpoint(root, {}.__getitem__)
        part_id = root["parts"][0]["id"]
        for field, value in (("data", "A" * store[part_id]["encoded_bytes"]),
                             ("sha256", "0" * 64), ("encoded_bytes", store[part_id]["encoded_bytes"] + 1),
                             ("encoding", "json-ascii")):
            corrupted = copy.deepcopy(store)
            corrupted[part_id][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                checkpoint.hydrate_checkpoint(root, corrupted.__getitem__)
        bad = copy.deepcopy(root)
        bad["parts"][0]["raw_sha256"] = "0" * 64
        reseal(bad)
        with self.assertRaises(ValueError):
            checkpoint.hydrate_checkpoint(bad, store.__getitem__)

    def test_member_identity_root_digest_and_inline_metadata_are_verified(self):
        _, root, store = staged(self.state())
        cases = []
        bad = copy.deepcopy(root)
        bad["parts"][0]["path"] = ["forged"]
        cases.append(bad)
        bad = copy.deepcopy(root)
        bad["sha256"] = "0" * 64
        cases.append(bad)
        bad = copy.deepcopy(root)
        bad["runtime"]["revision"] += 1
        cases.append(bad)
        bad = copy.deepcopy(root)
        bad["inline_members"][0]["data"] = "A" * bad["inline_members"][0]["bytes"]
        cases.append(bad)
        bad = copy.deepcopy(root)
        bad["inline_members"][0]["before"] = len(bad["parts"])
        cases.append(bad)
        for index, bad in enumerate(cases):
            reseal(bad)
            with self.subTest(case=index), self.assertRaises(ValueError):
                checkpoint.hydrate_checkpoint(bad, store.__getitem__)

    def test_forged_small_decoded_size_cannot_expand_a_member_unboundedly(self):
        _, root, store = staged({"evidence": "x" * 100000})
        bad = copy.deepcopy(root)
        index = next(index for index, ref in enumerate(bad["parts"]) if ref["path"] == ["evidence"])
        difference = bad["parts"][index]["decoded_bytes"] - 1
        bad["parts"][index]["decoded_bytes"] = 1
        bad["decoded_bytes"] -= difference
        reseal(bad)
        with self.assertRaisesRegex(ValueError, "decoded integrity"):
            checkpoint.hydrate_checkpoint(bad, store.__getitem__)

    def test_unknown_or_removed_layout_fails_before_any_fetch(self):
        _, root, _ = staged(self.state())
        for layout in (None, "future-layout"):
            bad = copy.deepcopy(root)
            if layout is None:
                del bad["part_layout"]
            else:
                bad["part_layout"] = layout
            fetch = Mock()
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                checkpoint.hydrate_checkpoint(bad, fetch)
            fetch.assert_not_called()

    def test_invalid_deflate_or_crc_fails_even_with_matching_transport_digest(self):
        _, root, store = staged({"evidence": "preserve"})
        ref = root["parts"][0]
        original = base64.b64decode(store[ref["id"]]["data"])
        offset = 12 + int.from_bytes(original[10:12], "little")
        for field in ("deflate", "crc"):
            compressed = bytearray(original)
            if field == "deflate":
                compressed[offset] = (compressed[offset] & ~7) | 7
            else:
                compressed[-8] ^= 255
            encoded = base64.b64encode(compressed).decode("ascii")
            changed = {**ref, "id": digest(encoded.encode("ascii"))}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "compressed"):
                checkpoint._read_member(encoded, changed)


if __name__ == "__main__":
    unittest.main()
