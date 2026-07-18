#!/usr/bin/env python3
"""Self-test for the marketplace rig's non-browser logic:
staging->fields parsing, session save/load, expiry detection, idempotency,
and attachment verification. No browser, no network.

    python3 -m tools.tests.test_marketplace_rig
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.marketplace_session_harvest as H  # noqa: E402
import tools.marketplace_publish as P  # noqa: E402
import tools.chatgpt_share_link as C  # noqa: E402
import tools.marketplace_payout_connect as PC  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  XX  FAIL: {name}")


def test_parse_real_listings():
    print("[parse] real staging listing.md files")
    cases = [
        ("whop", "small-business-sops"),
        ("promptbase", "chatgpt-prompt-pack-consultants"),
        ("etsy", "executive-resume-pack"),
        ("etsy", "freelancer-rate-calculator"),
    ]
    for site, slug in cases:
        pack = P.load_pack(site, slug)
        check(f"{site}/{slug} name", bool(pack.name) and len(pack.name) > 5)
        check(f"{site}/{slug} price>0", pack.price and pack.price > 0)
        check(f"{site}/{slug} description", len(pack.description) > 50)
        check(f"{site}/{slug} deliverable found", pack.deliverable is not None and pack.deliverable.exists())
    # tag extraction on a tag-rich listing
    etsy = P.load_pack("etsy", "executive-resume-pack")
    check("etsy tags parsed (>=5)", len(etsy.tags) >= 5)
    check("etsy no-PII (no surname)", "Operator" not in (etsy.name + etsy.description).lower())


def test_field_mapping_unit():
    print("[parse] unit field mapping")
    md = """# X Listing
