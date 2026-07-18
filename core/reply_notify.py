#!/usr/bin/env python3
"""Reusable reply-draft notifier — ONE rollup into Operator's own "zoom drafts" channel + a push backstop.

Both reply bots (the Zoom reply loop and the Outlook reply loop) call `notify(items)` after they
stage a draft, so Operator gets a single, durable "these drafts are waiting for your tap" pointer plus a
phone push — instead of each bot inventing its own alert.

item schema (one dict per staged draft):
    {
      "surface":     "zoom" | "outlook",
      "state":       "teed" | "held",                # REQUIRED for zoom items: teed = VERIFIED in the
                                                     # thread composer; held = review-queue only. A zoom
                                                     # item with no/unknown state renders as HELD (never
                                                     # a "tap the thread" link for an unverified draft).
      "target_name": "Vijay Krishna Janapala"        # person/thread the draft is for
      "thread_link": "<clickable link>"  (optional)  # see per-surface link findings below
      "summary":     "re: chandra profile"           # short context (for outlook: the subject)
      "draft_text":  "..."                           # held items: the FULL draft text (shown inline)
      "reason":      "SF-work/blast-radius"          # held items: why it was held
    }

WHAT ACTUALLY WORKS AS A "JUMP TO THE DRAFT" LINK (researched + tested live 2026-07-13):

  ZOOM  — the web client SPA is NOT URL-addressable (every thread is https://app.zoom.us/wc/team-chat;
          /wc/<jid>/chat and ?jid=<jid> variants load blank). Zoom's native per-message "Copy link"
          DOES support 1:1 DMs (v5.11.0+) but its format is undocumented and the control isn't
          exposable to our headless automation. The one clickable deep-link that targets a thread by
          its stable jid is the APP-LAUNCH endpoint:
              https://zoom.us/launch/chat?jid=<jid>
          It renders "Opening Zoom…" and hands off to the installed Zoom desktop/mobile app on the
          thread — so on Operator's phone/desktop it opens that exact chat. (In a no-Zoom-app env it only
          shows the launcher.) If a caller passes no thread_link, we synthesize this from the jid when
          the item carries one, else fall back to a plain "→ open <name>" reference. Every DM Operator has
          a staged draft on ALSO shows Zoom's native red [Draft] badge on all his synced devices, so
          the thread name alone is already a one-tap affordance.

  OUTLOOK — the native Mac Outlook bridge (tools/outlook_app.create_draft) returns only a LOCAL
          AppleScript integer id; there is no public URL scheme (ms-outlook:// / outlook:) that opens
          a specific Mac Outlook message by it, and we don't get an EWS/immutable id to build an OWA
          per-draft deeplink. So: use a caller-supplied OWA deeplink if it has one; otherwise fall
          back to the OWA Drafts-folder link (https://outlook.office.com/mail/drafts — opens Operator's
          Drafts list, one step from the draft) plus a clear "in Outlook Drafts: RE:<subject>" ref.

ZOOM-ONLY (Operator's explicit call). There is NO iMessage / phone push / alert_gateway in this path. The
notification is two Zoom surfaces: (1) the rollup posted to Operator's own "zoom drafts" channel (the durable
pointer) and (2) a native Zoom "Remind Me" set ON that rollup post, which is the actual in-app ping —
Zoom won't push-notify a self-post, but a reminder does, and it covers BOTH held + teed drafts.

SAFETY. The "zoom drafts" channel post is a REAL send, which the zoom send path hard-gates to a human.
This module posts via zoom_web_send.send_message's self_notify path: the sid must equal the module-const
SELF_NOTIFY_SIDS allowlist inside zoom_web_send (exactly Operator's own "zoom drafts" channel) and the opened
header must token-set-EQUAL the channel name — enforced in the send primitive itself, not here. This
module never touches os.environ and never fakes human arming (2026-07-13 hardening, finding #7), so the
human gate for colleague threads is structurally untouched. Both surfaces are best-effort/fail-soft.
Nothing here ever sends to a colleague thread.
"""
from __future__ import annotations
import sys, json, argparse, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Operator's OWN notify surface — the ONLY thread this module may ever send to (hard pin).
DRAFTS_CHANNEL_JID = "user@example.com"
DRAFTS_CHANNEL_NAME = "zoom drafts"
OWA_DRAFTS_FOLDER = "https://outlook.office.com/mail/drafts"
ZOOM_LAUNCH = "https://zoom.us/launch/chat?jid="
ALEX_TAG = "@Operator"  # textual tag; the push backstop is the real device alert

