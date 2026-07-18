#!/usr/bin/env python3
"""research_bee.py — cheap research generator. Turns a brief file into a written
report using a CHEAP model (deepseek-flash via OpenRouter) — no Opus, no Codex.
Knowledge-grade synthesis on the cheap tier; chunked so output isn't truncated.

Usage: python3 -m core.research_bee <brief_path> <out_path> [model]
Reusable for ANY research brief (GTM, hustle taxonomy, competitor scan, etc.).
"""
import json
import sys
import urllib.request

sys.path.insert(0, "/home/user/claude-stack")
from core.bee import _key, log  # noqa: E402

MODEL = "deepseek/deepseek-v4-flash"
SYS = ("You are a world-class growth/GTM and business-research analyst. Produce "
       "EXHAUSTIVE, concrete, example-rich, immediately-actionable output. Follow the "
       "brief's required output FORMAT exactly (including any machine-readable blocks). "
       "Cite real operators/companies + results where possible. No fluff, no disclaimers.")


def _post(messages, key, model, max_tokens=8000):
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens, "temperature": 0.5}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    ch = d["choices"][0]
    return ch["message"]["content"], ch.get("finish_reason")


def run(brief_path, out_path, model=MODEL, max_chunks=5):
    key = _key()
    if not key:
        log("[research_bee] ERR no key"); print("ERR: no key"); return
    brief = open(brief_path).read()
    msgs = [{"role": "system", "content": SYS},
            {"role": "user", "content": brief + "\n\nWrite the COMPLETE report now, in full."}]
    out = []
    log(f"[research_bee] start {brief_path} -> {out_path} ({model})")
    for i in range(max_chunks):
        try:
            content, fr = _post(msgs, key, model)
        except Exception as e:
            log(f"[research_bee] api err chunk{i}: {e}"); break
        out.append(content)
        msgs.append({"role": "assistant", "content": content})
        # incremental write so nothing is lost if a later chunk fails
        open(out_path, "w").write("".join(out))
        if fr != "length":
            break
        msgs.append({"role": "user", "content": "Continue exactly where you left off. Do not repeat anything."})
    text = "".join(out)
    log(f"[research_bee] DONE {len(text)} chars -> {out_path}")
    print(f"wrote {len(text)} chars -> {out_path}")


if __name__ == "__main__":
    brief = sys.argv[1] if len(sys.argv) > 1 else "/home/user/claude-stack/GTM_BRIEF.md"
    out = sys.argv[2] if len(sys.argv) > 2 else "/home/user/claude-stack/GTM_PLAYBOOK.md"
    model = sys.argv[3] if len(sys.argv) > 3 else MODEL
    run(brief, out, model)