## Product Name (paste into "Name")
Test Widget Pack — 10 Things
## Price
**$12.50** one-time
## Description
This is a real description with enough length to pass the validation gate here.
**PII/brand note:** anon.
## Tags
alpha, beta, gamma, delta
"""
    d = P.parse_listing_md(md)
    check("name stripped of paste hint", d["name"] == "Test Widget Pack — 10 Things")
    check("price 12.50", d["price"] == 12.50)
    check("4 tags", d["tags"] == ["alpha", "beta", "gamma", "delta"])
    check("desc excludes PII note", "PII" not in d["description"])


def test_session_roundtrip(tmp):
    print("[session] save/load roundtrip + chmod 600")
    H.SESSION_ROOT = tmp / "sessions"
    state = {"cookies": [{"name": "a", "value": "1", "domain": ".whop.com"}], "origins": []}
    p = H.save_session("whop", state)
    check("file exists", p.exists())
    check("chmod 600", (p.stat().st_mode & 0o777) == 0o600)
    loaded = H.load_session("whop")
    check("cookies survive", loaded["cookies"][0]["name"] == "a")
    check("stamp written", "_saved_at" in loaded)
    age = H.session_age_days("whop")
    check("age ~0", age is not None and age < 0.01)
    check("missing site -> None", H.load_session("nope") is None)


def test_expiry_logic():
    print("[session] expiry detection (no false alarms)")
    check("logged_out -> expired", H.is_expired({"logged_in": False, "age_days": 2}) is True)
    check("no session -> expired", H.is_expired({"logged_in": None, "age_days": None}) is True)
    check("logged_in fresh -> ok", H.is_expired({"logged_in": True, "age_days": 5}) is False)
    check("unknown+fresh -> ok (no cry-wolf)", H.is_expired({"logged_in": None, "age_days": 5}) is False)
    check("unknown+stale -> expired", H.is_expired({"logged_in": None, "age_days": 90}) is True)


def test_idempotency(tmp):
    print("[publish] idempotency registry")
    P.PUBLISHED_REGISTRY = tmp / "published.json"
    check("not listed initially", P.is_listed("whop", "foo") is False)
    P.record_published("whop", "foo", "https://whop.com/x/foo")
    check("listed after record", P.is_listed("whop", "foo") is True)
    check("other slug still unlisted", P.is_listed("whop", "bar") is False)
    reg = json.loads(P.PUBLISHED_REGISTRY.read_text())
    check("url stored", reg["whop/foo"]["url"].endswith("/foo"))


def test_publish_gate(tmp):
    print("[publish] empty-listing gate")
    # synthetic staging product with a deliverable but no cover
    s = tmp / "staging" / "whop" / "demo"
    s.mkdir(parents=True)
    (s / "listing.md").write_text(
        "## Product Name\nDemo Pack Of Things\n## Price\n$9.00\n## Description\n"
        + "x" * 80 + "\n## Tags\na, b, c\n")
    (s / "demo.pdf").write_bytes(b"%PDF-1.4 fake")
    P.STAGING_ROOT = tmp / "staging"
    P.COVERS_ROOT = tmp / "covers"  # empty -> no cover
    pack = P.load_pack("whop", "demo")
    ok, why = pack.ok_to_publish
    check("blocked when no cover", ok is False and "cover" in why)
    # add a cover image, now passes
    (s / "cover.png").write_bytes(b"\x89PNG fake")
    pack2 = P.load_pack("whop", "demo")
    ok2, why2 = pack2.ok_to_publish
    check("passes with deliverable+cover", ok2 is True)
    check("deliverable is the pdf", pack2.deliverable.suffix == ".pdf")


def test_chatgpt_share_link():
    print("[chatgpt] share-link uuid + anon gate + prompt assembly")
    good = "https://chatgpt.com/share/12345678-90ab-cdef-1234-567890abcdef"
    check("uuid extracted", C.extract_share_uuid(good) == "12345678-90ab-cdef-1234-567890abcdef")
    check("uuid case-insensitive", C.extract_share_uuid(good.upper()) is not None)
    check("no uuid -> None", C.extract_share_uuid("https://chatgpt.com/c/abc") is None)
    check("clean text is anon", C.is_anon("a clean revops proposal, no names") is True)
    check("surname caught", C.scan_personal("signed, Operator Operator") == ["Operator"])
    check("email caught", "user@example.com" in C.scan_personal("reach user@example.com"))
    check("employer caught", "CompanyA" in C.scan_personal("at CompanyA Industries"))
    msg = C.build_verification_message()
    check("message non-trivial", len(msg) > 300)
    check("message has sample client", "B2B SaaS" in msg)
    check("message suppresses clarifying Qs", "do not ask clarifying" in msg.lower())
    check("assembled message is anon", C.is_anon(msg) is True)
    check("flagship prompt loads", len(C.load_flagship_prompt()) > 100)


def test_payout_logic():
    print("[payout] status interpretation + KYC guard + account match")
    check("account suffix match", PC.account_matches("acct ends AemilvQSfv here") is True)
    check("account no-match", PC.account_matches("acct_otherXYZ") is False)
    check("fresh KYC detected", PC.detects_fresh_kyc("enter your Social Security Number") is True)
    check("no KYC on plain page", PC.detects_fresh_kyc("Payouts enabled") is False)
    unsup = PC.interpret_payout_status({"external_supported": False})
    check("whop unsupported reported", unsup["state"] == "unsupported_external" and unsup["done"])
    conn = PC.interpret_payout_status({"external_supported": True, "connected": True,
                                       "payouts_enabled": True, "account_match": True})
    check("connected+enabled done", conn["state"] == "connected" and conn["done"])
    check("connected notes owner acct", "owner" in conn["message"])
    kyc = PC.interpret_payout_status({"external_supported": True, "fresh_kyc": True})
    check("fresh KYC -> needs_action, not done", kyc["state"] == "needs_action_reuse" and not kyc["done"])
    none = PC.interpret_payout_status({"external_supported": True})
    check("nothing -> not_connected", none["state"] == "not_connected" and not none["done"])


def test_promptbase_publish_extras():
    print("[publish] promptbase extras + submission-landed signal")
    extras = P._load_promptbase_extras("chatgpt-prompt-pack-consultants")
    check("prompt_text loaded", len(extras["prompt_text"]) > 100)
    check("example output loaded", len(extras["example_output"]) > 50)
    check("no link until minted (or anon-gated)", isinstance(extras["verification_url"], str))
    check("pending review = landed", P.promptbase_landed_from_text("Your prompt is under review", "") is True)
    check("dashboard url = landed", P.promptbase_landed_from_text("", "https://promptbase.com/dashboard") is True)
    check("random page != landed", P.promptbase_landed_from_text("hello", "https://promptbase.com/sell") is False)


def test_harvest_cookie_filter():
    print("[harvest] :18806 cookie domain filter (deferred import)")
    cookies = [
        {"name": "a", "domain": ".whop.com"},
        {"name": "b", "domain": "www.etsy.com"},
        {"name": "c", "domain": ".gumroad.com"},
    ]
    whop = H._filter_cookies(cookies, ["whop.com"])
    check("whop cookie kept", len(whop) == 1 and whop[0]["name"] == "a")
    etsy = H._filter_cookies(cookies, ["etsy.com"])
    check("etsy cookie kept", len(etsy) == 1 and etsy[0]["name"] == "b")
    check("no false match", len(H._filter_cookies(cookies, ["promptbase.com"])) == 0)
    rc = H.import_from_18806("whop", confirm_cover_agent_done=False)
    check("import refuses without ack (cover-agent safety)", rc == 3)


def main():
    test_parse_real_listings()
    test_field_mapping_unit()
    test_chatgpt_share_link()
    test_payout_logic()
    test_promptbase_publish_extras()
    test_harvest_cookie_filter()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_session_roundtrip(tmp)
        test_expiry_logic()
        test_idempotency(tmp)
        test_publish_gate(tmp)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
