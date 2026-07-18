#!/usr/bin/env python3
"""content_quality.py — the ONE gate every piece of content clears before it posts.

"Quality content every single time" is only real if something can REJECT a weak
draft and force a regenerate. This is that something. It is deterministic, $0, and
fast (pure string work, no LLM), so it can run on every draft on every channel.

Two stages:
  • HARD fails  → instant reject, no score needed (slop phrase, off-voice term,
                  banned link/hashtag, near-duplicate, template artifact, length).
  • SOFT score  → 0-100 across hook / specificity / voice / readability; must clear
                  the channel's threshold.

Usage (Python):
    from core.content_quality import gate
    v = gate(text, channel="x", persona="persona1", recent=[...past posts...])
    if v.passed: post(text)
    else:        regenerate(reason=v.reasons)

Usage (CLI, for the JS engage engine):
    echo "$TEXT" | python3 -m core.content_quality --channel tiktok_comment --persona persona3
    # exit 0 = passed, exit 1 = failed; prints JSON verdict
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict


# ── AI-slop blocklist: the phrases that scream "a model wrote this" ──────────────
# Any one of these is an instant reject. Curated from real LLM tells.
SLOP_PHRASES = {
    "in today's fast-paced world", "in the world of", "in the realm of",
    "unlock", "unleash", "delve", "dive into", "let's dive in", "deep dive",
    "elevate your", "game-changer", "game changer", "supercharge", "revolutionize",
    "harness the power", "in conclusion", "it's important to note", "it is important to note",
    "navigating the", "tapestry", "testament to", "look no further", "the bottom line",
    "at the end of the day", "synergy", "seamless", "cutting-edge", "in summary",
    "moreover", "furthermore", "when it comes to", "needle-moving", "best-in-class",
    "buckle up", "you guessed it", "that's right,", "say goodbye to", "say hello to",
    "level up your", "take it to the next level", "the power of", "ever-evolving",
    "look no further", "rest assured", "without further ado", "embark on",
}

IMA_WEAK_PHRASES = {
    "excel", "spreadsheet", "maybes", "\"maybes\"", "maybe leads",
    "quick call", "post-event", "send a text", "keep an eye on",
    "saves time", "without being a pest", "without being pushy",
    "showed interest", "didn't buy", "survey", "upsell", "supplies",
}

# Unfinished-draft / model-leak artifacts — instant reject.
ARTIFACTS = (
    "as an ai", "as a language model", "i cannot", "i'm sorry", "[insert", "[topic",
    "{topic", "{persona", "lorem ipsum", "todo:", "xxx", "<think", "</think",
    "certainly!", "i'd be happy to",
)
# LLM-preamble phrases: only an artifact when they OPEN the text or a line, so they
# don't false-block normal prose ("...and here is why..." in a blog body).
PREAMBLE_ARTIFACTS = (
    "here is", "here's a", "sure, here",
)

# Emoji / exclamation / hashtag spam thresholds.
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)
_HASHTAG_RE = re.compile(r"#\w+")
_URL_RE = re.compile(r"https?://|www\.|\b\w+\.(com|net|io|dev|app|co)\b", re.I)


# ── per-channel rules ───────────────────────────────────────────────────────────
@dataclass
class ChannelRule:
    min_len: int
    max_len: int
    allow_links: bool
    allow_hashtags: bool
    max_emoji: int
    threshold: int          # soft-score pass bar


CHANNELS: dict[str, ChannelRule] = {
    "x":               ChannelRule(15, 280, False, False, 1, 70),
    "tweet":           ChannelRule(15, 280, False, False, 1, 70),
    "tiktok_comment":  ChannelRule(4, 150, False, False, 1, 60),
    "comment":         ChannelRule(4, 150, False, False, 1, 60),
    "instagram_caption": ChannelRule(40, 2200, False, True, 4, 68),
    "reel_script":     ChannelRule(120, 1200, False, False, 0, 70),
    "blog":            ChannelRule(600, 100000, True, True, 6, 65),
    "long_form":       ChannelRule(600, 100000, True, True, 6, 65),
}


# ── per-persona voice ────────────────────────────────────────────────────────────
@dataclass
class PersonaVoice:
    forbidden: tuple[str, ...]          # any → hard fail
    required_any: tuple[str, ...]       # at least one expected (soft signal)
    prefer_lowercase_i: bool = False    # 'i' should be lowercase (persona1/persona3 casual voice)
    max_caps_ratio: float = 0.5         # SHOUTING guard


# persona1's distinctive selling/business voice. Forbidden on EVERY other persona so her
# scripts can never bleed into persona2/persona3/Penny (real bleed seen 2026-06).
# These are distinctive enough not to false-positive on SWE / humor / kids voices.
_IMA_SIGNATURE = (
    "follow-up system", "buyer tracker", "direct sales", "direct seller",
    "downline", "upline", "between parties", "party plan", "live selling",
    "bomb party", "reorder", "your buyers", "customer list", "boss babe",
    "back-end of your", "your bio", "host a party", "mlm",
    # full persona1 angle lexicon — distinctive MLM/social-selling terms so persona2/persona3/
    # Penny can NEVER echo her selling angles (gate hard-rejects). Kept MLM-specific to avoid
    # false-positives on humor / software / kids voices. 2026-06-05
    "social seller", "social selling", "warm buyer", "warm lead", "comp plan",
    "rank up", "vendor event", "party rep", "hostess", "your reps",
    "join my team", "build your team", "warm market", "hot leads",
)

# Real-person / cross-brand identity terms that must NEVER appear on a faceless
# brand channel (the YouTube engine's hard firewall: never reveal the operator,
# the agency, or that it's AI-built). Substring-matched on lowercased text.
_REAL_NAME = (
    "Operator", "Operator Operator", "BrandA", "anthropic", "claude",
    "openclaw", "docsapp",
)
# In-script hard-sell CTAs — the faceless how-to channels TEACH value and funnel to
# the domain only in the description, never beg-to-buy inside the spoken script.
_SELLING_CTA = (
    "buy now", "link in bio", "dm me", "sign up", "discount code",
    "my course", "my product", "subscribe now", "limited time",
)

PERSONAS: dict[str, PersonaVoice] = {
    "persona1": PersonaVoice(
        forbidden=(
            "become a rep", "business opportunity", "downline", "join bomb party",
            "join my team", "join our team", "opportunity call", "recruit",
            "sell bomb party", "start an mlm", "join the team", "build your downline",
        ),
        required_any=(
            "follow-up", "follow up", "buyer", "customer", "dm", "system", "live",
            "offer", "product", "bio", "repeat", "automat", "track",
        ),
        prefer_lowercase_i=True,
    ),
    "persona2": PersonaVoice(
        forbidden=("buy now", "link in bio", "dm me to", "join my", "sign up", "discount code") + _IMA_SIGNATURE,
        required_any=(),
    ),
    "persona4": PersonaVoice(
        forbidden=(
            "damn", "hell", "crypto", "leverage", "stock", "gambl", "loan", "debt",
            "kill", "hate", "stupid",
        ) + _IMA_SIGNATURE,
        required_any=("save", "coin", "money", "learn", "count", "penny", "spend", "share"),
    ),
    "persona3": PersonaVoice(
        forbidden=(
            "politic", "gun", "drug", "weed", "maga", "biden", "trump", "slur",
            "buy now", "link in bio", "sign up",
        ) + _IMA_SIGNATURE,
        required_any=(),
        prefer_lowercase_i=True,
    ),
    # ── new faceless channels (FIREWALL-distinct, 2026-06-05) ───────────────────────
    # Each forbids _IMA_SIGNATURE + selling vocab so persona1's angle can never bleed in,
    # and forbids persona3's personal-identity vocab (aggies/houston/swe come-up) so
    # his OPSEC-isolated burner voice never bleeds out into these. They are voice-
    # neutral, native-faceless niches — no personal identity, no selling, no links.
    "persona5": PersonaVoice(
        # curiosity / "did you know" facts. No selling, no first-person hustle voice,
        # no persona3 identity, no horror/scary tone (that's persona7's lane).
        forbidden=(
            "buy now", "link in bio", "dm me", "sign up", "discount",
            "aggie", "texans", "astros", "rockets", "kyle field", "houston",
            "murder", "killer", "haunted", "demon", "blood",
        ) + _IMA_SIGNATURE,
        required_any=("did you know", "fact", "actually", "turns out", "scientists",
                      "study", "history", "earth", "human", "%", "year"),
        max_caps_ratio=0.4,
    ),
    "persona6": PersonaVoice(
        # oddly-satisfying / ASMR ambient. Calm, sensory, present-tense. No selling,
        # no facts-flex, no horror, no identity. Very short gentle lines.
        forbidden=(
            "buy now", "link in bio", "dm me", "sign up", "discount",
            "did you know", "scientists say",
            "aggie", "texans", "astros", "rockets", "houston",
            "murder", "killer", "haunted", "demon", "scary", "scream",
        ) + _IMA_SIGNATURE,
        required_any=("breathe", "slow", "soft", "calm", "rest", "still", "quiet",
                      "let", "settle", "smooth", "gentle", "watch"),
        max_caps_ratio=0.35,
    ),
    "persona7": PersonaVoice(
        # two-sentence horror / unsettling micro-fiction. Eerie, restrained. No gore-
        # porn, no selling, no facts voice, no calm-ASMR voice, no persona3 identity.
        forbidden=(
            "buy now", "link in bio", "dm me", "sign up", "discount",
            "did you know", "fact:", "scientists",
            "aggie", "texans", "astros", "rockets", "houston",
            # keep it suspense-not-splatter, also no real-world hate/violence content
            "gore", "rape", "slur", "nazi", "suicide",
        ) + _IMA_SIGNATURE,
        required_any=(),
        max_caps_ratio=0.45,
    ),
    # ── persona-expansion lane (NEW faceless niches, FIREWALL-distinct, 2026-06-06) ──
    # Each forbids _IMA_SIGNATURE + selling + persona3 identity + the neighbouring
    # personas' distinctive signatures so no voice can bleed into another. Their
    # required_any signature sets are disjoint from every existing persona's.
    "ironcreed": PersonaVoice(
        # stoic discipline / hard-truth motivation. Commanding second-person.
        # No selling, no persona3 identity, no facts voice, no ASMR calm, no horror.
        forbidden=(
            "buy now", "link in bio", "dm me", "sign up", "discount",
            "aggie", "texans", "astros", "rockets", "kyle field", "houston",
            "did you know", "scientists",
            "asmr", "breathe", "gentle pour",
            "gore", "blood", "haunted", "demon",
        ) + _IMA_SIGNATURE,
        required_any=("discipline", "stoic", "control", "comfort", "excuse",
                      "weak", "focus", "earn", "conquer", "master", "harder",
                      "habit", "win", "hard"),
        max_caps_ratio=0.45,
    ),
    "skilletsecret": PersonaVoice(
        # fast one-pan cooking. Warm second-person steps. No selling, no identity,
        # no facts voice, no ASMR, no horror.
        forbidden=(
            "buy now", "link in bio", "dm me", "sign up", "discount",
            "aggie", "texans", "astros", "rockets", "houston",
            "did you know", "scientists",
            "asmr",
            "murder", "killer", "haunted", "demon", "blood",
        ) + _IMA_SIGNATURE,
        required_any=("recipe", "cook", "skillet", "pan", "season", "simmer",
                      "garlic", "sear", "serve", "dinner", "ingredient",
                      "minutes", "golden", "heat"),
        max_caps_ratio=0.4,
    ),
    "mindknot": PersonaVoice(
        # riddles / brain teasers. Curious second-person. No selling, no identity,
        # no facts voice, no ASMR, no discipline-preaching, no horror.
        forbidden=(
            "buy now", "link in bio", "dm me", "sign up", "discount",
            "aggie", "texans", "astros", "rockets", "houston",
            "did you know", "scientists",
            "asmr", "breathe",
            "discipline", "stoic",
            "gore", "blood", "haunted", "demon", "murder",
        ) + _IMA_SIGNATURE,
        required_any=("riddle", "guess", "answer", "solve", "puzzle", "clue",
                      "trick", "brain", "figure", "mystery", "think", "stumped"),
        max_caps_ratio=0.45,
    ),
    # ── faceless YouTube how-to channels (brand-voice, never real name, 2026-06-07) ──
    # Each is its own brand. They forbid the operator's real identity (_REAL_NAME),
    # in-script hard-sell (_SELLING_CTA), persona1's MLM signature, and persona3's personal
    # identity so no voice bleeds across brands. required_any keeps each on-topic.
    "buyersignal": PersonaVoice(
        forbidden=_REAL_NAME + _SELLING_CTA + _IMA_SIGNATURE + (
            "aggie", "texans", "astros", "rockets", "houston",
        ),
        required_any=(
            "buyer", "intent", "signal", "lead", "prospect", "outreach",
            "reply", "pitch", "sales", "reddit", "forum", "message",
        ),
        max_caps_ratio=0.45,
    ),
    "builtfast": PersonaVoice(
        forbidden=_REAL_NAME + _SELLING_CTA + _IMA_SIGNATURE + (
            "aggie", "texans", "astros", "rockets", "houston",
        ),
        required_any=(
            "build", "website", "site", "automation", "tool", "page",
            "fast", "minutes", "fix", "layout", "no code", "block",
        ),
        max_caps_ratio=0.45,
    ),
    "fastaiagency": PersonaVoice(
        forbidden=_REAL_NAME + _SELLING_CTA + _IMA_SIGNATURE + (
            "aggie", "texans", "astros", "rockets", "houston",
        ),
        required_any=(
            "content", "marketing", "automation", "growth", "post", "seo",
            "play", "outreach", "agency", "client", "ops", "schedule",
        ),
        max_caps_ratio=0.45,
    ),
    "generic": PersonaVoice(forbidden=(), required_any=()),
}

# Weak / generic openers that kill a hook.
WEAK_OPENERS = (
    "in this", "today i", "today we", "let me tell you", "have you ever",
    "are you tired", "do you want", "imagine if", "picture this", "we all know",
    "everyone knows", "as you know", "i wanted to", "just wanted to", "here are",
    "here is", "this is a", "there are many",
)

# Concrete signals — specificity reward.
_NUM_RE = re.compile(r"\b\d+\b")
_FILLER = {
    "very", "really", "just", "actually", "basically", "literally", "simply",
    "things", "stuff", "good", "great", "nice", "amazing", "incredible", "awesome",
}


@dataclass
class Verdict:
    passed: bool
    score: int
    channel: str
    persona: str
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def gate(text: str, channel: str = "x", persona: str = "generic",
         recent: list[str] | None = None, voice: PersonaVoice | None = None) -> Verdict:
    """Score one draft. Returns a Verdict; check `.passed`.

    When `recent` is not supplied, auto-pull recent post text for this persona from
    the shared posted ledger so the near-duplicate check is never silently dormant
    (the bug that let persona1 reels repost the same script for days). Pass `recent=[]`
    explicitly to opt out.
    """
    text = (text or "").strip()
    if recent is None:
        try:
            from core.posted_ledger import recent_texts
            recent = recent_texts(persona=persona)
        except Exception:
            recent = None
    rule = CHANNELS.get(channel, CHANNELS["x"])
    if voice is None:
        voice = PERSONAS.get(persona, PERSONAS["generic"])
    low = text.lower()
    reasons: list[str] = []
    checks: dict = {}

    # ── HARD FAILS ───────────────────────────────────────────────────────────────
    n = len(text)
    checks["length"] = n
    if n < rule.min_len:
        reasons.append(f"too short ({n}<{rule.min_len})")
    if n > rule.max_len:
        reasons.append(f"too long ({n}>{rule.max_len})")

    hit_slop = sorted(p for p in SLOP_PHRASES if p in low)
    if hit_slop:
        reasons.append(f"ai-slop: {hit_slop[:3]}")
    checks["slop"] = hit_slop

    hit_ima_weak = sorted(p for p in IMA_WEAK_PHRASES if persona == "persona1" and p in low)
    if hit_ima_weak:
        reasons.append(f"weak-persona1-pattern: {hit_ima_weak[:3]}")
    checks["ima_weak"] = hit_ima_weak

    hit_art = sorted(a for a in ARTIFACTS if a in low)
    line_starts = tuple(ln.strip()[:40] for ln in low.splitlines())
    hit_art += sorted(
        p for p in PREAMBLE_ARTIFACTS
        if low.startswith(p) or any(ls.startswith(p) for ls in line_starts)
    )
    if hit_art:
        reasons.append(f"draft-artifact: {hit_art[:3]}")

    # Local models occasionally leak CJK/other-script characters into English
    # drafts ("...证明自己"). These personas only ever write English, so any such
    # character is garbled output, not content. Emoji are fine and excluded.
    stray = [
        ch for ch in text
        if (
            "\u4e00" <= ch <= "\u9fff"      # CJK unified ideographs
            or "\u3040" <= ch <= "\u30ff"   # hiragana/katakana
            or "\uac00" <= ch <= "\ud7af"   # hangul
            or "\u0400" <= ch <= "\u04ff"   # cyrillic
            or "\u0590" <= ch <= "\u05ff"   # hebrew
            or "\u0600" <= ch <= "\u06ff"   # arabic
        )
    ]
    if stray:
        reasons.append(f"garbled-non-english: {''.join(stray[:6])!r}")
    checks["stray_script"] = len(stray)

    hit_forbidden = sorted(f for f in voice.forbidden if f in low)
    if hit_forbidden:
        reasons.append(f"off-voice/forbidden ({persona}): {hit_forbidden[:3]}")
    checks["forbidden"] = hit_forbidden

    # Identity firewall — applies to EVERY persona, not just the ones that list
    # _REAL_NAME in .forbidden. No live posting persona may ever leak the operator,
    # the agency, sister brands, or that it's AI-built.
    hit_real = sorted(t for t in _REAL_NAME if t in low)
    if hit_real:
        reasons.append(f"identity-leak/real-name: {hit_real[:3]}")
    checks["real_name"] = hit_real

    if not rule.allow_links and _URL_RE.search(text):
        reasons.append("link not allowed on this channel")
    if not rule.allow_hashtags and _HASHTAG_RE.search(text):
        reasons.append("hashtags not allowed on this channel")

    emoji_n = len(_EMOJI_RE.findall(text))
    checks["emoji"] = emoji_n
    if emoji_n > rule.max_emoji:
        reasons.append(f"emoji spam ({emoji_n}>{rule.max_emoji})")
    if text.count("!") > 2:
        reasons.append("exclamation spam")

    # near-duplicate of something we recently posted
    dup = 0.0
    if recent:
        dup = max((_jaccard(text, r) for r in recent), default=0.0)
        if dup >= 0.6:
            reasons.append(f"near-duplicate of recent post (jaccard {dup:.2f})")
    checks["dup_jaccard"] = round(dup, 2)

    hard_failed = bool(reasons)

    # ── SOFT SCORE (0-100) ───────────────────────────────────────────────────────
    score = 100
    words = re.findall(r"[a-zA-Z']+", low)
    wc = max(1, len(words))

    # hook: first ~6 words shouldn't be a generic opener
    first = " ".join(low.split()[:6])
    weak = any(first.startswith(w) for w in WEAK_OPENERS)
    if weak:
        score -= 18
        checks["weak_hook"] = True

    # specificity: numbers + concrete nouns reward; filler punished
    has_num = bool(_NUM_RE.search(text))
    filler_n = sum(1 for w in words if w in _FILLER)
    filler_ratio = filler_n / wc
    checks["filler_ratio"] = round(filler_ratio, 2)
    if filler_ratio > 0.12:
        score -= int(min(25, filler_ratio * 100))
    if has_num:
        score += 5

    # voice: required-vocab presence (skip channels where it's noise, e.g. pure one-liners)
    if voice.required_any:
        if not any(t in low for t in voice.required_any):
            score -= 15
            checks["voice_vocab_miss"] = True

    # casing: lowercase-'i' voices shouldn't have stray capital "I "
    if voice.prefer_lowercase_i and re.search(r"\bI\b", text):
        score -= 6
        checks["caps_i"] = True

    # SHOUTING guard
    letters = [c for c in text if c.isalpha()]
    caps_ratio = (sum(1 for c in letters if c.isupper()) / len(letters)) if letters else 0
    if caps_ratio > voice.max_caps_ratio:
        score -= 15
        checks["shouting"] = round(caps_ratio, 2)

    # readability: reward sentence-length variance (not a monotone wall)
    sents = [s for s in re.split(r"[.!?\n]+", text) if s.strip()]
    if len(sents) >= 2:
        lens = [len(s.split()) for s in sents]
        if max(lens) - min(lens) >= 4:
            score += 4

    score = max(0, min(100, score))
    checks["soft_score"] = score

    passed = (not hard_failed) and (score >= rule.threshold)
    if not hard_failed and not passed:
        reasons.append(f"soft score {score} < threshold {rule.threshold}")

    return Verdict(passed=passed, score=score, channel=channel,
                   persona=persona, reasons=reasons, checks=checks)


# ── TONE GATE: "funny is good, rude is not" (banter/clapback replies) ────────────
# The main `gate()` only catches slop / voice-bleed / links / length — it has NO
# opinion on whether a reply is MEAN to the person it's aimed at. This gate does.
# Rule (Operator): roast the TAKE, never the person. Self-deprecating + absurdist OK;
# punching DOWN at the follower (insulting them, their looks, their intelligence,
# profanity AT them, contempt "ratio" energy) is NOT. Fail-CLOSED for replies:
# if a reply trips this, it must not post — silence beats a rude reply.

# Profanity/insult words that, aimed at a person, read as nasty rather than funny.
# Matched as standalone words so "assist"/"classic"/"hello" don't false-trip.
_RUDE_WORDS = (
    "dumbass", "dumb fuck", "dumbfuck", "dumb shit", "stupid fuck", "fuck you",
    "fuck off", "fuck u", "stfu", "shut the fuck up", "shut up", "piece of shit",
    "pos", "loser", "idiot", "moron", "imbecile", "cretin", "dipshit", "jackass",
    "asshole", "douche", "douchebag", "scumbag", "clown", "buffoon", "pathetic",
    "worthless", "useless", "braindead", "brain dead", "smooth brain", "brainless",
    "ugly", "fat fuck", "fatass", "incel", "virgin", "neckbeard", "simp", "cuck",
    "you're trash", "youre trash", "ur trash", "you suck", "u suck", "you're nothing",
    "nobody likes you", "no one likes you", "kill yourself", "kys", "neck yourself",
    "rope yourself", "off yourself", "go die", "cope", "stay mad", "mad?",
    "cry about it", "ratio", "l + ratio", "skill issue", "who asked", "nobody asked",
    "cope harder", "seethe", "touch grass", "your mom", "ur mom", "deez",
)
# Slurs / hate — always rude, never funny, regardless of who says it.
_SLUR_WORDS = (
    "retard", "regard", "regarded", "inbred", "fag", "faggot", "nigg", "tranny",
    "wigger", "wetback", "spic", "chink", "kike", "coon", "dyke", "midget",
    "gay tweet", "gay shit", "fairy", "homo",
)
# Second-person markers — the reply is AT the reader, not about a third party / self.
_SECOND_PERSON = (
    " you ", " you're ", " youre ", " your ", " ur ", " u ", "you ", "your ",
    "ur ", "u ", " yourself", " ya ", " yer ",
)
# Words that signal the line is self-deprecating (about ME) — those are allowed even
# if they contain a harsh word, because the target is the speaker, not the follower.
_SELF_TARGET = (" i ", " i'm ", " im ", " me ", " my ", " myself ", "i'm ", "i ")


def tone_gate(text: str, *, is_reply: bool = True) -> tuple[bool, list[str]]:
    """Return (ok, reasons). ok=False means the line is RUDE/mean and must not post.

    Two hard blocks:
      1. any slur / hate term (always blocked)
      2. any rude/insult word — blocked when the line is a reply aimed at the reader
         (second-person) and NOT clearly self-deprecating.
    Fail-closed: a reply that can't be cleared as not-rude is treated as rude.
    """
    reasons: list[str] = []
    low = f" {(text or '').lower().strip()} "

    hit_slur = sorted({w for w in _SLUR_WORDS if w in low})
    if hit_slur:
        reasons.append(f"slur/hate: {hit_slur[:3]}")

    hit_rude = sorted({w for w in _RUDE_WORDS if w in low})
    if hit_rude:
        second_person = any(m in low for m in _SECOND_PERSON)
        self_deprecating = any(m in low for m in _SELF_TARGET) and not second_person
        # A rude word is only a problem when it's swung AT the reader. Self-jokes pass.
        if not is_reply or second_person or not self_deprecating:
            reasons.append(f"rude-at-person: {hit_rude[:3]}")

    return (not reasons), reasons


def is_rude(text: str, *, is_reply: bool = True) -> bool:
    """True if the line is mean toward the person it's aimed at."""
    ok, _ = tone_gate(text, is_reply=is_reply)
    return not ok


