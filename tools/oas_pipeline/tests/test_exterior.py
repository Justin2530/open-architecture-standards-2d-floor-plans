"""Exterior (3D) model tests: plan -> exterior model -> consistency, on two different houses.

Run from the repository root:  python3 -m unittest discover -s tools/oas_pipeline/tests
"""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(PKG))
sys.path.insert(0, os.path.dirname(PKG))

from oas_pipeline import generate  # noqa: E402
from oas_pipeline.exterior.consistency import check_consistency  # noqa: E402
from oas_pipeline.exterior.model import bbox, build_exterior_model  # noqa: E402

BARNDO = os.path.join(PKG, "examples", "barndominium_40x60")
CABIN = os.path.join(PKG, "examples", "simple_cabin")
FT = 304.8


def load(*parts):
    with open(os.path.join(*parts)) as fh:
        return json.load(fh)


def by_ref(model, ref, kind=None):
    return [e for e in model["elements"] if ref in e["refs"] and (kind is None or e["kind"] == kind)]


class BarndominiumExterior(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = load(ROOT, "svg-viewer", "examples", "barndominium_40x60.json")
        cls.model = build_exterior_model(cls.doc, load(BARNDO, "exterior.json"))

    def test_consistent_with_plan(self):
        rep, _ = check_consistency(self.doc, self.model)
        self.assertEqual(rep.errors, [])

    def test_balcony_and_porch_are_exactly_the_plan_rooms(self):
        for rid, z in (("l1_porch", -102), ("l2_balcony", 3048 - 25)):
            deck = by_ref(self.model, rid, "deck")
            self.assertEqual(len(deck), 1)
            lo, hi = bbox(deck[0])
            self.assertEqual([round(v / FT, 2) for v in (lo[0], lo[1], hi[0], hi[1])], [20.0, 0.0, 60.0, 12.0])
            self.assertEqual(deck[0]["geom"]["points"][0][2], z)

    def test_second_story_is_not_widened(self):
        # The plan's second floor spans x = 20'..60'. No wall-like element may extend it (the old scratch
        # renderer added "attic" walls to x = 80', making the balcony look like 2/3 of the facade).
        for e in self.model["elements"]:
            if e["kind"] in ("wall_panel", "knee_wall", "band") and e.get("level") == "level_02":
                lo, hi = bbox(e)
                self.assertLessEqual(hi[0], 60 * FT + 77, e["id"])
        self.assertFalse([e for e in self.model["elements"] if e["kind"] == "knee_wall"])

    def test_first_floor_walls_keep_plan_height(self):
        for e in self.model["elements"]:
            if e["kind"] == "wall_panel" and e["level"] == "level_01":
                self.assertLessEqual(bbox(e)[1][2], 2743 + 0.5, e["id"])

    def test_balcony_roof_uses_plan_roof_data(self):
        plane = by_ref(self.model, "roof_balcony", "roof_plane")[0]
        zs = sorted({p[2] for p in plane["geom"]["points"]})
        # eave (outer edge) at level_02 + 2743 minus the overhang drop; nothing repositioned
        self.assertAlmostEqual(zs[0], 3048 + 2743 - 305 * 0.08328, delta=2)

    def test_known_plan_roof_issues_are_reported_not_fixed(self):
        codes = {(i["code"], tuple(i["refs"])) for i in self.model["issues"]}
        self.assertIn(("roof_edge_unsupported", ("roof_main",)), codes)
        self.assertIn(("roofs_overlap", ("roof_main", "roof_balcony")), codes)

    def test_every_non_plan_element_is_labelled(self):
        for e in self.model["elements"]:
            self.assertIn(e["class"], ("plan", "derived", "inferred"))
            self.assertTrue(e["rule"])
        kinds = {e["kind"]: e["class"] for e in self.model["elements"]}
        self.assertEqual(kinds["post"], "inferred")
        self.assertEqual(kinds["roof_plane"], "derived")
        self.assertEqual(kinds["opening"], "plan")

    def test_front_is_the_entry_facade(self):
        self.assertEqual(self.model["front"]["normal"], [0, -1])
        self.assertIn("l1_d_front", self.model["front"]["rule"])
        self.assertEqual(set(self.model["views"]), {"front", "rear", "left", "right", "perspective", "aerial"})


class PlanChangesPropagate(unittest.TestCase):
    """Editing the floor-plan spec must change the 3D model with no manual 3D edits."""

    def rebuild(self, edit):
        spec = load(BARNDO, "spec.json")
        edit(spec)
        doc = generate(spec)
        model = build_exterior_model(doc, load(BARNDO, "exterior.json"))
        rep, _ = check_consistency(doc, model)
        self.assertEqual(rep.errors, [])
        return doc, model

    def test_moving_a_window_moves_it_in_3d(self):
        def edit(spec):
            next(o for o in spec["openings"] if o["id"] == "l2_win_bed3_n")["offset"] += 2
        _, before = self.rebuild(lambda s: None)
        _, after = self.rebuild(edit)
        a = by_ref(before, "l2_win_bed3_n", "opening")[0]["geom"]["origin"]
        b = by_ref(after, "l2_win_bed3_n", "opening")[0]["geom"]["origin"]
        self.assertAlmostEqual(abs(b[0] - a[0]), 2 * FT, delta=1)

    def test_shallower_balcony_changes_deck_posts_and_roof(self):
        def edit(spec):
            for r in spec["rooms"]:
                if r["id"] in ("l1_porch", "l2_balcony"):
                    r["rect"] = [20, -8, 60, 0]
            for s in spec["floor_slabs"]:
                if s["id"] in ("slab_l1_porch", "slab_l2_balcony"):
                    s["rect"] = [20, -8, 60, 0]
            next(r for r in spec["railings"] if r["id"] == "l2_rail_balcony")["points"] = [[20, 0], [20, -8], [60, -8], [60, 0]]
            next(r for r in spec["roofs"] if r["id"] == "roof_balcony")["rect"] = [20, -8, 60, 0]
        doc, model = self.rebuild(edit)
        deck = by_ref(model, "l2_balcony", "deck")[0]
        lo, hi = bbox(deck)
        self.assertAlmostEqual((hi[1] - lo[1]) / FT, 8, places=2)
        for p in [e for e in model["elements"] if e["kind"] == "post"]:
            plo, phi = bbox(p)
            self.assertGreaterEqual(plo[1], lo[1] - 1)
            self.assertLessEqual(phi[1], hi[1] + 1)


class ConsistencyCatchesDivergence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = load(ROOT, "svg-viewer", "examples", "barndominium_40x60.json")
        cls.model = build_exterior_model(cls.doc)

    def errors(self, fn, audit=None):
        m = copy.deepcopy(self.model)
        fn(m)
        rep, _ = check_consistency(self.doc, m, audit)
        return " | ".join(rep.errors)

    def test_moved_balcony(self):
        def mv(m):
            d = by_ref(m, "l2_balcony", "deck")[0]
            for p in d["geom"]["points"]:
                p[1] -= 600
        self.assertIn("[balconies] l2_balcony", self.errors(mv))

    def test_raised_balcony(self):
        def up(m):
            for p in by_ref(m, "l2_balcony", "deck")[0]["geom"]["points"]:
                p[2] += 150
        self.assertIn("[balconies] l2_balcony", self.errors(up))

    def test_missing_window(self):
        def drop(m):
            m["elements"] = [e for e in m["elements"] if "l2_win_bed3_n" not in e["refs"]]
        self.assertIn("l2_win_bed3_n", self.errors(drop))

    def test_shifted_wall(self):
        def shift(m):
            for e in by_ref(m, "l2_w_north_3", "wall_panel"):
                for p in e["geom"]["points"]:
                    p[1] += 100
        self.assertIn("[exterior walls] l2_w_north_3", self.errors(shift))

    def test_wrong_floor_height(self):
        self.assertIn("[floor heights] level_02", self.errors(lambda m: m["levels"][1].update(elevation_mm=3200)))

    def test_widened_story_is_flagged(self):
        doc = self.doc
        model = build_exterior_model(doc, {"infer_roof_bearing_walls": True})
        rep, _ = check_consistency(doc, model)
        self.assertEqual(rep.errors, [])
        self.assertTrue(any("visually extends this story" in w for w in rep.warnings))
        self.assertTrue(all(e["class"] == "inferred" for e in model["elements"] if e["kind"] == "knee_wall"))

    def test_renderer_that_moves_an_element_fails_audit(self):
        audit = {"elements": [{"id": e["id"], "min": list(bbox(e)[0]), "max": list(bbox(e)[1])} for e in self.model["elements"]]}
        self.assertEqual(self.errors(lambda m: None, audit), "")
        bad = copy.deepcopy(audit)
        post = next(a for a in bad["elements"] if a["id"].startswith("deck"))
        post["min"][0] += 300
        post["max"][0] += 300
        self.assertIn("[renderer audit]", self.errors(lambda m: None, bad))


class CabinExterior(unittest.TestCase):
    """Second, unrelated house: single level, metric, no roof in the plan."""

    def test_default_roof_is_inferred_and_consistent(self):
        doc = generate(load(CABIN, "spec.json"))
        model = build_exterior_model(doc)
        rep, _ = check_consistency(doc, model)
        self.assertEqual(rep.errors, [])
        roofs = [e for e in model["elements"] if e["kind"] == "roof_plane"]
        self.assertTrue(roofs)
        self.assertTrue(all(e["class"] == "inferred" for e in roofs))
        self.assertTrue(any(i["code"] == "roof_inferred" for i in model["issues"]))
        self.assertEqual(model["front"]["normal"], [0, -1])


if __name__ == "__main__":
    unittest.main()