# Sits-too-long nudge (Operator 2026-07-13): re-ping an UNACTIONED draft at aging tiers, DEDUPED so the same
# rollup never re-posts every cycle — only on a NEW draft or a tier escalation. "Age" = elapsed time since
# a draft was first surfaced here; a draft that gets sent/dismissed/thread-advanced simply stops appearing
# in `items` and is pruned from state. Zoom-only, no iMessage. Elapsed-tier proxy for "before you log off".
_NUDGE_STATE = ROOT / "data" / "runtime" / "reply_nudge_state.json"
_TIER_SECONDS = (3600, 10800, 28800)  # ~1h, ~3h, ~8h


def _zoom_link(item: dict) -> str:
    link = (item.get("thread_link") or "").strip()
    if link:
        return link
    jid = (item.get("jid") or item.get("sid") or "").strip()
    if jid and "@" in jid:
        return ZOOM_LAUNCH + jid
    return ""


def _outlook_link(item: dict) -> str:
    link = (item.get("thread_link") or "").strip()
    return link or OWA_DRAFTS_FOLDER


def _line(item: dict) -> str:
    surface = (item.get("surface") or "").lower()
    state = (item.get("state") or "").lower()
    name = (item.get("target_name") or "?").strip()
    summ = (item.get("summary") or "").strip()

    # ZOOM — branch on the draft's ACTUAL state. A notification must NEVER point Operator at a thread as if a
    # draft is sitting there unless a tee is VERIFIED landed. (Bug: a HELD SF-work draft was announced with
    # the same "tap the thread" language as a teed one; Operator opened the thread to an empty composer.)
    if surface == "zoom" and state == "teed":
        # VERIFIED in the thread composer (ledger entry present) -> the deep-link is honest: open + send.
        link = _zoom_link(item)
        tail = link if link else f"→ open {name}  (red [Draft] badge on your DM)"
        ctx = f" ({summ})" if summ else ""
        return f"📌 draft's in your {name} composer — tap to review + send{ctx}\n   {tail}"
    if surface == "zoom" and state == "held":
        # In the REVIEW QUEUE only — nothing in the thread to open. Show the reason + FULL text inline so
        # Operator can act on it, and DO NOT emit a thread deep-link that implies a ready draft.
        reason = (item.get("reason") or "review").strip()
        text = (item.get("draft_text") or "").strip()
        body = "\n".join("      " + ln for ln in text.splitlines()) if text else "      (no draft text)"
        return (f"⏸️ needs your call → {name}: I drafted a reply but HELD it b/c {reason}. "
                f"nothing's teed into the thread — review + send from the queue:\n"
                f"   draft:\n{body}")

    if surface == "outlook":
        subj = summ or name
        link = _outlook_link(item)
        tail = link if link != OWA_DRAFTS_FOLDER else f"in Outlook Drafts: RE:{subj}  ({link})"
        return f"📧 email draft ready → RE:{subj} to {name}\n   {tail}"

    # NO/UNKNOWN state (finding #11): fail SAFE — nothing verified a tee landed, so NEVER imply a
    # ready in-thread draft or emit a thread deep-link. Render as HELD: text inline when we have it,
    # loud pointer at the review queue when we don't.
    text = (item.get("draft_text") or "").strip()
    body = ("\n".join("      " + ln for ln in text.splitlines()) if text
            else "      (no draft text in the item — find it in the review queue)")
    ctx = f" ({summ})" if summ else ""
    return (f"⏸️ needs your call → {name}{ctx}: draft state UNVERIFIED (item missing 'state') — "
            f"treating it as HELD; nothing's confirmed in the thread:\n   draft:\n{body}")


def compose(items: list[dict]) -> str:
    """The ONE rollup text posted to the drafts channel (tags Operator; the push carries the same)."""
    items = [i for i in (items or []) if i]
    if not items:
        return ""
    head = f"{ALEX_TAG} {len(items)} reply draft{'s' if len(items) != 1 else ''} — teed ones are in-thread, held ones show the text here:"
    return head + "\n" + "\n".join(_age_badge(int(i.get("_age_tier") or 0)) + _line(i) for i in items)