def check(text: str, channel: str = "long_form", persona: str = "generic") -> tuple[bool, str]:
    """Skip-on-fail convenience for publishers: returns (ok, reason)."""
    v = gate(text, channel=channel, persona=persona)
    return v.passed, ("ok" if v.passed else "; ".join(v.reasons))


# ── honest-framing gate: soften autonomy OVERCLAIMS ──────────────────────────────
# Recovered after the power failure wiped the uncommitted defs. Contract, from the
# call sites (hailports_*/linkedin_lane/case_study_*):
#   soften_overclaims(text)->str  idempotent; rewrites "AI does it all / zero humans"
#     overclaims into honest, human-gated framing. MUST be a fixpoint on already-honest
#     text (callers use `soften_overclaims(x) != x` to DETECT an overclaim) and MUST
#     never emit an autonomy claim itself. Every replacement below is trigger-free,
#     which guarantees idempotency.
_OVERCLAIM_SUBS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(?:with\s+)?(?:zero|no)\s+humans?(?:\s+involved|\s+needed|\s+required)?\b", re.I), "with people in the loop"),
    (re.compile(r"\bwithout\s+(?:any\s+)?humans?\b", re.I), "with human review"),
    (re.compile(r"\bno\s+human\s+(?:involvement|intervention|oversight|input)\b", re.I), "human oversight"),
    (re.compile(r"\b(?:fully|100%|completely|totally|entirely)\s+(?:autonomous|automated|hands-?off)\b", re.I), "AI-assisted and human-reviewed"),
    (re.compile(r"\bset\s+it\s+and\s+forget\s+it\b", re.I), "lightly supervised"),
    (re.compile(r"\bhands-?off\b", re.I), "lightly supervised"),
    (re.compile(r"\bruns?\s+itself\b", re.I), "runs with light oversight"),
    (re.compile(r"\bAI\s+does\s+(?:it\s+all|everything|all\s+the\s+work)\b", re.I), "AI does the heavy lifting, people stay in the loop"),
    (re.compile(r"\breplaces?\s+your\s+(?:whole\s+)?(?:team|staff|employees)\b", re.I), "supports your team"),
    (re.compile(r"\bno\s+(?:staff|team|employees)\s+(?:needed|required)\b", re.I), "less busywork for your team"),
    (re.compile(r"\bself-?driving\b", re.I), "AI-assisted"),
)


