#!/usr/bin/env python3
"""Legacy — a private place for Operator's words, and a gentle way for his family to ask.

Two jobs:
  1. CAPTURE (for Operator): a frictionless, PRIVATE diary. Entries are his memories, messages,
     answers to "what would you tell them about X", practical notes — anything. Stored OUTSIDE
     the synced repo (~/.legacy) so it NEVER touches GitHub/iCloud. It is his, only his.
  2. ANSWER (for his family): given a question, find what he wrote that's relevant and share
     it warmly. It does NOT invent him or put words in his mouth — it surfaces HIS actual words.
     If nothing was written on a topic, it says so gently rather than fabricating.

  from core.legacy import add_entry, answer
  add_entry("The day you were born I cried in the parking lot for an hour.", tag="kids")
  answer("what did dad think about my wedding")
"""
from __future__ import annotations
import getpass, hashlib, json, os, re, secrets, urllib.request
from datetime import datetime, timezone
from pathlib import Path

# PRIVATE — outside the repo, never synced. His words stay his.
LEGACY_DIR = Path(os.path.expanduser("~/.legacy"))
DIARY = LEGACY_DIR / "diary.jsonl"
KEY_HASH = LEGACY_DIR / "her_key.hash"   # salted hash of her access key — never the key itself


def set_key():
    """One-time: store a SALTED HASH of her access key (the headphones password). Hidden input,
    nothing logged, plaintext never written to disk."""
    LEGACY_DIR.mkdir(mode=0o700, exist_ok=True)
    k = getpass.getpass("Type the password from her headphones (hidden, stored only as a hash): ")
    if not k.strip():
        print("empty — aborted"); return
    salt = secrets.token_hex(16)
    KEY_HASH.write_text(salt + ":" + hashlib.sha256((salt + k).encode()).hexdigest())
    try:
        os.chmod(KEY_HASH, 0o600)
    except OSError:
        pass
    print("set. Only a hash is stored — the password itself is nowhere on this machine.")


def check_key(k: str) -> bool:
    if not KEY_HASH.exists():
        return False
    try:
        salt, h = KEY_HASH.read_text().strip().split(":", 1)
        return secrets.compare_digest(hashlib.sha256((salt + (k or "")).encode()).hexdigest(), h)
    except Exception:
        return False


def add_entry(text: str, tag: str = "", to: str = "") -> dict:
    """Append one diary entry. `to` = who it's for (her, the kids, a name); `tag` = topic."""
    LEGACY_DIR.mkdir(mode=0o700, exist_ok=True)
    e = {"text": text.strip(), "tag": tag.strip().lower(), "to": to.strip().lower(),
         "ts": datetime.now(timezone.utc).isoformat()}
    with DIARY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
    try:
        os.chmod(DIARY, 0o600)
    except OSError:
        pass
    return e


def _entries() -> list[dict]:
    if not DIARY.exists():
        return []
    out = []
    for line in DIARY.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _relevant(question: str, k: int = 6) -> list[dict]:
    es = _entries()
    if not es:
        return []
    qwords = {w for w in re.findall(r"[a-z']{3,}", question.lower())}
    def score(e):
        blob = (e.get("text", "") + " " + e.get("tag", "") + " " + e.get("to", "")).lower()
        return sum(1 for w in qwords if w in blob)
    ranked = sorted(es, key=lambda e: (score(e), e.get("ts", "")), reverse=True)
    hits = [e for e in ranked if score(e) > 0][:k]
    return hits or es[-k:]   # if nothing matches, offer his most recent words


def _llm_warm(question: str, entries: list[dict]) -> str | None:
    """If a local LLM is reachable, weave his actual words into a gentle answer. Grounded
    ONLY in what he wrote — instructed never to invent him. Degrades to None if unavailable."""
    his_words = "\n\n".join(f'- "{e["text"]}"' for e in entries)
    prompt = (
        "You are gently helping a grieving family member by sharing what their loved one (Operator) "
        "actually wrote in his private diary. Answer their question warmly and simply, using ONLY "
        "his words below. Quote or paraphrase faithfully. NEVER invent memories, opinions, or "
        "messages he did not write. If his words do not cover it, say softly that he didn't write "
        "about that, but offer what he did say. Speak with care.\n\n"
        f"His diary entries:\n{his_words}\n\nTheir question: {question}\n\nGentle answer:"
    )
    try:
        body = json.dumps({"model": os.environ.get("LEGACY_MODEL", "llama3.1"),
                           "prompt": prompt, "stream": False}).encode()
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=40))
        return (d.get("response") or "").strip() or None
    except Exception:
        return None


def answer(question: str) -> dict:
    """Return a warm answer for a family member, grounded in his actual diary."""
    if not DIARY.exists() or not _entries():
        return {"answer": "He hasn't written anything here yet. When he does, his words will be "
                          "right here for you to ask about.", "entries": []}
    entries = _relevant(question)
    warm = _llm_warm(question, entries)
    if warm:
        return {"answer": warm, "entries": entries}
    # no LLM — share his actual words directly (still precious, still real)
    quotes = "\n\n".join(f'He wrote: "{e["text"]}"' for e in entries)
    return {"answer": "Here's what he wrote that feels close to that:\n\n" + quotes, "entries": entries}


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["setkey"]:
        set_key()
    elif len(sys.argv) >= 3 and sys.argv[1] == "add":
        e = add_entry(" ".join(sys.argv[2:]))
        print("saved to your private diary:", e["text"][:60])
    elif len(sys.argv) >= 3 and sys.argv[1] == "ask":
        print(answer(" ".join(sys.argv[2:]))["answer"])
    else:
        print('usage: python -m core.legacy add "your memory"   |   ask "their question"')
