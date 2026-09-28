"""Tests for the OAS floor-plan pipeline.

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

from oas_pipeline import SpecError, check_program, generate, level_extracts, validate  # noqa: E402

BARNDO = os.path.join(PKG, "examples", "barndominium_40x60")
CABIN = os.path.join(PKG, "examples", "simple_cabin")
VIEWER_EXAMPLES = os.path.join(ROOT, "svg-viewer", "examples")


def load(*parts):
    with open(os.path.join(*parts)) as fh:
        return json.load(fh)


class BarndominiumRegression(unittest.TestCase):
    """The committed viewer example must be exactly what the spec compiles to."""

    @classmethod
    def setUpClass(cls):
        cls.spec = load(BARNDO, "spec.json")
        cls.doc = generate(cls.spec)

    def test_combined_document_matches_committed_example(self):
        committed = load(VIEWER_EXAMPLES, "barndominium_40x60.json")
        self.assertEqual(json.dumps(self.doc), json.dumps(committed))

    def test_level_extracts_match_committed_examples(self):
        for suffix, sub in level_extracts(self.doc):
            committed = load(VIEWER_EXAMPLES, f"barndominium_40x60_{suffix}.json")
            self.assertEqual(json.dumps(sub), json.dumps(committed), suffix)

    def test_validates_clean(self):
        rep = validate(self.doc)
        self.assertEqual(rep.errors, [])
        self.assertEqual(rep.warnings, [])

    def test_meets_program(self):
        rep = check_program(self.doc, load(BARNDO, "program.json"))
        self.assertEqual(rep.errors, [], rep.format())


class CabinExample(unittest.TestCase):
    """A second, unrelated spec (metric, single level) goes through the same pipeline."""

    def test_generates_validates_and_meets_program(self):
        doc = generate(load(CABIN, "spec.json"))
        self.assertEqual(validate(doc).errors, [])
        self.assertEqual(check_program(doc, load(CABIN, "program.json")).errors, [])
        self.assertEqual(level_extracts(doc)[0][1]["rooms"], doc["rooms"])

    def test_derives_adjacency_and_swing(self):
        doc = generate(load(CABIN, "spec.json"))
        wall = next(w for w in doc["walls"] if w["id"] == "w_mid_1")
        self.assertEqual(sorted(wall["adjacent_rooms"]), ["bedroom", "living"])
        door = next(o for o in doc["openings"] if o["id"] == "d_bedroom")
        # w_mid_1 runs south->north, so the bedroom (east) is on its right: the leaf swings "outward".
        self.assertEqual(door["swing_direction"], "outward")
        self.assertEqual(door["connects_rooms"], ["living", "bedroom"])


class ValidatorCatchesFaults(unittest.TestCase):
    """Each injected fault must produce a specific error."""

    @classmethod
    def setUpClass(cls):
        cls.base = generate(load(BARNDO, "spec.json"))

    def mutate(self, fn):
        doc = copy.deepcopy(self.base)
        fn(doc)
        return " | ".join(validate(doc).errors)

    @staticmethod
    def opening(doc, oid):
        return next(o for o in doc["openings"] if o["id"] == oid)

    def test_missing_door_makes_room_unreachable(self):
        errs = self.mutate(lambda d: d["openings"].remove(self.opening(d, "l1_d_powder")))
        self.assertIn("not reachable from exterior: l1_powder", errs)

    def test_missing_railing_leaves_void_unguarded(self):
        errs = self.mutate(lambda d: d["railings"].remove(next(r for r in d["railings"] if r["id"] == "l2_rail_loft_overlook")))
        self.assertIn("unguarded open edge", errs)

    def test_opening_past_wall_end(self):
        errs = self.mutate(lambda d: self.opening(d, "l1_d_linen").update(position_along_wall_mm=900))
        self.assertIn("outside wall", errs)

    def test_door_swing_wider_than_room(self):
        # The master-suite entry swings into a 4'-0" (1219 mm) hall; a 1300 mm leaf cannot fit.
        errs = self.mutate(lambda d: self.opening(d, "l1_d_master_entry").update(width_mm=1300))
        self.assertIn("l1_d_master_entry: swing leaves l1_master_hall", errs)
        self.assertIn("l1_d_master_entry: swing hits a wall", errs)

    def test_misaligned_stair(self):
        def shift(d):
            room = next(r for r in d["rooms"] if r["id"] == "l2_stair")
            for p in room["boundary_polygon"]["points"]:
                p["y"] += 300
        errs = self.mutate(shift)
        self.assertIn("overlap", errs)
        self.assertIn("no matching footprint", errs)

    def test_bedroom_without_window(self):
        errs = self.mutate(lambda d: d["openings"].remove(self.opening(d, "l2_win_bed3_n")))
        self.assertIn("l2_bed3: no exterior window", errs)

    def test_non_integer_coordinate(self):
        errs = self.mutate(lambda d: d["walls"][0]["from"].update(x=0.5))
        self.assertIn("non-integer", errs)

    def test_wrong_adjacent_rooms(self):
        errs = self.mutate(lambda d: d["walls"][0].update(adjacent_rooms=["l1_kitchen", "exterior"]))
        self.assertIn("adjacent_rooms", errs)


class ProgramCatchesUnmetRequirements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = generate(load(BARNDO, "spec.json"))
        cls.program = load(BARNDO, "program.json")

    def check(self, fn):
        prog = copy.deepcopy(self.program)
        fn(prog)
        return " | ".join(check_program(self.doc, prog).errors)

    def req(self, prog, rid):
        return next(r for r in prog["rooms"] if r["id"] == rid)

    def test_pantry_too_small(self):
        errs = self.check(lambda p: self.req(p, "pantry")["desired_area_m2"].update(min=20))
        self.assertIn("below min", errs)

    def test_missing_bedroom(self):
        errs = self.check(lambda p: self.req(p, "upstairs_bedrooms").update(count=3))
        self.assertIn("needs 3 room(s)", errs)

    def test_open_concept_required(self):
        errs = self.check(lambda p: self.req(p, "pantry").setdefault("adjacency", {}).update(must_open_to=["kitchen"]))
        self.assertIn("not open (wall-less)", errs)

    def test_balcony_size(self):
        errs = self.check(lambda p: self.req(p, "balcony").update(exact_dims_m=[4.0, 12.192]))
        self.assertIn("required", errs)


class SpecErrors(unittest.TestCase):
    def test_swing_into_non_adjacent_room_is_rejected(self):
        spec = load(CABIN, "spec.json")
        next(o for o in spec["openings"] if o["id"] == "d_bedroom")["swing_into"] = "kitchen"
        with self.assertRaises(SpecError):
            generate(spec)

    def test_unknown_wall_is_rejected(self):
        spec = load(CABIN, "spec.json")
        spec["openings"][0]["wall"] = "nope"
        with self.assertRaises(SpecError):
            generate(spec)

    def test_roof_arrays_must_match_edges(self):
        spec = load(CABIN, "spec.json")
        spec["roofs"] = [{"id": "r", "level": "level_01", "rect": [0, 0, 9.5, 4], "slope_angles": [30, 0, 30]}]
        with self.assertRaises(SpecError):
            generate(spec)


if __name__ == "__main__":
    unittest.main()