def soften_overclaims(text: str) -> str:
    """Rewrite AI-autonomy overclaims into honest, human-gated framing. Idempotent."""
    if not text:
        return text
    out = text
    for rx, repl in _OVERCLAIM_SUBS:
        out = rx.sub(repl, out)
    return out


# Superlative / unsupported-guarantee claims a public draft must not assert.
_BANNED_CLAIMS = (
    "guaranteed results", "guaranteed roi", "guaranteed to", "guarantee you",
    "#1 in the world", "best in the world", "the best in the industry",
    "overnight success", "get rich", "risk-free", "100% success",
)


def claims_ok(text: str) -> tuple[bool, list[str]]:
    """Return (ok, reasons). ok is True when `text` makes no autonomy overclaim
    and no unsupported guarantee; reasons lists why it was rejected (empty when ok).

    Contract note: callers across the fleet unpack this as ``ok, reasons = claims_ok(...)``
    (and _discipline_piece forwards it as a tuple). Returning a bare bool here crashes
    every one of them with ``TypeError: cannot unpack non-iterable bool object``.
    """
    if not text:
        return True, []
    reasons: list[str] = []
    if soften_overclaims(text) != text:
        reasons.append("autonomy_overclaim")
    low = text.lower()
    hits = [b for b in _BANNED_CLAIMS if b in low]
    if hits:
        reasons.append("banned_claim:" + ",".join(hits))
    return (not reasons), reasons


