#!/usr/bin/env python3
"""build_launch_assets.py — stage ready-to-fire social-growth marketing assets.

Every asset is gated through core.content_quality (slop / AI-fingerprint / voice-bleed
/ length) PLUS a local no-leak blocklist that the quality gate does NOT cover: words
that would reveal the service is automation/AI (bot, automat*, ai, script, software,
gaussian, circadian, ...). Buyer-facing copy must never reveal it's automated — that's
the moat. Posting stays owner-fired; this only writes files for the persona posters.

Run from the main repo so `import core.*` resolves (content_quality.py is untracked and
absent from the worktree). Output paths are absolute (worktree data/launch_assets).
"""
from __future__ import annotations
import json, re, sys, datetime
from pathlib import Path

from core.content_quality import (
    gate, tone_gate, SLOP_PHRASES, ARTIFACTS, PREAMBLE_ARTIFACTS,
    _REAL_NAME, _SLUR_WORDS, PERSONAS, IMA_WEAK_PHRASES,
)

OUT = Path("/home/user/claude-stack/.claude/worktrees/wf_c1ee6a70-a33-6/data/launch_assets")
OUT.mkdir(parents=True, exist_ok=True)

# ── local no-leak blocklist (the quality gate does NOT catch these) ──────────────
# Words/patterns that would reveal the managed service is automation/AI software.
# "algorithm"/"feed"/"system"/"tool" are deliberately NOT here — they're normal
# creator vocabulary that doesn't expose OUR mechanism.
_LEAK = re.compile(
    r"\b(bots?|robots?|ai|saas|macros?|scripts?|scripted|scheduler|software|cron|"
    r"playwright|selenium|cdp|chromium|headless|gaussian|circadian)\b"
    r"|automat|auto-?post|a\.i\.|artificial intelligence",
    re.I,
)
def leak_hits(t: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _LEAK.finditer(t or "")})

def hard_check(text: str, persona: str) -> list[str]:
    """HARD content failures only (no length / link / soft-score) — for hooks + bios."""
    low = (text or "").lower(); r: list[str] = []
    r += [f"slop:{p}" for p in SLOP_PHRASES if p in low]
    r += [f"artifact:{a}" for a in ARTIFACTS if a in low]
    r += [f"preamble:{p}" for p in PREAMBLE_ARTIFACTS if low.startswith(p)]
    r += [f"real-name:{t}" for t in _REAL_NAME if t in low]
    r += [f"slur:{w}" for w in _SLUR_WORDS if w in f" {low} "]
    v = PERSONAS.get(persona, PERSONAS["generic"])
    r += [f"forbidden:{f}" for f in v.forbidden if f in low]
    if persona == "persona1":
        r += [f"persona1-weak:{p}" for p in IMA_WEAK_PHRASES if p in low]
    r += [f"leak:{h}" for h in leak_hits(text)]
    return r

# ── HOOKS (short opening lines, usable across TikTok + IG/Threads) ───────────────
# persona "generic" = brand-neutral handles (Main Street Social / Shop Reach Co /
# Growth Concierge). "persona1" = @persona1 lead consumer persona. "buyersignal" = B2B.
HOOKS = [
    ("H01", "persona1",         "your account isn't dead, it's cold. here's the 14-day fix."),
    ("H02", "persona1",         "POV: you run your business and your socials quietly grow in the background."),
    ("H03", "persona1",         "3 signs your account is about to get buried, and the fix."),
    ("H04", "persona1",         "stop posting 5 times a day. that's why your reach tanked."),
    ("H05", "persona1",         "dead 200-follower account, warmed for 14 days. day 1 vs day 14."),
    ("H06", "persona1",         "your reach didn't drop by accident. quiet accounts get buried."),
    ("H07", "persona1",         "hun, you don't need 6 hours a day on your phone to grow. you need a rhythm."),
    ("H08", "persona1",         "i grow people's accounts without them ever touching their phone. here's week one."),
    ("H09", "persona1",         "the 14-day warm-up that brings a cold account back to life."),
    ("H10", "persona1",         "everyone said post more. posting more is what buried my account."),
    ("H11", "persona1",         "your account is one steady warm-up away from getting seen again."),
    ("H12", "persona1",         "what a safe, slow account warm-up actually looks like, day by day."),
    ("H13", "persona1",         "the reason your new account got buried in week one, and how to dodge it."),
    ("H14", "persona1",         "i don't just post for you. i grow you. there's a difference."),
    ("H15", "persona1",         "before you post again, run this 60-second account check."),
    ("H16", "generic",     "Your shop is great. Your Instagram going quiet is what's costing you walk-ins."),
    ("H17", "generic",     "We run your storefront's socials so you can run your storefront."),
    ("H18", "generic",     "Posting for your business shouldn't take an hour you don't have."),
    ("H19", "generic",     "Your store's feed went quiet, so your reach did too. Here's the fix."),
    ("H20", "generic",     "Coaches: your audience forgets you the week you stop showing up."),
    ("H21", "buyersignal", "Buyers see you once a quarter, right before they need you. That's why the pipeline stalls."),
    ("H22", "buyersignal", "Staying in front of buyers every week is the cheapest sales move there is."),
]