def _post_to_drafts_channel(text: str) -> dict:
    """Send the rollup into Operator's own 'zoom drafts' channel — HARD-PINNED to that one jid.
    Hardened (finding #7): this NEVER touches os.environ and never fakes human arming. It passes
    self_notify=True, and zoom_web_send.send_message itself hard-compares the sid to its module-const
    SELF_NOTIFY_SIDS allowlist ({the drafts channel}) + token-set-equality-verifies the opened header
    before its one unarmed Enter — so this path structurally cannot reach a colleague thread, and the
    human gate for colleague sends is untouched."""
    try:
        from tools.zoom_web_send import send_message
    except Exception as e:
        return {"ok": False, "reason": f"zoom_web_send import failed: {e}"}
    res = send_message(text, sid=DRAFTS_CHANNEL_JID, name=None, do_send=True,
                       verify_contains=DRAFTS_CHANNEL_NAME, self_notify=True)
    res["ok"] = bool(res.get("sent"))
    return res


def _remind_on_drafts_channel() -> dict:
    """PRIMARY notification (Zoom-only, per Operator): set a native Zoom 'Remind Me' ON the zoom-drafts
    channel's latest message (the rollup we just posted). Zoom will NOT push-notify a self-post, so this
    in-app reminder is the one ping that actually reaches Operator — and it covers BOTH held + teed drafts
    (a per-thread reminder would miss held drafts, which never tee). NO iMessage / phone push /
    alert_gateway anywhere in this path. Fail-soft."""
    try:
        from tools.zoom_web_send import set_thread_reminder
        return set_thread_reminder(DRAFTS_CHANNEL_JID, DRAFTS_CHANNEL_NAME)
    except Exception as e:
        return {"reminded": False, "detail": f"set_thread_reminder failed: {e}"}


def _age_tier(age_s: float) -> int:
    """0 = fresh; 1 = ~1h; 2 = ~3h; 3 = ~8h (before-you-log-off)."""
    t = 0
    for i, thr in enumerate(_TIER_SECONDS, start=1):
        if age_s >= thr:
            t = i
    return t


def _age_badge(tier: int) -> str:
    return {0: "", 1: "⏰ waiting ~1h · ", 2: "⏰ waiting ~3h · ",
            3: "⏰ still waiting — before you log off · "}.get(tier, "")


def _load_nudge_state() -> dict:
    try:
        return json.loads(_NUDGE_STATE.read_text())
    except Exception:
        return {}


def _save_nudge_state(state: dict) -> None:
    try:
        _NUDGE_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _NUDGE_STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(_NUDGE_STATE)  # atomic
    except Exception:
        pass


def _item_key(item: dict) -> str:
    """Stable per-draft identity across cycles: the thread jid/sid if present, else surface+who+summary."""
    k = (item.get("jid") or item.get("sid") or "").strip()
    if k:
        return k
    return "|".join([(item.get("surface") or "").lower(),
                     (item.get("target_name") or "").strip().lower(),
                     (item.get("summary") or "").strip().lower()])


def _apply_aging(items: list[dict]) -> bool:
    """Stamp each item with its _age_tier from persisted first-seen time, prune drafts that are gone
    (sent/dismissed/advanced), and return whether we should NUDGE this cycle — True iff a draft is NEW
    or crossed into a higher aging tier since last nudge. Deduped: an unchanged set returns False so the
    same rollup is not re-posted every cycle."""
    now = time.time()
    state = _load_nudge_state()
    keys_now, escalate = set(), False
    for it in items:
        k = _item_key(it)
        keys_now.add(k)
        ent = state.get(k)
        if ent is None:
            ent = {"first": now, "tier": 0}
            state[k] = ent
            escalate = True  # brand-new draft
        tier = _age_tier(now - float(ent.get("first", now)))
        it["_age_tier"] = tier
        if tier > int(ent.get("tier", 0)):
            ent["tier"] = tier
            escalate = True  # aged into a new tier -> re-nudge
    for k in list(state):
        if k not in keys_now:
            del state[k]  # draft handled -> stop nudging it
    _save_nudge_state(state)
    return escalate


