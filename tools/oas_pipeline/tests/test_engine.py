"""Design engine tests: homeowner brief -> solver -> validated, scored design (no LLM; briefs are replayed).

TEST CASE #2 is the recorded intake output for examples/tc2_ranch/homeowner.txt. The other briefs vary
the program and the lot to show the engine is not tuned to one house; programs outside the solver
template must fail with a clear reason, never with a broken design.

Run from the repository root:  python3 -m unittest discover -s tools/oas_pipeline/tests
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

from oas_pipeline.engine.run import design  # noqa: E402
from oas_pipeline.generate import generate  # noqa: E402
from oas_pipeline.solver import ranch  # noqa: E402

TC2 = os.path.join(PKG, "examples", "tc2_ranch")


def load(*parts):
    with open(os.path.join(*parts)) as fh:
        return json.load(fh)


def brief(**kw):
    b = {"typology": "ranch", "stories": 1, "target_conditioned_sf": 1800, "bedrooms": 3, "bathrooms": 2,
         "master_separate_from_other_bedrooms": True, "open_kitchen_family": True, "kitchen_island": "standard",
         "garage": {"cars": 2, "attached": True, "position": "front"}, "outdoor": [], "lot": {"width_ft": 70},
         "assumptions": [], "questions": []}
    b.update(kw)
    return b


def run(b):
    with tempfile.TemporaryDirectory() as d:
        return design(None, d, brief=b, log=lambda *a: None)


class TestCase2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rep = run(load(TC2, "brief.json"))

    def test_valid_without_intervention(self):
        r = self.rep
        self.assertEqual(r["failures"], [])
        self.assertEqual(r["manual_intervention"], "none")
        self.assertEqual(r["validation"]["errors"], [])
        self.assertEqual(r["program_check"]["errors"], [])
        self.assertEqual(r["consistency"]["errors"], [])
        self.assertTrue(all(c["valid"] for c in r["candidates"]))

    def test_scored_on_interior_and_exterior(self):
        s = self.rep["score"]
        self.assertGreaterEqual(s["total"], 88)
        self.assertEqual(set(s["groups"]), {"interior", "exterior"})
        self.assertAlmostEqual(s["conditioned_sf"], 1800, delta=90)

    def test_deterministic_replay_matches_recorded_design(self):
        self.assertEqual(self.rep["_best"]["doc"], load(TC2, "tc_design.json"))

    def test_brief_holds_no_geometry(self):
        b = load(TC2, "brief.json")
        numbers = [v for v in json.dumps({k: v for k, v in b.items() if k not in ("assumptions", "questions")}) if v.isdigit()]
        self.assertTrue(numbers)  # only counts/area/lot the homeowner stated (checked by schema below)
        self.assertLessEqual(set(b), {"typology", "stories", "target_conditioned_sf", "bedrooms", "bathrooms",
                                      "master_separate_from_other_bedrooms", "open_kitchen_family", "kitchen_island",
                                      "garage", "outdoor", "lot", "style", "assumptions", "questions", "plan_id"})
        spec = load(TC2, "tc_design.spec.json")
        self.assertEqual(spec["walls"], "derive")
        self.assertTrue(all("wall" not in o and "position_mm" not in o for o in spec["openings"]))

class Generalization(unittest.TestCase):
    def test_small_two_bed_no_garage_narrow_lot(self):
        r = run(brief(target_conditioned_sf=1400, bedrooms=2, garage=None, lot={"width_ft": 60}))
        self.assertEqual(r["failures"], [])
        self.assertEqual(r["_best"]["params"]["gw"], 0)

    def test_three_car_garage_large_house(self):
        r = run(brief(target_conditioned_sf=2200, kitchen_island="large", garage={"cars": 3, "attached": True, "position": "front"},
                      outdoor=[{"kind": "patio", "location": "rear", "covered": False}], lot={"width_ft": 90}))
        self.assertEqual(r["failures"], [])
        doc = r["_best"]["doc"]
        self.assertEqual(sum(1 for o in doc["openings"] if o["id"].startswith("d_garage_vehicle")), 3)

    def test_no_lot_width_given(self):
        r = run(brief(lot={"width_ft": None}))
        self.assertEqual(r["failures"], [])

    def test_unsupported_programs_fail_cleanly(self):
        r = run(brief(bedrooms=4))
        self.assertTrue(any("2-3 bedrooms" in f for f in r["failures"]))
        r = run(brief(lot={"width_ft": 50}))
        self.assertTrue(any("needs at least" in f for f in r["failures"]), r["failures"])
        with self.assertRaises(ranch.Unsupported):
            ranch.check_brief(brief(bathrooms=2.5))


class DerivedWalls(unittest.TestCase):
    def test_walls_follow_rooms(self):
        doc = generate(load(TC2, "tc_design.spec.json"))
        walls = doc["walls"]
        self.assertTrue(walls)
        self.assertTrue(all(w.get("derived_from") for w in walls))
        self.assertEqual({w["thickness_mm"] for w in walls}, {152, 114})  # exterior and interior
        for w in walls:  # open-plan pairs get no wall between them
            self.assertFalse({"family", "kitchen"} <= set(w["adjacent_rooms"]), w["id"])
        exterior = [w for w in walls if "exterior" in w["adjacent_rooms"]]
        self.assertTrue(all(w["thickness_mm"] == 152 for w in exterior))

if __name__ == "__main__":
    unittest.main()
