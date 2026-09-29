"""Intake: homeowner text -> house brief (one small LLM call).

This is the only AI step on the free path. The model interprets intent and fills in a small JSON brief.
It never produces dimensions the homeowner did not state, room coordinates, or layouts. Everything
geometric is decided by the deterministic solver from brain rules.

The call goes through the Claude Code CLI in headless mode (``claude -p``, no tools, one turn,
our own system prompt) because that is what this environment provides. In production the same
prompt would go to the Messages API directly. The CLI adds its own context tokens, which are
reported separately.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

SYSTEM = """You convert a homeowner's plain-English house request into a JSON house brief for a deterministic floor-plan solver.

Rules:
- Output ONLY one JSON object, no prose, no code fences.
- Never invent dimensions, coordinates or room sizes. Only copy numbers the homeowner stated (total square feet, lot width, garage car count, bedroom/bathroom counts).
- Choose "typology" from: "ranch" (one story), "barndominium", "two_story_traditional".
- Record every interpretation of the homeowner's words that they did not state explicitly in "assumptions" (short phrases about requirements, e.g. "big island read as large island"). Never put layout, placement, orientation or sizes in assumptions: the solver decides those.
- "bathrooms": count full bathrooms; a half bath counts 0.5.
- Put a question in "questions" only if a genuine preference is missing that changes the design fundamentally (e.g. basement or no basement). Do not ask about room sizes or layout details. Usually "questions" is empty.

Schema:
{
  "typology": "ranch" | "barndominium" | "two_story_traditional",
  "stories": integer,
  "target_conditioned_sf": number,
  "bedrooms": integer,
  "bathrooms": number,
  "master_separate_from_other_bedrooms": boolean,
  "open_kitchen_family": boolean,
  "kitchen_island": "none" | "standard" | "large",
  "garage": null | {"cars": integer, "attached": boolean, "position": "front" | "side" | "rear"},
  "outdoor": [{"kind": "patio" | "porch" | "deck" | "balcony", "location": "front" | "rear" | "side", "covered": boolean}],
  "lot": {"width_ft": number | null},
  "style": string | null,
  "assumptions": [string],
  "questions": [string]
}"""

SCHEMA_KEYS = {"typology", "stories", "target_conditioned_sf", "bedrooms", "bathrooms", "kitchen_island", "garage", "outdoor", "lot"}


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"intake returned no JSON: {text[:200]}")
    return json.loads(m.group(0))


def run_intake(text: str, model: str = "claude-haiku-4-5-20251001", timeout: int = 180, thinking: bool = False) -> tuple[dict, dict]:
    """Return (brief, usage). usage has calls, tokens, cost and latency as reported by the CLI."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("intake needs the `claude` CLI (or replace with a direct Messages API call)")
    t0 = time.perf_counter()
    proc = subprocess.run([exe, "-p", text, "--model", model, "--output-format", "json", "--max-turns", "1",
                           "--tools", "", "--system-prompt", SYSTEM], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL,
                          # extraction needs no extended thinking: it cost ~3k tokens and ~30 s in the first run
                          env={**os.environ, **({} if thinking else {"MAX_THINKING_TOKENS": "0"})})
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"intake call failed: {proc.stderr[:500] or proc.stdout[:500]}")
    out = json.loads(proc.stdout)
    brief = parse_json(out["result"])
    missing = SCHEMA_KEYS - set(brief)
    if missing:
        raise ValueError(f"intake brief missing {sorted(missing)}")
    u = out.get("usage", {})
    usage = {"calls": 1, "model": model,
             "input_tokens": u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
             "output_tokens": u.get("output_tokens", 0),
             "thinking_tokens": (u.get("output_tokens_details") or {}).get("thinking_tokens", 0),
             "cost_usd": out.get("total_cost_usd"),
             "api_seconds": round(out.get("duration_api_ms", 0) / 1000, 2), "wall_seconds": round(wall, 2),
             "prompt_chars": len(SYSTEM) + len(text), "response_chars": len(out["result"])}
    return brief, usage


__all__ = ["SYSTEM", "run_intake", "parse_json"]