def notify(items: list[dict], issue_key: str | None = None, dry_run: bool = False,
           post_channel: bool = True) -> dict:
    """Compose ONE rollup for `items`, post it to the 'zoom drafts' channel, and set a native Zoom
    'Remind Me' on that post so Zoom pings Operator in-app to check it. ZOOM-ONLY — no iMessage / phone push
    / alert_gateway. Sits-too-long nudge: re-pings an unactioned draft at aging tiers (~1h/~3h/~8h) with an
    escalating badge, and DEDUPES — if nothing is new and no draft aged into a higher tier, it does NOT
    re-post (no 20-min spam). Returns {ok, rollup, channel, reminder, nudged} or {ok, skipped}. dry_run
    composes + returns without touching state or sending. `issue_key` accepted for back-compat, ignored."""
    items = [i for i in (items or []) if i]
    if not items:
        return {"ok": False, "reason": "no items"}
    if dry_run:
        for it in items:
            it.setdefault("_age_tier", 0)
        return {"ok": True, "dry_run": True, "rollup": compose(items),
                "would_post_to": DRAFTS_CHANNEL_NAME, "would_remind": True}
    escalate = _apply_aging(items)          # stamps _age_tier + prunes handled + decides re-nudge
    rollup = compose(items)
    if not rollup:
        return {"ok": False, "reason": "no items"}
    if not escalate:
        # nothing new + no draft crossed a new aging tier -> don't re-post the same rollup (dedup).
        return {"ok": True, "skipped": "no change since last nudge", "rollup": rollup}
    channel = _post_to_drafts_channel(rollup) if post_channel else {"skipped": True}
    # PRIMARY ping: Zoom-native reminder ON the drafts-channel post (a self-post won't notify on its own).
    reminder = _remind_on_drafts_channel() if post_channel else {"skipped": True}
    return {"ok": True, "rollup": rollup, "channel": channel, "reminder": reminder, "nudged": True}


def _cli():
    ap = argparse.ArgumentParser(description="Post a reply-draft rollup notification + push backstop")
    ap.add_argument("--items", default="", help="JSON list of {surface,target_name,thread_link,summary}")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-channel", action="store_true", help="push only, skip the channel post")
    ap.add_argument("--issue-key", default=None)
    ap.add_argument("--selftest", action="store_true", help="dry compose of a sample zoom+outlook rollup")
    a = ap.parse_args()
    if a.selftest:
        items = [
            {"surface": "zoom", "state": "teed", "target_name": "Vijay Krishna Janapala",
             "thread_link": ZOOM_LAUNCH + "user@example.com",
             "summary": "re: chandra profile"},
            {"surface": "zoom", "state": "held", "target_name": "Alevato, Felipe R.",
             "reason": "SF-work/blast-radius",
             "draft_text": "quick q before i build this -- for the object/field, do you want X or Y?"},
        ]
        out = notify(items, dry_run=True)
        # finding #11: a zoom item with NO state must render HELD-style — no deep link, never
        # "draft ready for your tap" language pointing at an unverified thread draft.
        stateless = _line({"surface": "zoom", "target_name": "Somebody, New",
                           "jid": "user@example.com", "draft_text": "hi there"})
        assert "zoom.us/launch" not in stateless and "UNVERIFIED" in stateless and "hi there" in stateless, \
            "stateless zoom item must render HELD (text inline, no deep link)"
        assert "draft ready for your tap" not in stateless
        # sits-too-long nudge: an aged draft re-nudges w/ an escalating badge; an unchanged set is deduped.
        # Use a THROWAWAY state path so the selftest never touches the live nudge state.
        global _NUDGE_STATE
        import tempfile
        _orig_ns = _NUDGE_STATE
        _NUDGE_STATE = Path(tempfile.mkdtemp()) / "nudge.json"
        try:
            _k = "user@example.com"
            _save_nudge_state({_k: {"first": time.time() - 7200, "tier": 0}})  # 2h old, not yet nudged
            _aged = notify([{"surface": "zoom", "state": "held", "target_name": "Aged One",
                             "jid": _k, "draft_text": "x"}], post_channel=False)
            assert _aged.get("nudged") and "waiting ~1h" in _aged["rollup"], "aged draft must re-nudge w/ a badge"
            _again = notify([{"surface": "zoom", "state": "held", "target_name": "Aged One",
                              "jid": _k, "draft_text": "x"}], post_channel=False)
            assert _again.get("skipped"), "no tier change -> deduped, no re-post"
        finally:
            _NUDGE_STATE = _orig_ns
        print(json.dumps(out, ensure_ascii=False, indent=1))
        print("reply_notify selftest OK: stateless item renders HELD (no deep link); nudge ages+dedups; no env mutation")
        return 0
    items = json.loads(a.items) if a.items else []
    print(json.dumps(notify(items, issue_key=a.issue_key, dry_run=a.dry_run,
                            post_channel=not a.no_channel), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