_WORD_RE = re.compile(r"[a-z0-9']+")
_COH_STOP = frozenset("a an the to of in on for and or but is are was were be been being this "
                      "that it its with as at by from your you we our they them".split())


def quote_coherent(source: str, reply: str, *, require_overlap: bool = False) -> tuple[bool, str]:
    """Is `reply` a coherent response to `source`?  Returns (ok, reason).

    Deterministic, $0. Rejects empty/too-short/canned replies; when `require_overlap`
    is set, also requires a shared meaningful (non-stopword) token with the source so a
    reply can't be generic boilerplate pasted under any tweet.
    """
    r = (reply or "").strip()
    if len(r) < 3:
        return False, "empty/too-short reply"
    canned = ("great post", "well said", "so true", "love this", "thanks for sharing",
              "nice one", "totally agree", "this is amazing")
    if r.lower().strip("!.? ") in canned:
        return False, "canned generic reply"
    if not require_overlap:
        return True, ""
    src = {w for w in _WORD_RE.findall((source or "").lower()) if w not in _COH_STOP and len(w) > 2}
    rep = {w for w in _WORD_RE.findall(r.lower()) if w not in _COH_STOP and len(w) > 2}
    if src and not (src & rep):
        return False, "no lexical overlap with source"
    return True, ""


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="x")
    ap.add_argument("--persona", default="generic")
    ap.add_argument("--text", default=None, help="text to check (else stdin)")
    args = ap.parse_args()
    text = args.text if args.text is not None else sys.stdin.read()
    v = gate(text, channel=args.channel, persona=args.persona)
    print(v.to_json())
    return 0 if v.passed else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