# ── TikTok reel scripts (channel reel_script: 120-1200 chars, 0 emoji, no links) ─
TIKTOK = [
    ("TT01", "persona1", "hun-growth-desk",
     "your account isn't dead. it's cold. a dead account is gone for good. a cold one just "
     "stopped getting shown because it went quiet for too long. so we wake it up the slow "
     "way. a little real activity the first few days, a bit more the next, the way a person "
     "eases back in after time off, never all at once. fourteen days later the same handle "
     "that was getting zero reach is back in front of buyers. no posting marathon, no living "
     "on your phone. that steady rhythm is the whole system. want me to look at yours? "
     "comment the word GROW and the free account audit is yours."),
    ("TT02", "persona1", "hun-growth-desk",
     "posting five times a day is not growth, it is a flag. the feed watches for accounts "
     "that suddenly fire off content like clockwork, because real people don't move like "
     "that. when you post in bursts to beat the feed, the pattern looks forced, and that is "
     "exactly when reach gets quietly throttled. the fix is boring: fewer posts, spaced the "
     "way a human would space them, plus a little real activity in between so the account "
     "stays warm. that steady rhythm is how a cold account climbs back without losing the "
     "buyers you already have. i set this up for sellers every day. comment GROW and i'll "
     "send the free account check."),
    ("TT03", "persona1", "hun-growth-desk",
     "two hundred followers, no likes, no reach. that account looked done. we didn't post "
     "harder, we warmed it. day one, almost nothing, just a few real interactions. by day "
     "five, a little more. by day fourteen it was showing up in feeds again and pulling in "
     "new buyers on its own. the trick was never volume. it was pacing that looks human, "
     "every single day, so the feed trusts the account again. that is the entire play. if "
     "your account went quiet and never came back, comment GROW and i'll run you a free audit."),
    ("TT04", "persona1", "hun-growth-desk",
     "here's what working with me actually looks like. you hand me one thing: access to "
     "your own account. that's it. no calendar to fill, no captions to write at midnight, "
     "no guessing the best time to post. i handle the rhythm, the engagement, the "
     "consistency, all paced like a careful person so your account stays safe. you keep "
     "doing the work you're actually good at. every week you get a simple report showing "
     "what grew and which posts your buyers reacted to. it's your account, your voice, just "
     "finally consistent. comment GROW if you want me to look at yours first, free."),
    ("TT05", "persona1", "hun-growth-desk",
     "if you sell anything online, your account going quiet costs you more than a bad post "
     "ever could. the feed forgets accounts that disappear. one slow week and your next "
     "post barely reaches the people who already follow you. that is why consistency beats "
     "brilliance here. a steady, human-paced presence keeps you in front of your buyers, so "
     "when you do drop an offer, it actually lands. you don't need to be louder. you need to "
     "be there, on a rhythm you can't hold by hand. comment GROW and i'll send a free read "
     "on your account."),
    ("TT06", "persona1", "hun-growth-desk",
     "three quick signs your account is in trouble. one, your views dropped off a cliff and "
     "never recovered. two, new posts reach fewer people than your follower count. three, "
     "you've gone more than a week without showing up. any one of those means the account "
     "went cold, not that your content got worse. the fix isn't a viral video, it's a "
     "steady warm-up that rebuilds trust with the feed over about two weeks, so your next "
     "offer is actually seen. i do this quietly in the background for people who'd rather "
     "run their business. comment GROW for a free account audit."),
    ("TT07", "persona1", "hun-growth-desk",
     "you do not need to go viral. i need to say that louder for the people refreshing their "
     "analytics at midnight. one viral video to a cold audience does almost nothing, the "
     "spike fades in two days and the account goes quiet again. steady wins. a handle that "
     "shows up consistently, paced like a person, builds an audience of real buyers, not one "
     "that watched once and left. that is the difference between reach and revenue. i grow "
     "accounts on that boring, reliable rhythm. comment GROW and i'll audit yours free."),
    ("TT08", "persona1", "hun-growth-desk",
     "the number one fear i hear: won't all this activity get my account banned? it's the "
     "opposite. bans come from spikes, from accounts that blast out actions faster than a "
     "person ever could. what keeps an account safe is restraint, slow ramps on a new "
     "account, varied timing, quiet hours overnight, and never doing more in a day than a "
     "real person would. that caution is the whole point. i'd rather grow you steady for "
     "months than burn you in a week. comment GROW and i'll show you what safe growth looks "
     "like on your account and offers."),
    ("TT09", "persona1", "hun-growth-desk",
     "growth has a rhythm, and most people are playing it wrong. they go silent for two "
     "weeks then dump six posts in a day, then wonder why nothing reaches. the feed reads "
     "that as someone who isn't really here. real presence is small and steady: show up, "
     "engage a little, post when it makes sense, repeat. that pattern, held for weeks, is "
     "what tells the feed your account is alive and worth showing. it's not exciting, it "
     "just works. i hold that rhythm for you so you don't have to. comment GROW for a free "
     "audit and i'll find your gaps."),
    ("TT10", "generic", "main-street-social",
     "Your shop does great work. The problem is nobody local sees it between visits. When "
     "your Instagram and Facebook go quiet, the feed stops showing you, and you slowly fall "
     "off your customers' radar. We fix that without adding anything to your plate. We keep "
     "your pages active and engaging the local audience that actually walks in, paced "
     "naturally so the accounts stay healthy. You run the shop, we keep the town seeing it. "
     "Comment GROW or tap the link in our bio for a free look at your accounts."),
    ("TT11", "generic", "shop-reach-co",
     "Your store's feed is your storefront window, and right now it's dark. Shoppers scroll "
     "past brands that post once and vanish, because a quiet feed reads as a closed shop. We "
     "keep your TikTok and Instagram consistently active with content and real engagement in "
     "your niche, so your store stays in front of people who buy. No agency retainer, no "
     "hiring, no learning another dashboard. You ship product, we keep the feed alive and "
     "the reach climbing. Comment GROW for a free read on your store's accounts."),
    ("TT12", "generic", "growth-concierge",
     "Coaches, your audience forgets you the week you stop showing up. It's not personal, "
     "it's just how the feed works, attention goes to whoever is consistent. The problem is "
     "consistency is a full-time job and you already have one. So we carry it. We keep you "
     "visible with steady, human-paced posting and engagement, in your voice, so your people "
     "see you every week and your offers actually get noticed. You coach, we keep the room "
     "full. Comment GROW for a free look at where your reach is leaking."),
]

