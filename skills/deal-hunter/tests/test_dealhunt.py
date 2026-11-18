import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dealhunt as d

HDD = d.load_profile("hdd")

GPU = {
    "name": "gpu-3060", "title": "RTX 3060 12GB", "currency": "$", "budget": 250, "top_n": 10,
    "allowed_conditions": ["new", "used", "refurbished"],
    "reject_keywords": ["for parts", "no display", "mining", "locked"],
    "title_must_match_any": [r"3060(?!\s*ti)"],
    "rules": [{"field": "vram_gb", "op": "gte", "value": 12, "label": "needs 12GB VRAM"}],
    "verify": ["vram_gb", "warranty_days"], "verify_skip_if_new": [],
    "display": [{"label": "VRAM", "field": "vram_gb"}],
    "scoring": {"price_weight": 40, "price_best": 150, "price_worst": 250, "seller_weight": 20,
                "metrics": [{"name": "condition", "field": "condition", "weight": 40,
                             "map": {"new": 1, "refurbished": 0.8, "used": 0.6}}]},
}


def H(**kw):
    base = dict(title="HGST 8TB HDD", platform="ebay", seller="s1", url="https://www.ebay.com/itm/123456789012",
                model="HUH728080ALE600", brand="HGST", capacity_tb=8, form_factor="3.5", condition="used",
                price=80, shipping=0, mfg_year=2021, power_on_hours=9000, ships_ok=True, active=True,
                smart={"reallocated": 0, "pending": 0, "uncorrectable": 0}, health_level="verified",
                seller_rating_pct=99.5, seller_feedback_count=1000, return_days=30, warranty_days=90)
    base.update(kw)
    return base


def G(**kw):
    base = dict(title="MSI RTX 3060 12GB", platform="ebay", seller="g1", url="https://www.ebay.com/itm/555555555555",
                model="RTX 3060", condition="used", price=190, shipping=10, vram_gb=12, ships_ok=True,
                active=True, warranty_days=90, seller_rating_pct=99, seller_feedback_count=300, return_days=30)
    base.update(kw)
    return base


def bad(l, prof=HDD):
    return d.hard_filter(l, prof)


class HddProfile(unittest.TestCase):
    def test_good_passes(self):
        self.assertEqual(bad(H()), [])

    def test_total_includes_shipping(self):
        self.assertTrue(bad(H(price=95, shipping=10)))
        self.assertEqual(bad(H(price=90, shipping=10)), [])

    def test_sas_smr_rpm_via_lookup(self):
        self.assertTrue(bad(H(model="HUH728080AL5200")))
        self.assertTrue(bad(H(model="ST8000DM004")))
        self.assertTrue(bad(H(model="WD80EFZZ")))
        self.assertTrue(bad(H(title="8TB SAS drive")))

    def test_unknown_model_unverified_not_rejected(self):
        l = H(model="XYZ123")
        self.assertEqual(bad(l), [])
        self.assertIn("manufacturer specs", d.unverified_fields(l, HDD))

    def test_datasheet_spec_source_verifies(self):
        l = H(model="XYZ123", interface="SATA", rpm=7200, recording="CMR", spec_source="datasheet")
        self.assertNotIn("manufacturer specs", d.unverified_fields(l, HDD))

    def test_smart_and_hours(self):
        self.assertTrue(bad(H(smart={"reallocated": 8, "pending": 0, "uncorrectable": 0})))
        self.assertTrue(bad(H(power_on_hours=21000)))

    def test_new_drive_exempt_from_smart(self):
        n = H(condition="new", mfg_year=None, power_on_hours=None, smart=None, health_level=None)
        self.assertEqual(d.unverified_fields(n, HDD), [])


class Scoring(unittest.TestCase):
    def test_age_and_hours_separate(self):
        a = d.score_listing(H(mfg_year=2013, power_on_hours=100), HDD)[1]
        b = d.score_listing(H(mfg_year=2023, power_on_hours=10000), HDD)[1]
        self.assertGreater(b["age"], a["age"])
        self.assertGreater(a["power_on_hours"], b["power_on_hours"])

    def test_missing_scores_zero(self):
        p = d.score_listing(H(mfg_year=None, power_on_hours=None, health_level=None), HDD)[1]
        self.assertEqual((p["age"], p["power_on_hours"], p["health"]), (0, 0, 0))

    def test_cheaper_per_tb_wins(self):
        self.assertGreater(d.score_listing(H(price=60), HDD)[1]["price"],
                           d.score_listing(H(price=95), HDD)[1]["price"])

    def test_max_100(self):
        best = H(mfg_year=2024, power_on_hours=100, price=56, return_days=30, warranty_days=365,
                 health_level="verified_long_test")
        self.assertEqual(d.score_listing(best, HDD)[0], 100.0)


