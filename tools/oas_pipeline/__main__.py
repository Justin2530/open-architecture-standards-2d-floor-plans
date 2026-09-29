"""Command line entry point.

    python3 tools/oas_pipeline build    SPEC -o OUT_DIR [--program PROGRAM] [--render]
    python3 tools/oas_pipeline generate SPEC -o OUT_DIR
    python3 tools/oas_pipeline validate PLAN [--program PROGRAM] [--json]
    python3 tools/oas_pipeline render   PLAN -o OUT_DIR
    python3 tools/oas_pipeline exterior PLAN -o OUT_DIR [--design EXTERIOR.json] [--render]
    python3 tools/oas_pipeline design   "homeowner text" -o OUT_DIR [--render]   (or --brief BRIEF.json)

Exit status is non-zero when validation or the program check reports errors.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

if __package__ in (None, ""):  # executed as `python3 tools/oas_pipeline ...`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "oas_pipeline"

from oas_pipeline.generate import SpecError, load_spec, write_outputs  # noqa: E402
from oas_pipeline.program import check_program  # noqa: E402
from oas_pipeline.validate import validate  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER = os.path.join(os.path.dirname(os.path.dirname(HERE)), "svg-viewer", "index.html")


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def run_checks(plan_path, program_path=None, as_json=False):
    doc = _load(plan_path)
    rep = validate(doc)
    result = {"plan": plan_path, "validation": rep.to_dict()}
    ok = rep.ok
    if program_path:
        prog = check_program(doc, _load(program_path))
        result["program"] = prog.to_dict()
        ok = ok and prog.ok
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"== validation: {plan_path}")
        print(rep.format())
        if program_path:
            print(f"\n== program check: {program_path}")
            print(prog.format())
    return ok


def render(plan_path, out_dir):
    node = shutil.which("node")
    if not node:
        sys.exit("render needs Node.js and the 'playwright' package")
    env = dict(os.environ)
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
        env["NODE_PATH"] = os.pathsep.join(filter(None, [env.get("NODE_PATH"), root]))
    except OSError:
        pass
    os.makedirs(out_dir, exist_ok=True)
    subprocess.run([node, os.path.join(HERE, "render.mjs"), VIEWER, os.path.abspath(plan_path), os.path.abspath(out_dir)],
                   check=True, env=env)


def exterior(plan_path, out_dir, design_path=None, do_render=False, as_json=False):
    """Plan -> exterior model -> consistency check (-> render -> renderer audit re-check)."""
    from oas_pipeline.exterior.consistency import check_consistency, format_table
    from oas_pipeline.exterior.model import build_exterior_model

    doc = _load(plan_path)
    model = build_exterior_model(doc, _load(design_path) if design_path else None)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(plan_path))[0]
    model_path = os.path.join(out_dir, f"{base}.exterior.json")
    with open(model_path, "w") as fh:
        json.dump(model, fh, indent=1)
    print("wrote", model_path)
    audit = None
    if do_render:
        node = shutil.which("node")
        if not node:
            sys.exit("rendering needs Node.js; run `npm install` in tools/oas_pipeline/exterior")
        env = dict(os.environ)
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
        env["NODE_PATH"] = os.pathsep.join(filter(None, [env.get("NODE_PATH"), root]))
        subprocess.run([node, os.path.join(HERE, "exterior", "render.mjs"), model_path, out_dir], check=True, env=env)
        audit = _load(os.path.join(out_dir, "audit.json"))
    rep, table = check_consistency(doc, model, audit)
    report = {"plan": plan_path, "model": model_path, "consistency": rep.to_dict(), "table": table}
    with open(os.path.join(out_dir, f"{base}.consistency.json"), "w") as fh:
        json.dump(report, fh, indent=1)
    text = format_table(table) + "\n\n" + rep.format()
    with open(os.path.join(out_dir, f"{base}.consistency.txt"), "w") as fh:
        fh.write(text + "\n")
    print(json.dumps(report, indent=1) if as_json else text)
    return rep.ok


def run_design(a):
    import time
    from oas_pipeline.engine.run import design
    from oas_pipeline.scoring import format_score
    rep = design(a.text, a.out, brief=_load(a.brief) if a.brief else None)
    best = rep.get("_best")
    if best and a.render:
        t = time.perf_counter()
        render(rep["outputs"]["plan"], a.out)
        rep["timing_s"]["render_floor_plans"] = round(time.perf_counter() - t, 2)
        t = time.perf_counter()
        env = dict(os.environ)
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
        env["NODE_PATH"] = os.pathsep.join(filter(None, [env.get("NODE_PATH"), root]))
        subprocess.run([shutil.which("node"), os.path.join(HERE, "exterior", "render.mjs"), rep["outputs"]["exterior_model"], a.out],
                       check=True, env=env)
        rep["timing_s"]["render_exterior_views"] = round(time.perf_counter() - t, 2)
        from oas_pipeline.exterior.consistency import check_consistency
        cons, _ = check_consistency(best["doc"], best["model"], _load(os.path.join(a.out, "audit.json")))
        rep["consistency_with_renderer_audit"] = {"errors": cons.errors}
    rep.pop("_best", None)
    with open(os.path.join(a.out, "report.json"), "w") as fh:
        json.dump(rep, fh, indent=2, default=str)
    print(json.dumps({k: rep.get(k) for k in ("ai", "timing_s", "decisions", "failures", "questions_for_homeowner",
                                               "validation", "program_check", "consistency", "consistency_with_renderer_audit", "candidates")},
                     indent=1, default=str))
    if rep.get("score"):
        print(format_score(rep["score"]))
    return 0 if best else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="oas_pipeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="generate + validate (+ program check, + render)")
    b.add_argument("spec"); b.add_argument("-o", "--out", required=True)
    b.add_argument("--program"); b.add_argument("--render", action="store_true")
    g = sub.add_parser("generate", help="compile a design spec to OAS-Layout JSON")
    g.add_argument("spec"); g.add_argument("-o", "--out", required=True)
    v = sub.add_parser("validate", help="validate an OAS-Layout JSON file")
    v.add_argument("plan"); v.add_argument("--program"); v.add_argument("--json", action="store_true")
    r = sub.add_parser("render", help="screenshot each level in svg-viewer (needs Node + Playwright)")
    r.add_argument("plan"); r.add_argument("-o", "--out", required=True)
    x = sub.add_parser("exterior", help="build the 3D exterior model, check it against the plan, optionally render views")
    x.add_argument("plan"); x.add_argument("-o", "--out", required=True)
    x.add_argument("--design", help="exterior design spec (materials, inferred-detail parameters)")
    x.add_argument("--render", action="store_true"); x.add_argument("--json", action="store_true")
    dz = sub.add_parser("design", help="homeowner text -> brief (1 LLM call) -> solver -> validated, scored design")
    dz.add_argument("text", nargs="?"); dz.add_argument("-o", "--out", required=True)
    dz.add_argument("--brief", help="use this brief instead of calling the LLM (replay)")
    dz.add_argument("--render", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd in ("build", "generate"):
        try:
            paths = write_outputs(load_spec(a.spec), a.out)
        except SpecError as e:
            sys.exit(f"spec error: {e}")
        print("wrote", *paths, sep="\n  ")
        if a.cmd == "generate":
            return 0
        ok = run_checks(paths[0], a.program)
        if a.render:
            render(paths[0], a.out)
        return 0 if ok else 1
    if a.cmd == "validate":
        return 0 if run_checks(a.plan, a.program, a.json) else 1
    if a.cmd == "render":
        render(a.plan, a.out)
        return 0
    if a.cmd == "design":
        return run_design(a)
    if a.cmd == "exterior":
        return 0 if exterior(a.plan, a.out, a.design, a.render, a.json) else 1


if __name__ == "__main__":
    sys.exit(main())