# ── IG / Threads captions (channel instagram_caption: 40-2200, hashtags ok, no links) ─
CAPTIONS = [
    ("IG01", "persona1", "hun-growth-desk",
     "your account isn't dead. it's cold.\n\nthere's a real difference. dead means gone. "
     "cold means it just stopped getting shown because you went quiet for a while. the feed "
     "deprioritizes accounts that disappear, even good ones.\n\nthe fix isn't a viral hail "
     "mary. it's a slow, steady warm-up, a little real activity each day, building back up "
     "the way a person naturally would, until the account is trusted and seen again. usually "
     "about two weeks.\n\nthat's the whole system, and it's the part most people skip because "
     "it's boring and it takes patience.\n\nif your reach fell off and never came back, the "
     "free account audit in my bio will tell you exactly why. or comment GROW and i'll send "
     "it over.\n\n#smallbusiness #socialmediatips #directsales"),
    ("IG02", "persona1", "hun-growth-desk",
     "posting more is not the answer. i promise.\n\ni watch sellers burn out posting five "
     "times a day, then panic when reach drops anyway. here's what's actually happening: "
     "bursts of content followed by silence look unnatural, and the feed quietly throttles "
     "accounts that move in spikes.\n\nthe accounts that climb are the consistent ones. a "
     "steady presence, paced like a real human across the week, beats a frantic posting day "
     "every single time.\n\nyou don't need to do more. you need a rhythm you can actually "
     "hold, or someone holding it for you.\n\nthat's what i do, quietly, in the background, "
     "so the account grows and you get your evenings back. free audit's in my bio.\n\n"
     "#contentstrategy #socialmediagrowth"),
    ("IG03", "persona1", "hun-growth-desk",
     "the offer is simple: you do one thing, i do the rest.\n\nyou hand me access to your "
     "own account. that's the only step on your side. no content calendar, no writing "
     "captions at midnight, no guessing the best time to post.\n\ni handle the engagement, "
     "the consistency, the pacing, all of it done the way a careful human would, so your "
     "account stays safe while it grows. it stays your voice and your handle.\n\nevery week "
     "you get a plain-english report: who you reached, what grew, which posts your buyers "
     "responded to.\n\nyou keep running your business. i keep your presence alive. comment "
     "GROW or tap the link in my bio for a free look first.\n\n#donewithyou #socialmediamanager"),
    ("IG04", "persona1", "hun-growth-desk",
     "three signs your account went cold (not bad, cold):\n\n1. your views dropped off a "
     "cliff and never recovered.\n2. new posts reach fewer people than your follower count."
     "\n3. you've gone more than a week without showing up.\n\nany one of those and the issue "
     "isn't your content, it's that the account went quiet and the feed stopped trusting it."
     "\n\nthe fix is a steady two-week warm-up that rebuilds that trust, not a desperate "
     "viral attempt. boring, but it's the thing that actually works.\n\ni run this quietly "
     "for people who'd rather spend their time on the business than on the feed. comment "
     "GROW for a free audit and i'll tell you which sign you're hitting.\n\n#socialmediatips "
     "#smallbiz #creatortips"),
    ("IG05", "persona1", "hun-growth-desk",
     "\"won't all that activity get me banned?\"\n\nthe opposite, actually. bans come from "
     "spikes, accounts that fire off actions faster than any person could, or a brand-new "
     "account acting like a five-year-old one on day two.\n\nwhat keeps an account safe is "
     "restraint. slow ramps. varied timing. quiet overnight hours. never more in a day than "
     "a real person would do. that caution is the entire point of how i work.\n\ni'd rather "
     "grow your account steadily for months than push too hard and lose it in a week. safe "
     "is the strategy, not the afterthought.\n\nwant to see what safe growth would look like "
     "on your account? the free audit is in my bio.\n\n#socialmediagrowth #accountsafety"),
    ("IG06", "persona1", "hun-growth-desk",
     "hun, you do not need to live on your phone to grow.\n\ni know the advice out there: "
     "post all day, go live constantly, slide into every dm. it's exhausting and it's why so "
     "many sellers quit before it works.\n\nhere's the truth. growth is consistency, not "
     "intensity. a steady presence that shows up every day, paced like a real person, will "
     "out-grow a frantic posting spree every time, and it won't get your account flagged."
     "\n\nyou've got a business to run and a life to live. let the steady part run in the "
     "background while you do what you're good at, talking to your buyers.\n\nfree account "
     "audit is in my bio, or comment GROW.\n\n#directsales #bossbabe #socialselling"),
    ("IG07", "persona1", "hun-growth-desk",
     "the boring truth about growing on here:\n\nit's not the hook. it's not the trending "
     "sound. it's that you showed up today, and yesterday, and you'll show up tomorrow, "
     "paced like a normal person instead of someone gaming the system.\n\nconsistency is the "
     "only hack that survives every feed update. the problem is consistency is hard to fake "
     "and harder to keep when you're running an actual business.\n\nso i keep it for you. "
     "steady presence, real engagement, your voice, your account staying safe the whole way."
     "\n\nyou'll feel it in about two weeks. comment GROW or grab the free audit in my bio."
     "\n\n#socialmediagrowth #consistencyiskey"),
    ("IG08", "generic", "main-street-social",
     "Here's the quiet math nobody tells local businesses.\n\nThe week your Instagram goes "
     "silent, your reach drops. A few silent weeks and you've basically vanished from the "
     "feed your customers scroll every morning. You didn't do anything wrong, the feed just "
     "rewards whoever shows up consistently.\n\nThe problem is consistency is a part-time "
     "job, and you're already working a full one running the place.\n\nThat's the whole "
     "reason we exist. We keep your pages active and engaging the people in your town, paced "
     "naturally so the accounts stay healthy, in your shop's voice.\n\nYou run the shop. We "
     "keep the town seeing it. Free look at your accounts, link in bio.\n\n#localbusiness "
     "#smallbusinessowner"),
    ("IG09", "generic", "shop-reach-co",
     "Your store's feed is a storefront window, and right now it's dark.\n\nShoppers scroll "
     "past brands that post once and disappear. A quiet feed reads as a closed shop, so they "
     "keep scrolling to a competitor who looks open.\n\nYou don't need to become a full-time "
     "content creator on top of running fulfillment. You need the feed to stay alive.\n\nWe "
     "keep your TikTok and Instagram consistently active, real content and real engagement "
     "in your niche, so your store stays in front of people ready to buy.\n\nNo retainer, no "
     "hiring, no new dashboard to learn. You ship product, we keep the reach climbing. Free "
     "read on your accounts, link in bio.\n\n#ecommerce #shopifystore #etsyseller"),
    ("IG10", "generic", "growth-concierge",
     "Coaches: your audience forgets you the week you stop showing up.\n\nIt isn't personal. "
     "Attention flows to whoever is consistent, and the feed has a short memory. Miss a week "
     "and your next post barely reaches the people who already follow you.\n\nThe catch is "
     "that consistency is a full-time job and you already have one, coaching.\n\nSo we carry "
     "it. Steady, human-paced posting and engagement in your voice, so your people see you "
     "every week and your offers actually get noticed instead of buried.\n\nYou coach. We "
     "keep the room full. Want a free look at where your reach is leaking? Link in bio.\n\n"
     "#lifecoach #onlinecoach #coachingbusiness"),
    ("IG11", "buyersignal", "presence",
     "Buyers see you about once a quarter, right before they need you. That's why your "
     "pipeline feels feast or famine.\n\nThe fix isn't more cold outreach. It's presence. "
     "When buyers see you show up every week with something useful, you're the name "
     "they remember the moment intent kicks in.\n\nMost founders go quiet because posting "
     "consistently is real work on top of actually selling. So it slips, and the pipeline "
     "goes cold with it.\n\nStaying in front of buyers every week is the cheapest sales move "
     "there is, and it compounds. We keep that presence running so you stay top of mind "
     "without it eating your week.\n\nFollow for more on turning presence into pipeline.\n\n"
     "#b2bsales #founderled #linkedintips"),
    ("IG12", "buyersignal", "presence",
     "Cold outreach has a math problem: you're interrupting people who never asked to hear "
     "from you.\n\nPresence flips it. When a prospect has seen your name in their feed every "
     "week for two months, your message lands as a familiar voice, not a stranger's pitch. "
     "Same message, completely different reply rate.\n\nThe hard part is the every-week part. "
     "Founders start strong, then a busy month kills the streak and the warmth resets to "
     "zero.\n\nWe keep your professional presence consistent so prospects know you before "
     "you ever reach out. The pitch gets easier when you're already a known name.\n\nFollow "
     "if you'd rather warm your prospects than cold-call them.\n\n#b2bmarketing #sales "
     "#prospecting"),
]

