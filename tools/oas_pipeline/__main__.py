"""Command line entry point.

    python3 tools/oas_pipeline build    SPEC -o OUT_DIR [--program PROGRAM] [--render]
    python3 tools/oas_pipeline generate SPEC -o OUT_DIR
    python3 tools/oas_pipeline validate PLAN [--program PROGRAM] [--json]
    python3 tools/oas_pipeline render   PLAN -o OUT_DIR
    python3 tools/oas_pipeline exterior PLAN -o OUT_DIR [--design EXTERIOR.json] [--render]

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
    if a.cmd == "exterior":
        return 0 if exterior(a.plan, a.out, a.design, a.render, a.json) else 1


if __name__ == "__main__":
    sys.exit(main())