class OtherItem(unittest.TestCase):
    """Engine must work for any item, not only HDDs."""

    def test_gpu_passes(self):
        self.assertEqual(bad(G(), GPU), [])

    def test_gpu_variant_rejected(self):
        self.assertTrue(bad(G(title="RTX 3060 Ti 8GB", vram_gb=8), GPU))
        self.assertTrue(bad(G(vram_gb=8), GPU))
        self.assertTrue(bad(G(title="RTX 3060 12GB mining card"), GPU))
        self.assertTrue(bad(G(price=245, shipping=10), GPU))

    def test_gpu_scoring_and_verify(self):
        s, parts = d.score_listing(G(), GPU)
        self.assertLessEqual(s, 100)
        self.assertEqual(set(parts), {"price", "seller", "condition"})
        self.assertEqual(d.unverified_fields(G(vram_gb=None), GPU), ["vram_gb"])

    def test_render_uses_profile_columns(self):
        r = d.run([G()], GPU, {})
        out = d.render(r, GPU, 10)
        self.assertIn("VRAM", out)
        self.assertIn("VERIFIED", out)


class Dedupe(unittest.TestCase):
    def test_same_item_url_variants(self):
        a = H(url="https://www.ebay.com/itm/123456789012?hash=a")
        b = H(url="https://www.ebay.com/itm/Some-Title/123456789012?x=1")
        self.assertEqual(len(d.dedupe([a, b])), 1)

    def test_relist_fingerprint(self):
        a = H(url="https://www.ebay.com/itm/111111111111")
        b = H(url="https://www.ebay.com/itm/222222222222")
        self.assertEqual(len(d.dedupe([a, b])), 1)

    def test_distinct_kept(self):
        a = H(url="https://www.ebay.com/itm/111111111111")
        b = H(url="https://www.ebay.com/itm/222222222222", seller="s2")
        self.assertEqual(len(d.dedupe([a, b])), 2)

    def test_amazon_asin(self):
        self.assertEqual(d.listing_key({"url": "https://www.amazon.com/x/dp/B0ABCDEFGH?tag=1"}), "amazon:B0ABCDEFGH")


class Pipeline(unittest.TestCase):
    def test_split_and_history(self):
        good = H()
        unv = H(url="https://www.ebay.com/itm/333333333333", seller="s3", power_on_hours=None)
        rej = H(url="https://www.ebay.com/itm/444444444444", seller="s4", model="ST8000DM004")
        hist = {}
        r = d.run([good, unv, rej], HDD, hist, today="2026-01-01")
        self.assertEqual((len(r["verified"]), len(r["needs_confirmation"]), len(r["rejected"])), (1, 1, 1))
        r2 = d.run([H(url="https://www.ebay.com/itm/444444444444", seller="s4", model="ST8000DM004")],
                   HDD, hist, today="2026-01-02")
        self.assertEqual(len(r2["skipped_seen_rejected"]), 1)
        self.assertEqual(hist["ebay:444444444444"]["first_seen"], "2026-01-01")

    def test_user_profile_overrides_bundled(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "profiles").mkdir()
            (Path(tmp) / "profiles" / "hdd.json").write_text(json.dumps({"name": "hdd", "title": "mine"}))
            old = d.DATA
            d.DATA = Path(tmp)
            try:
                self.assertEqual(d.load_profile("hdd")["title"], "mine")
            finally:
                d.DATA = old

    def test_unknown_profile_exits(self):
        with self.assertRaises(SystemExit):
            d.load_profile("does-not-exist-xyz")

    def test_profiles_listed(self):
        self.assertIn("hdd", [n for n, _, _ in d.list_profiles()])


if __name__ == "__main__":
    unittest.main()