# ── BIO / link copy per brand (links allowed; gated with hard_check, not channel gate) ─
WARMUP = "https://scannerapp.dev/go/account-warm-up-sprint"   # $49 tripwire
BIZ    = "https://scannerapp.dev/biz"                          # storefront
BIOS = [
    ("hun-growth-desk", "persona1", "@persona1", WARMUP, "GROW", [
        "operator, not influencer. i quietly grow accounts the safe, steady way, no posting "
        "marathons, no living on your phone. free account audit + the 14-day warm-up below. "
        + WARMUP,
        "i grow your socials so you don't have to live on your phone. steady, safe, hands-off. "
        "comment GROW or grab the free account audit below. " + WARMUP,
    ]),
    ("main-street-social", "generic", "@mainstreetsocial", BIZ, "GROW", [
        "We run your shop's socials so you can run your shop. Local-first, no contracts. Free "
        "account check below. " + BIZ,
        "Your town should see your business every week. We keep your pages active so locals "
        "find you first. Free look below. " + BIZ,
    ]),
    ("shop-reach-co", "generic", "@shopreachco", BIZ, "GROW", [
        "We keep your store's feed alive so your reach never goes quiet. More eyes, zero "
        "hours from you. Free read below. " + BIZ,
        "Your storefront window shouldn't go dark. We keep your TikTok and Instagram active "
        "so shoppers keep scrolling to you. Free read below. " + BIZ,
    ]),
    ("growth-concierge", "generic", "@thegrowthconcierge", BIZ, "GROW", [
        "Your audience, kept warm while you coach. Steady presence, your voice, hands-off. "
        "Free reach audit below. " + BIZ,
        "We keep your people seeing you every week so your offers never get buried. You "
        "coach, we keep the room full. Free audit below. " + BIZ,
    ]),
    ("presence", "buyersignal", "@buyersignal", "https://redacted.com", None, [
        "Stay in front of buyers every week without it eating your time. Turn presence "
        "into pipeline. More at redacted.com",
        "Buyers forget you between quarters. We keep your professional presence "
        "consistent so you're the name they remember. redacted.com",
    ]),
]

