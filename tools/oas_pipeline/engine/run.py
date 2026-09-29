"""End-to-end house generation: homeowner text -> brief -> candidates -> validated, scored design.

    python3 tools/oas_pipeline design "We'd like a one-story ranch ..." -o out/ [--render]
    python3 tools/oas_pipeline design --brief brief.json -o out/        # skip the LLM (replay)

Every stage is timed. The report records AI calls/tokens, deterministic time per stage, validation,
program check, 2D/3D consistency, scores, decisions (AI and solver) and failures. Nothing is fixed
by hand: a stage that fails is reported as a failure.
"""
from __future__ import annotations

import json
import os
import time

from ..exterior.consistency import check_consistency
from ..exterior.model import build_exterior_model
from ..generate import generate, level_extracts
from ..program import check_program
from ..scoring import score
from ..solver import ranch
from ..validate import validate

SOLVERS = {"ranch": ranch}


def program_from_brief(b: dict) -> dict:
    """The homeowner's requirements as an OAS-Program (only what they asked for)."""
    rooms = [{"id": "bedrooms", "usage": "bedroom", "count": b["bedrooms"], "must_have": ["daylight"]},
             {"id": "bathrooms", "usage": "bathroom", "count": int(b["bathrooms"])},
             {"id": "kitchen", "usage": "kitchen", "adjacency": {"must_open_to": ["family"]} if b.get("open_kitchen_family") else {}},
             {"id": "family", "usage": "living"}]
    if b.get("garage"):
        rooms.append({"id": "garage", "usage": "garage", "desired_area_m2": {"min": 18.5 * b["garage"]["cars"]},
                      "must_have": ["exterior_access"]})
    for o in b.get("outdoor", []):
        rooms.append({"id": o["kind"], "usage": o["kind"]})
    t = b["target_conditioned_sf"] / 10.7639
    return {"oas_program": "1.0.0", "plan_id": b.get("plan_id", "design"),
            "global_constraints": {"stories": b.get("stories", 1), "min_area_m2": round(t * 0.9, 1), "max_area_m2": round(t * 1.1, 1)},
            "circulation": {"no_pass_through_usages": ["bedroom"]}, "rooms": rooms}


