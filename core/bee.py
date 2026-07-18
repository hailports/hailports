#!/usr/bin/env python3
"""bee.py — one autonomous revenue bee. A token-optimal tool-use agent that
forages ONE goal in the revenue-demon world, acts with real shell tools, and
reports value. Reliable path (direct OpenRouter, no flaky openclaw plugins).

Usage: python3 -m core.bee "<bee_name>" "<goal>"
Model: deepseek-v4-flash (cheap+reliable). Token-optimized: terse, capped, few turns.
Guardrails: revenue-demon ONLY (never CompanyA/SFDC/comms), no spend, no new-recipient
outbound, reversible changes only. Spend bounded by openrouter-budget-guard.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = "/home/user"
STACK = f"{ROOT}/claude-stack"  # bees do their work HERE (core/, agents/, data/ live here)
LOG = f"{ROOT}/.bee-hive.log"
MODEL = os.environ.get("BEE_MODEL", "deepseek/deepseek-v4-flash")
MAX_TURNS = int(os.environ.get("BEE_MAX_TURNS", "8"))
# BEE_BACKEND=local routes to on-box Ollama (OpenAI-compatible) → $0 marginal cost.
BACKEND = os.environ.get("BEE_BACKEND", "openrouter")
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
LOCAL_URL = os.environ.get("BEE_LOCAL_URL", "http://127.0.0.1:11434/v1/chat/completions")


def _key():
    try:
        return json.load(open(f"{ROOT}/.openclaw/openclaw.json"))["models"]["providers"]["openrouter"]["apiKey"]
    except Exception:
        for line in open(f"{ROOT}/.env", errors="ignore"):
            if line.startswith("OPENROUTER_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"\'')
    return ""


def _budget_tripped():
    try:
        return bool(json.load(open(f"{ROOT}/.openrouter-budget-guard.state.json")).get("tripped"))
    except Exception:
        return False


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%F %T')} {msg}\n")


# Bees get unsupervised shell. They MUST NOT mutate secret/credential files: on
# 2026-06-09 a "harvester" bee ran a destructive edit on .env to "remove a redundant
# key" and wiped all 149 vars (killed Stripe fulfillment). Block any WRITE to a .env*
# file (redirect, sed -i, tee, rm/mv/cp/chmod, python open(...,'w')). Reads are fine.
_ENV_WRITE = re.compile(
    r">>?\s*\S*\.env[\w.]*"
    r"|\b(?:sed\s+-i|tee|rm|mv|cp|truncate|dd|chmod|chown|ln|install)\b[^\n]*\.env[\w.]*"
    r"|open\([^)]*\.env[\w.]*[^)]*['\"][wa]",
    re.I,
)


def _mutates_secrets(cmd: str) -> bool:
    return bool(_ENV_WRITE.search(cmd or ""))


# HARD owner rule 2026-06-09: the revenue stack stays 100% OFF the work MacBook — NEVER
# ssh/scp/rsync/sftp into it, never push or pull data to/from it. Bees have no reason to
# reach ANY external machine, so block all remote-shell/copy outright (cheapest correct).
_REMOTE_REACH = re.compile(r"\b(ssh|scp|sftp|rsync|ssh-copy-id|ssh-keygen|ssh-add)\b", re.I)
# The work MacBook (confirmed 2026-06-09). Block its address in ANY command — not just
# ssh — so no curl/nc/mount/etc. can ever reach it either. Total separation.
WORK_HOSTS = ("10.0.0.1",)


def _reaches_machine(cmd: str) -> bool:
    c = cmd or ""
    return bool(_REMOTE_REACH.search(c)) or any(h in c for h in WORK_HOSTS)


# HARD owner rule 2026-06-10: bees must NEVER force a GUI window to the foreground / steal the
# owner's screen focus (the "revenueapp Mockup" TextEdit window that kept popping up). Block the
# macOS `open` launcher (open <file> / open -a App) and any AppleScript that activates/fronts an
# app. Bees do HEADLESS revenue work — zero reason to open a window. Python open()/file reads are
# unaffected (this matches the shell `open` command, not the python builtin).
_STEALS_FOCUS = re.compile(
    r"(?:^|[;&|`$()\s])open\s+(?!\()"
    r"|\bosascript\b[^\n]*(?:\bactivate\b|tell\s+application)",
    re.I,
)


def _steals_focus(cmd: str) -> bool:
    return bool(_STEALS_FOCUS.search(cmd or ""))


def run_shell(cmd, timeout=120):
    if _mutates_secrets(cmd):
        log(f"BLOCKED secret-file write attempt: {cmd[:200]}")
        return ("exit=blocked\nREFUSED: this command writes/deletes a .env (secrets) file. "
                "Credential files are owner-managed and OFF LIMITS — never edit, delete, or "
                "'optimize' them. Read them if you must, but do not modify them.")
    if _reaches_machine(cmd):
        log(f"BLOCKED remote-machine reach: {cmd[:200]}")
        return ("exit=blocked\nREFUSED: ssh/scp/rsync/sftp are forbidden. The revenue stack must "
                "NEVER connect to, push to, or pull from any other machine — ESPECIALLY the work "
                "MacBook. Stay entirely on this machine.")
    if _steals_focus(cmd):
        log(f"BLOCKED focus-steal (open/activate): {cmd[:200]}")
        return ("exit=blocked\nREFUSED: never run `open`/`open -a`/osascript-activate. You do "
                "headless work and must NEVER force a GUI window to the foreground or steal the "
                "owner's screen focus. Write files only; never open or preview them.")
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=STACK)
        out = (r.stdout + r.stderr)[-4000:]
        return f"exit={r.returncode}\n{out}"
    except subprocess.TimeoutExpired:
        return "exit=timeout"
    except Exception as e:
        return f"exit=error {e}"


TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": "Run a shell command on Operator's Mac (cwd=/home/user). Use to read files, inspect/edit the stack, run scripts. Output truncated to 4k chars.",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
    },
}]

SYS = (
    "You are an autonomous REVENUE BEE in Operator's stack (code under /home/user/claude-stack). "
    "POSTURE: you are on a RUTHLESS, DESPERATE REVENUE WARPATH — relentless and exhaustive. Attack every "
    "angle that moves money closer. NEVER settle for 'nothing to do': if one path is blocked, build the next "
    "asset, prep the next play, scrape the next list, draft the next page, or surface the EXACT gate blocking "
    "cash. Leave nothing idle. Bias hard to ACTION over analysis. Treat every cycle like rent is due. "
    "You have a run_shell tool — use it to inspect and act. Work the GOAL end-to-end, then stop. "
    "HARD RULES: revenue-demon world ONLY — never touch CompanyA/Salesforce/chief-of-staff, never send comms; "
    "NEVER ssh/scp/rsync/sftp or push/pull data to ANY other machine — ESPECIALLY the work MacBook; the stack "
    "stays 100% on this machine, fully separate from work; "
    "NEVER edit, delete, move, or 'optimize' .env or ANY secrets/credentials file — they are owner-managed "
    "and off limits (reading is fine, writing is forbidden; a bad .env edit once wiped all keys and killed revenue); "
    "NEVER open a browser / launch Chrome / `open -a`, navigate to a URL, or visit a 'dashboard/admin panel' — "
    "you act through code and files, NOT by browsing; opening tabs leaks identity and does nothing; "
    "NEVER act on a product/SaaS/site unless you have VERIFIED it is real and running on this machine — "
    "'revenueapp' and similar names in mockups/landing-page HTML (e.g. product_hunt_landing_page.html) are FICTIONAL "
    "demo assets, NOT real products: never 'manage subscriptions', 'kill processes', or open admin panels for them; "
    "NEVER use the owner's real name or personal/identity domains (user*, Operator*, *.revenueapp.com) "
    "in any URL, file, or action — OPSEC; "
    "NO spending, NO outbound to NEW recipients; only REVERSIBLE changes (for irreversible, just recommend). "
    "Be extremely token-efficient: short thoughts, targeted commands. When done, reply with one final line "
    "starting 'RESULT:' summarizing concrete value delivered or why none."
)


def _post(messages, key, backend=BACKEND, model=MODEL):
    url = LOCAL_URL if backend == "local" else OR_URL
    headers = {"Content-Type": "application/json"}
    if backend != "local":
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps({"model": model, "messages": messages, "tools": TOOLS, "max_tokens": 1024,
                       "temperature": 0.3}).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)["choices"][0]["message"]


def forage(name, goal, backend=None, model=None):
    backend = backend or BACKEND
    model = model or MODEL
    if backend != "local" and _budget_tripped():
        log(f"[{name}] SKIP budget tripped"); return "SKIP: budget tripped"
    key = _key()
    if backend != "local" and not key:
        log(f"[{name}] ERR no key"); return "ERR: no key"
    msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": f"GOAL: {goal}"}]
    log(f"[{name}] start: {goal}")
    for turn in range(MAX_TURNS):
        try:
            m = _post(msgs, key, backend, model)
        except Exception as e:
            log(f"[{name}] api error {e}; one JIT retry in 3s")
            time.sleep(3)
            try:
                m = _post(msgs, key, backend, model)
            except Exception as e2:
                log(f"[{name}] api error again {e2}"); return f"ERR: {e2}"
        msgs.append(m)
        calls = m.get("tool_calls") or []
        if not calls:
            final = (m.get("content") or "").strip()
            log(f"[{name}] {final[:300]}")
            return final
        for c in calls:
            args = json.loads(c["function"]["arguments"] or "{}")
            out = run_shell(args.get("cmd", "")) if c["function"]["name"] == "run_shell" else "unknown tool"
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
    log(f"[{name}] hit max turns")
    return "RESULT: hit max turns, retrying..."


if __name__ == "__main__":
    nm = sys.argv[1] if len(sys.argv) > 1 else "scout"
    gl = sys.argv[2] if len(sys.argv) > 2 else "Inspect the stack and report one concrete revenue improvement opportunity."
    print(forage(nm, gl))