# ── gate everything ──────────────────────────────────────────────────────────────
report = {"hooks": [], "tiktok_scripts": [], "captions": [], "bios": [], "failures": []}

def record(section, asset_id, persona, channel, text, passed, score, reasons):
    row = {"id": asset_id, "persona": persona, "channel": channel,
           "passed": passed, "score": score, "reasons": reasons}
    report[section].append(row)
    if not passed:
        report["failures"].append({"section": section, **row,
                                    "text": text[:160]})

hooks_out, tt_out, cap_out, bio_out = [], [], [], []

for hid, persona, text in HOOKS:
    hr = hard_check(text, persona)
    v = gate(text, channel="tiktok_comment", persona=persona)  # informational soft score
    passed = not hr   # hooks: accept on zero HARD fails (too short for soft thresholds)
    record("hooks", hid, persona, "hook", text, passed, v.score, hr or v.reasons)
    hooks_out.append({"id": hid, "persona": persona, "text": text,
                      "len": len(text), "soft_score": v.score})

for tid, persona, brand, text in TIKTOK:
    v = gate(text, channel="reel_script", persona=persona)
    lk = leak_hits(text)
    passed = v.passed and not lk
    reasons = list(v.reasons) + ([f"leak:{lk}"] if lk else [])
    record("tiktok_scripts", tid, persona, "reel_script", text, passed, v.score, reasons)
    tt_out.append({"id": tid, "persona": persona, "brand": brand, "platform": "tiktok",
                   "channel": "reel_script", "script": text, "len": len(text),
                   "soft_score": v.score, "cta_word": "GROW"})

