"""Tests for TOUBuckets and TOUResult dataclasses.

Phase 4 of direction-b-dataclasses: replaces dict returns from
_aggregate() and _get_tou_model() with typed dataclasses.
"""

import unittest
from datetime import datetime, timezone

from energy_aggregator import TOUBuckets
from metrics import TOUResult


class TestTOUBuckets(unittest.TestCase):
    """Tests for TOUBuckets dataclass."""

    def test_construction_all_fields(self):
        """TOUBuckets can be constructed with all fields."""
        b = TOUBuckets(total=100.0, peak=30.0, part_peak=20.0, off_peak=50.0)
        self.assertEqual(b.total, 100.0)
        self.assertEqual(b.peak, 30.0)
        self.assertEqual(b.part_peak, 20.0)
        self.assertEqual(b.off_peak, 50.0)

    def test_default_zero(self):
        """TOUBuckets defaults to all zeros."""
        b = TOUBuckets()
        self.assertEqual(b.total, 0.0)
        self.assertEqual(b.peak, 0.0)
        self.assertEqual(b.part_peak, 0.0)
        self.assertEqual(b.off_peak, 0.0)

    def test_to_dict_matches_original_structure(self):
        """to_dict() output matches the original _aggregate return dict."""
        b = TOUBuckets(total=200.0, peak=50.0, part_peak=60.0, off_peak=90.0)
        d = b.to_dict()
        self.assertEqual(d["total"], 200.0)
        self.assertEqual(d["peak"], 50.0)
        self.assertEqual(d["part_peak"], 60.0)
        self.assertEqual(d["off_peak"], 90.0)

    def test_to_dict_has_all_expected_keys(self):
        """to_dict() contains all 4 keys."""
        b = TOUBuckets()
        self.assertEqual(set(b.to_dict().keys()), {
            "total", "peak", "part_peak", "off_peak",
        })

    def test_roundtrip_to_dict(self):
        """TOUBuckets(**b.to_dict()) roundtrips."""
        original = TOUBuckets(total=100.0, peak=25.0, part_peak=35.0, off_peak=40.0)
        restored = TOUBuckets(**original.to_dict())
        self.assertEqual(original, restored)

    def test_is_frozen(self):
        """TOUBuckets is frozen (immutable)."""
        import dataclasses
        b = TOUBuckets()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            b.total = 999.0


class TestTOUResult(unittest.TestCase):
    """Tests for TOUResult dataclass."""

    def test_construction(self):
        """TOUResult can be constructed with TOUBuckets and nbc."""
        buckets = TOUBuckets(total=100.0, peak=30.0, part_peak=20.0, off_peak=50.0)
        r = TOUResult(buckets=buckets, nbc=500.0)
        self.assertIs(r.buckets, buckets)
        self.assertEqual(r.nbc, 500.0)

    def test_to_dict_matches_original_structure(self):
        """to_dict() output matches the original _get_tou_model return dict."""
        buckets = TOUBuckets(total=200.0, peak=50.0, part_peak=60.0, off_peak=90.0)
        r = TOUResult(buckets=buckets, nbc=1000.0)
        d = r.to_dict()
        self.assertEqual(d["buckets"]["total"], 200.0)
        self.assertEqual(d["buckets"]["peak"], 50.0)
        self.assertEqual(d["buckets"]["part_peak"], 60.0)
        self.assertEqual(d["buckets"]["off_peak"], 90.0)
        self.assertEqual(d["nbc"], 1000.0)

    def test_to_dict_has_all_expected_keys(self):
        """to_dict() contains all expected keys."""
        buckets = TOUBuckets()
        r = TOUResult(buckets=buckets, nbc=0.0)
        self.assertEqual(set(r.to_dict().keys()), {"buckets", "nbc"})

    def test_is_frozen(self):
        """TOUResult is frozen (immutable)."""
        import dataclasses
        buckets = TOUBuckets()
        r = TOUResult(buckets=buckets, nbc=0.0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            r.nbc = 999.0


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
