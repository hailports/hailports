#!/usr/bin/env python3
"""Agent-quality eval harness — certify the subagent fleet instead of asserting it's good.

Pattern mined from wshobson/agents' plugin-eval (static analysis + LLM judge; NO foreign code — rebuilt).
Two layers:
  STATIC (fast, deterministic) — frontmatter completeness, description trigger-quality, body depth,
    master-template section coverage. Scores 0-1.
  JUDGE (--judge, local $0 via Ollama) — a local model rates each agent's prompt on clarity, scope
    boundaries, actionability, and trigger specificity, 0-1, and flags the weak ones.

CLI:
  python3 tools/agent_eval.py                 # static pass over the whole fleet + summary
  python3 tools/agent_eval.py --judge         # + local-LLM quality score (slower)
  python3 tools/agent_eval.py --dir <path>    # eval a specific dir
  python3 tools/agent_eval.py --min 0.8       # non-zero exit if any agent scores below --min (CI gate)
"""
from __future__ import annotations
import argparse, json, os, re, sys, urllib.request
from pathlib import Path

HOME = Path.home()
DEFAULT_DIRS = [HOME/".claude"/"agents", HOME/"claude-stack"/".claude"/"agents"]
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
JUDGE_MODELS = ["qwen3:14b", "qwen2.5:7b"]


def parse_agent(path: Path) -> dict:
    txt = path.read_text()
    fm = {}
    body = txt
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", txt, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1); fm[k.strip()] = v.strip()
        body = m.group(2)
    return {"path": path, "fm": fm, "body": body, "raw": txt}


def static_score(a: dict) -> tuple[float, list]:
    fm, body = a["fm"], a["body"]
    issues, pts, total = [], 0.0, 0.0
    def check(cond, weight, label):
        nonlocal pts, total
        total += weight
        if cond: pts += weight
        else: issues.append(label)
    desc = fm.get("description", "")
    check(bool(fm.get("name")), 1, "missing name")
    check(len(desc) >= 200, 2, f"thin description ({len(desc)}c)")
    check(bool(re.search(r"use (proactively|when|this)", desc, re.I)), 2, "no trigger language in description")
    check(bool(fm.get("tools")), 1, "no tools declared")
    check(bool(fm.get("model")), 1, "no model declared")
    check(len(body) >= 3000, 3, f"shallow body ({len(body)}c)")
    check(len(re.findall(r"^##\s", body, re.M)) >= 4, 2, "few/no template sections")
    check(bool(re.search(r"\bnot for\b|out of scope|do not use|don'?t use", body, re.I)), 1, "no scope boundary (what it's NOT for)")
    return round(pts/total, 3) if total else 0.0, issues


_JUDGE = """Rate this AI subagent DEFINITION on quality for autonomous use. Score each 0-1:
- clarity: is the role + method unambiguous?
- scope: are boundaries (what it does / does NOT do) clear?
- actionability: would it produce concrete deliverables, not vague advice?
- trigger: is it clear WHEN to invoke it (vs other agents)?
Return STRICT JSON only: {"clarity":x,"scope":x,"actionability":x,"trigger":x,"overall":x,"weakest":"<one phrase>"}
No prose.

AGENT (first 6000 chars):
{agent}
"""


def _judge_model():
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=5) as r:
            avail = {m["name"] for m in json.loads(r.read()).get("models", [])}
    except Exception:
        return None
    for m in JUDGE_MODELS:
        if m in avail or any(x.startswith(m.split(":")[0]) for x in avail):
            return m
    return None


def judge(a: dict, model: str) -> dict:
    body = json.dumps({"model": model, "prompt": _JUDGE.replace("{agent}", a["raw"][:6000]),
                       "stream": False, "options": {"temperature": 0.0, "num_ctx": 8192}}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = re.sub(r"<think>.*?</think>", "", json.loads(r.read()).get("response", ""), flags=re.S)
    m = re.search(r"\{.*\}", out, re.S)
    return json.loads(m.group(0)) if m else {}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", action="append")
    p.add_argument("--judge", action="store_true")
    p.add_argument("--min", type=float, default=0.0)
    p.add_argument("--limit-judge", type=int, default=0, help="judge only first N (proof/sample)")
    args = p.parse_args()
    dirs = [Path(d) for d in args.dir] if args.dir else DEFAULT_DIRS
    files = sorted({f for d in dirs if d.exists() for f in d.rglob("*.md") if f.name.lower() != "readme.md"})
    if not files:
        print("no agent .md files found in", [str(d) for d in dirs]); sys.exit(1)
    jm = _judge_model() if args.judge else None
    rows, failing = [], []
    judged = 0
    for f in files:
        a = parse_agent(f)
        s, issues = static_score(a)
        row = {"agent": f.stem, "static": s, "issues": issues}
        if args.judge and jm and (args.limit_judge == 0 or judged < args.limit_judge):
            try:
                j = judge(a, jm); row["judge"] = round(float(j.get("overall", 0) or 0), 3); row["weakest"] = j.get("weakest", "")
            except Exception as e:
                row["judge"] = None; row["weakest"] = f"judge-err:{e}"[:40]
            judged += 1
        rows.append(row)
        if s < args.min:
            failing.append(row)
    rows.sort(key=lambda r: r["static"])
    print(f"{'agent':44}{'static':>7}{'judge':>7}  issues / weakest")
    for r in rows:
        j = "" if r.get("judge") is None else f"{r['judge']:.2f}"
        note = "; ".join(r["issues"][:2]) or r.get("weakest", "") or "clean"
        print(f"  {r['agent'][:42]:42}{r['static']:>7.2f}{j:>7}  {note[:60]}")
    avg = sum(r["static"] for r in rows)/len(rows)
    print(f"\n{len(rows)} agents | avg static {avg:.2f}" + (f" | judged {judged} on {jm.split('/')[-1] if jm else 'n/a'}" if args.judge else ""))
    if args.min and failing:
        print(f"BELOW --min {args.min}: {', '.join(r['agent'] for r in failing)}"); sys.exit(1)


if __name__ == "__main__":
    main()