def design(text: str | None, out_dir: str, brief: dict | None = None, render=False, design_spec=None, log=print) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    rep = {"input": text, "ai": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}, "timing_s": {},
           "decisions": {"ai": [], "solver": []}, "failures": [], "manual_intervention": "none"}
    T = rep["timing_s"]
    t_all = time.perf_counter()

    # 1. intake (AI)
    if brief is None:
        from .intake import run_intake
        brief, usage = run_intake(text)
        rep["ai"] = usage
        T["intake_llm"] = usage["wall_seconds"]
    brief.setdefault("plan_id", "tc_design")
    rep["brief"] = brief
    rep["decisions"]["ai"] = brief.get("assumptions", [])
    if brief.get("questions"):
        rep["questions_for_homeowner"] = brief["questions"]
    json.dump(brief, open(os.path.join(out_dir, "brief.json"), "w"), indent=2)

    # 2. solver: candidates (deterministic)
    t = time.perf_counter()
    solver = SOLVERS.get(brief.get("typology"))
    if solver is None:
        rep["failures"].append(f"no solver for typology {brief.get('typology')!r} yet")
        return finish(rep, out_dir, t_all)
    try:
        cands = solver.candidates(brief)
    except solver.Unsupported as e:
        rep["failures"].append(f"solver: {e}")
        return finish(rep, out_dir, t_all)
    T["solver_enumerate"] = round(time.perf_counter() - t, 3)

    # 3. per candidate: generate -> validate -> program -> exterior -> consistency -> score (deterministic)
    program = program_from_brief(brief)
    results = []
    t = time.perf_counter()
    for i, c in enumerate(cands):
        r = {"index": i, "params": c["params"], "pre_score": c["pre_score"]}
        try:
            doc = generate(c["spec"])
        except Exception as e:  # SpecError from placement etc.
            r["error"] = f"generate: {e}"
            results.append(r)
            continue
        v = validate(doc)
        pc = check_program(doc, program)
        model = build_exterior_model(doc)
        cons, _ = check_consistency(doc, model)
        sc = score(doc, model, brief["target_conditioned_sf"])
        r.update(valid=v.ok and pc.ok and cons.ok, validation_errors=v.errors, validation_warnings=v.warnings,
                 program_errors=pc.errors, consistency_errors=cons.errors, score=sc, doc=doc, spec=c["spec"], model=model)
        results.append(r)
    T["candidates_full_pipeline"] = round(time.perf_counter() - t, 3)
    T["per_candidate_avg"] = round(T["candidates_full_pipeline"] / max(1, len(cands)), 3)

    valid = [r for r in results if r.get("valid")]
    rep["candidates"] = [{"index": r["index"], "valid": r.get("valid", False), "error": r.get("error"),
                          "score": r.get("score", {}).get("total"), "groups": r.get("score", {}).get("groups"),
                          "params": {k: r["params"][k] for k in ("W", "D", "Wm", "Wg", "Wc", "bw", "Dk", "Dmb", "msplit", "mirror")}}
                         for r in results]
    for r in results:
        if not r.get("valid"):
            rep["failures"].append(f"candidate {r['index']}: " + (r.get("error") or "; ".join(
                (r.get("validation_errors") or []) + (r.get("program_errors") or []) + (r.get("consistency_errors") or []))[:400]))
    if not valid:
        rep["failures"].append("no valid candidate")
        return finish(rep, out_dir, t_all)
    best = max(valid, key=lambda r: r["score"]["total"])
    rep["chosen"] = best["index"]
    p = best["params"]
    rep["decisions"]["solver"] = [
        f"typology template: split-bedroom ranch (master wing / great room / kitchen column / kids wing)",
        f"building {p['W']:.1f}' x {p['D']:.1f}' main body; buildable width from lot {brief.get('lot', {}).get('width_ft')}' "
        f"minus {ranch.RULES['site']['side_setback_ft']}' side setbacks (inferred)",
        f"master suite: {'walk-through closet to bath' if p['msplit'] == 'walk_through' else 'bath and closet side by side'}",
        f"garage {p['gw']}' x {p['gd']}' projecting forward at the kids-wing end, entering through the laundry/mud room",
        "covered entry porch added by brain default (covered_entry_by_default)",
        f"{len(cands)} candidates generated, {len(valid)} valid; chose #{best['index']} (score {best['score']['total']})",
        "mirror image offered as the same design for a garage on the other side" if any(c['params']['mirror'] for c in cands) else "",
    ]
    rep["decisions"]["solver"] = [d for d in rep["decisions"]["solver"] if d]

    # 4. outputs
    t = time.perf_counter()
    base = os.path.join(out_dir, brief["plan_id"])
    json.dump(best["spec"], open(base + ".spec.json", "w"), indent=2)
    json.dump(best["doc"], open(base + ".json", "w"), indent=2)
    for suffix, sub in level_extracts(best["doc"]):
        if len(best["doc"].get("levels", [])) > 1:
            json.dump(sub, open(f"{base}_{suffix}.json", "w"), indent=2)
    json.dump(best["model"], open(base + ".exterior.json", "w"), indent=1)
    json.dump(program, open(base + ".program.json", "w"), indent=2)
    T["write_outputs"] = round(time.perf_counter() - t, 3)
    rep["validation"] = {"errors": best["validation_errors"], "warnings": best["validation_warnings"]}
    rep["program_check"] = {"errors": best["program_errors"]}
    rep["consistency"] = {"errors": best["consistency_errors"]}
    rep["score"] = best["score"]
    rep["outputs"] = {"plan": base + ".json", "spec": base + ".spec.json", "exterior_model": base + ".exterior.json"}
    rep["_best"] = best
    return finish(rep, out_dir, t_all)


def finish(rep, out_dir, t_all):
    rep["timing_s"]["total_generation"] = round(time.perf_counter() - t_all, 2)
    det = sum(v for k, v in rep["timing_s"].items() if k not in ("intake_llm", "total_generation", "per_candidate_avg"))
    rep["timing_s"]["deterministic_total"] = round(det, 3)
    best = rep.pop("_best", None)
    json.dump(rep, open(os.path.join(out_dir, "report.json"), "w"), indent=2, default=str)
    rep["_best"] = best
    return rep


__all__ = ["design", "program_from_brief"]