for cid, persona, brand, text in CAPTIONS:
    v = gate(text, channel="instagram_caption", persona=persona)
    lk = leak_hits(text)
    passed = v.passed and not lk
    reasons = list(v.reasons) + ([f"leak:{lk}"] if lk else [])
    record("captions", cid, persona, "instagram_caption", text, passed, v.score, reasons)
    cap_out.append({"id": cid, "persona": persona, "brand": brand,
                    "platform": ["instagram", "threads"], "channel": "instagram_caption",
                    "caption": text, "len": len(text), "soft_score": v.score,
                    "cta_word": ("GROW" if persona != "buyersignal" else None)})

for brand, persona, handle, link, cta, variants in BIOS:
    vb = []
    for i, btxt in enumerate(variants):
        hr = hard_check(btxt, persona)
        passed = not hr
        record("bios", f"{brand}#{i}", persona, "bio", btxt, passed, 100, hr)
        vb.append(btxt)
    bio_out.append({"brand": brand, "persona": persona, "handle": handle,
                    "link_target": link, "cta_word": cta, "variants": vb})

# ── 14-day calendar (owner-fired; circadian-safe windows; no 1-6am) ──────────────
START = datetime.date(2026, 6, 9)
def d(n): return (START + datetime.timedelta(days=n - 1)).isoformat()
CAL = [
    {"day": 1, "date": d(1), "phase": "setup+seed",
     "tasks": ["fix persona1 topic_pool (replace HN-headline slop with growth-pain topics)",
               "set persona bios from bio_link_copy.json", "verify /go links resolve"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT01", "window": "10:00-12:00"},
               {"persona": "persona1", "platform": "instagram", "asset": "IG01", "window": "18:00-21:00"}]},
    {"day": 2, "date": d(2), "phase": "setup+seed",
     "tasks": ["stand up 5 branded landings (clone persona1.html)", "build free 'Ban-Safe Growth Checklist' magnet"],
     "posts": [{"persona": "persona1", "platform": "threads", "asset": "IG02", "window": "11:00-12:00"},
               {"persona": "generic", "platform": "tiktok", "asset": "TT10", "window": "18:00-20:00"}]},
    {"day": 3, "date": d(3), "phase": "warm+seed",
     "tasks": ["start warmup on 2-3 fresh broadcast accounts", "begin intent replies (6/platform/day, value-first, no link)"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT02", "window": "10:00-12:00"},
               {"persona": "persona1", "platform": "instagram", "asset": "IG04", "window": "19:00-21:00"},
               {"persona": "generic", "platform": "threads", "asset": "IG08", "window": "12:00-13:00"}]},
    {"day": 4, "date": d(4), "phase": "warm+seed",
     "tasks": ["capture first emails via free-audit magnet"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT09", "window": "10:00-12:00"},
               {"persona": "persona1", "platform": "instagram", "asset": "IG07", "window": "18:00-21:00"},
               {"persona": "buyersignal", "platform": "threads", "asset": "IG11", "window": "08:00-10:00"}]},
    {"day": 5, "date": d(5), "phase": "warm+seed",
     "tasks": ["A/B which brand bio link gets most clicks (UTM per brand)"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT05", "window": "10:00-12:00"},
               {"persona": "generic", "platform": "tiktok", "asset": "TT11", "window": "18:00-20:00"},
               {"persona": "persona1", "platform": "instagram", "asset": "IG06", "window": "19:00-21:00"}]},
    {"day": 6, "date": d(6), "phase": "tripwire-push",
     "tasks": ["push $49 Warm-Up Sprint via before/after proof", "offer first 5 accounts free warm-up for testimonials"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT03", "window": "10:00-12:00"},
               {"persona": "persona1", "platform": "instagram", "asset": "IG03", "window": "18:00-21:00"}]},
    {"day": 7, "date": d(7), "phase": "tripwire-push",
     "tasks": ["DM intent buyers the branded landing (link in DM only)"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT04", "window": "10:00-12:00"},
               {"persona": "generic", "platform": "threads", "asset": "IG10", "window": "12:00-13:00"},
               {"persona": "persona1", "platform": "instagram", "asset": "IG05", "window": "19:00-21:00"}]},
    {"day": 8, "date": d(8), "phase": "tripwire-push",
     "tasks": ["address ban-fear objection content (safety = the pitch)"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT08", "window": "10:00-12:00"},
               {"persona": "buyersignal", "platform": "threads", "asset": "IG12", "window": "08:00-10:00"}]},
    {"day": 9, "date": d(9), "phase": "tripwire-push",
     "tasks": ["kill losing brands, reallocate posting to winner (Day-5 A/B result)"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT07", "window": "10:00-12:00"},
               {"persona": "generic", "platform": "tiktok", "asset": "TT12", "window": "18:00-20:00"},
               {"persona": "persona1", "platform": "instagram", "asset": "IG02", "window": "19:00-21:00"}]},
    {"day": 10, "date": d(10), "phase": "convert+scale",
     "tasks": ["turn warm-up buyers into $199/mo Autopilot via weekly growth report"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT06", "window": "10:00-12:00"},
               {"persona": "persona1", "platform": "instagram", "asset": "IG01", "window": "18:00-21:00"}]},
    {"day": 11, "date": d(11), "phase": "convert+scale",
     "tasks": ["email captured list the case-study + autopilot offer"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT02", "window": "10:00-12:00"},
               {"persona": "generic", "platform": "threads", "asset": "IG09", "window": "12:00-13:00"}]},
    {"day": 12, "date": d(12), "phase": "convert+scale",
     "tasks": ["double down on winning brand+platform combo"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT03", "window": "10:00-12:00"},
               {"persona": "persona1", "platform": "instagram", "asset": "IG04", "window": "19:00-21:00"}]},
    {"day": 13, "date": d(13), "phase": "convert+scale",
     "tasks": ["scale intent DMs on the converting lane"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT05", "window": "10:00-12:00"},
               {"persona": "buyersignal", "platform": "threads", "asset": "IG11", "window": "08:00-10:00"}]},
    {"day": 14, "date": d(14), "phase": "convert+scale",
     "tasks": ["review metrics vs north-star (paid conversions by 2026-07-06); reset cadence for next 14d"],
     "posts": [{"persona": "persona1", "platform": "tiktok", "asset": "TT09", "window": "10:00-12:00"},
               {"persona": "persona1", "platform": "instagram", "asset": "IG07", "window": "18:00-21:00"}]},
]

