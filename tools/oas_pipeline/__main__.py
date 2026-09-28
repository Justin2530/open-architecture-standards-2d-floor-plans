"""Command line entry point.

    python3 tools/oas_pipeline build    SPEC -o OUT_DIR [--program PROGRAM] [--render]
    python3 tools/oas_pipeline generate SPEC -o OUT_DIR
    python3 tools/oas_pipeline validate PLAN [--program PROGRAM] [--json]
    python3 tools/oas_pipeline render   PLAN -o OUT_DIR

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


if __name__ == "__main__":
    sys.exit(main())