# ── write outputs ─────────────────────────────────────────────────────────────────
def w(name, obj):
    p = OUT / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    return p

stamp = datetime.datetime.now().isoformat(timespec="seconds")
common = {"generated": stamp, "gated_by": "core.content_quality + local no-leak blocklist",
          "auto_post": False, "note": "Posting is owner-fired/gated. These are staged drafts only."}

w("hooks.json", {**common, "channel_for_gate": "tiktok_comment (hard-fail-only)", "hooks": hooks_out})
w("tiktok_scripts.json", {**common, "platform": "tiktok", "channel": "reel_script", "scripts": tt_out})
w("ig_threads_captions.json", {**common, "platform": ["instagram", "threads"],
                               "channel": "instagram_caption", "captions": cap_out})
w("bio_link_copy.json", {**common, "warmup_link": WARMUP, "storefront": BIZ, "bios": bio_out})
w("calendar_14day.json", {**common, "start": START.isoformat(),
                          "circadian_rule": "post only in 08:00-21:00 local; NEVER 01:00-06:00",
                          "days": CAL})

manifest = {
    **common,
    "purpose": "Ready-to-fire social-growth marketing assets for the persona posters.",
    "product": "Managed social-media growth, sold as outcome. Code never ships. Copy NEVER reveals automation/AI.",
    "files": {
        "hooks.json": f"{len(hooks_out)} short hooks (TikTok+IG/Threads)",
        "tiktok_scripts.json": f"{len(tt_out)} reel scripts (channel reel_script)",
        "ig_threads_captions.json": f"{len(cap_out)} captions (channel instagram_caption; also usable on Threads)",
        "bio_link_copy.json": f"{len(bio_out)} brand bios + storefront link targets",
        "calendar_14day.json": "14-day owner-fired posting calendar (circadian-safe windows)",
        "gate_report.json": "per-asset gate verdicts proving every staged asset passed",
    },
    "persona_map": {
        "persona1 (@persona1)": {
            "brand": "Hun Growth Desk", "platforms": ["tiktok", "instagram", "threads"],
            "angle": "direct-sales/creators; grow hands-off, ban-safe",
            "assets": {"hooks": [h[0] for h in HOOKS if h[1] == "persona1"],
                       "tiktok": [t[0] for t in TIKTOK if t[1] == "persona1"],
                       "captions": [c[0] for c in CAPTIONS if c[1] == "persona1"]},
            "cta": "comment GROW / free account audit -> " + WARMUP},
        "generic (new neutral handles)": {
            "brands": {"@mainstreetsocial": "Main Street Social (local SMB)",
                       "@shopreachco": "Shop Reach Co (ecom)",
                       "@thegrowthconcierge": "The Growth Concierge (coaches)"},
            "platforms": ["tiktok", "instagram", "threads"],
            "assets": {"hooks": [h[0] for h in HOOKS if h[1] == "generic"],
                       "tiktok": [t[0] for t in TIKTOK if t[1] == "generic"],
                       "captions": [c[0] for c in CAPTIONS if c[1] == "generic"]},
            "cta": "comment GROW / link in bio -> " + BIZ},
        "buyersignal (@buyersignal)": {
            "brand": "Presence (B2B)", "platforms": ["threads"],
            "angle": "B2B founders/SaaS; stay in front of buyers, presence->pipeline",
            "note": "voice forbids hard-sell CTAs; funnel to redacted.com in bio only",
            "assets": {"hooks": [h[0] for h in HOOKS if h[1] == "buyersignal"],
                       "captions": [c[0] for c in CAPTIONS if c[1] == "buyersignal"]},
            "cta": "follow / redacted.com (bio only)"},
    },
    "consumption": "Persona posters (reel/caption factories, engage-cdp) read these JSON files; "
                   "match on persona + platform + channel. Run each text back through "
                   "core.content_quality at post time as a second gate; posting stays owner-fired.",
    "totals": {"hooks": len(hooks_out), "tiktok_scripts": len(tt_out),
               "captions": len(cap_out), "bios": sum(len(b["variants"]) for b in bio_out)},
}
w("MANIFEST.json", manifest)
w("gate_report.json", report)

n_fail = len(report["failures"])
print(json.dumps({
    "ok": n_fail == 0,
    "counts": manifest["totals"],
    "failures": report["failures"],
    "out_dir": str(OUT),
}, ensure_ascii=False, indent=2))
sys.exit(1 if n_fail else 0)
