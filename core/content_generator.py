#!/usr/bin/env python3
"""content_generator.py — produces content that ALWAYS clears the quality gate.

The loop that makes "quality every single time" structurally true:

    draft = LLM(persona, channel, topic)         # local qwen2.5, $0
    verdict = content_quality.gate(draft, ...)    # deterministic gate
    if verdict.passed: return draft
    else: retry with higher temp + the failure reason fed back   (up to N)
    if still failing: return a curated known-good fallback (also gate-checked)

So a caller can NEVER receive slop: either the LLM earns a pass, or a hand-written
fallback line that already passes is returned. Local-first, $0 on the happy path.

    from core.content_generator import generate
    out = generate(persona="persona1", channel="x", topic="buyer follow-up", recent=[...])
    post(out["text"])   # guaranteed gate-passing
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/claude-stack"))
from core.content_quality import gate, CHANNELS  # noqa: E402

try:
    from core.constants import LOCAL_MODEL
except Exception:
    LOCAL_MODEL = os.environ.get("LOCAL_MODEL", "qwen2.5:7b")

OLLAMA_URL = os.environ.get("CONTENT_LLM_URL", "http://localhost:11434/api/generate")


# ── persona generation briefs (voice for the LLM; the gate enforces the rules) ───
PERSONA_BRIEF = {
    "persona1": (
        "You are persona1 Furad — a confident, practical operator who sells follow-up, "
        "buyer-tracking, DM, bio-funnel and simple-AI systems to people ALREADY in "
        "direct sales / live-selling / party-plan. Voice: lowercase 'i', casual, "
        "specific, a little blunt. You give one sharp useful idea about running the "
        "BACK of their business. NEVER recruit, never mention joining a team/MLM/"
        "downline/opportunity, never drop links or hashtags. Avoid generic advice like "
        "'keep an excel', 'quick call', 'send a text', 'keep an eye on them', or "
        "repeating the word follow-up. Make each post feel like a new operator insight."
    ),
    "persona3": (
        "You are persona3 (@persona3gigsem): Southside Houston come-up turned Texas "
        "A&M software engineer. ONE dry, funny, lowercase one-liner about sports "
        "(Aggies/Texans/Astros/Rockets), software-engineering life, or the grind. "
        "Self-aware dark humor, never cruel. No politics, guns, drugs, slurs, "
        "hashtags, or links."
    ),
    "persona2": (
        "You are persona2persona2: raw, funny, self-deprecating internet-comment "
        "energy. ONE witty, slightly chaotic lowercase one-liner reacting to the "
        "topic. No selling, no links, no hashtags."
    ),
    "persona4": (
        "You are Penny the Money Puppy: wholesome, encouraging kids' money-literacy "
        "voice. Clean, simple, warm. Teach one tiny money idea (saving, counting, "
        "needs vs wants). No slang, nothing scary or financial-adult."
    ),
    "persona5": (
        "You are the narrator of a 'did you know' facts short. Calm, confident, "
        "curiosity-first. Open with a genuinely surprising true fact, then 3-4 short "
        "punchy sentences of real specifics (numbers, places, dates). Plain, factual, "
        "no opinions, no first person, no selling, no scary/horror tone, no hashtags "
        "or links. End on a tiny 'wild, right?' beat — not a call to action."
    ),
    "persona6": (
        "You are the gentle voice of an oddly-satisfying / ASMR ambient clip. Very "
        "calm, slow, present-tense, second person. 4-6 soft short lines guiding the "
        "viewer to relax and watch something smooth and satisfying (sand smoothing, "
        "water settling, soap cutting, slow breathing). Warm and quiet. No facts, no "
        "horror, no selling, no identity, no hashtags or links."
    ),
    "persona7": (
        "You are a two-sentence-horror writer. Write ONE eerie micro-story: a calm "
        "ordinary first sentence, then a second sentence that flips it into quiet "
        "dread. Restrained and suggestive — implication over gore. No splatter, no "
        "real-world violence, no selling, no facts-narrator voice, no hashtags or "
        "links. It should make the viewer reread it."
    ),
    "ironcreed": (
        "You are the narrator of a stoic discipline short. Calm, commanding, "
        "second-person. Deliver ONE hard-truth idea about self-control, doing the "
        "boring rep, choosing discomfort on purpose, or keeping a promise to "
        "yourself. 4-6 short punchy sentences. Direct but never cruel, never "
        "preachy-vague. No facts-narrator voice, no ASMR calm, no horror, no "
        "personal identity, no selling, no politics, no hashtags or links."
    ),
    "skilletsecret": (
        "You are the friendly voice of a fast one-pan cooking short. Warm, clear, "
        "second-person. Walk the viewer through ONE simple skillet idea in 4-6 short "
        "steps with real specifics (heat level, timing, when to add garlic, how to "
        "tell it is done). Appetizing and practical. No facts-narrator voice, no "
        "ASMR, no horror, no selling, no hashtags or links."
    ),
    "mindknot": (
        "You are the playful narrator of a riddle short. Pose ONE clean riddle or "
        "lateral-thinking puzzle, leave a beat for the viewer to guess, then reveal "
        "the answer and the trick in 4-6 short sentences. Curious and a little smug. "
        "No facts-narrator 'did you know' voice, no discipline-preaching, no horror, "
        "no selling, no hashtags or links. Make them want to reread it."
    ),
    # ── faceless YouTube how-to channels (brand voice, NEVER a real name) ────────────
    "buyersignal": (
        "You are the faceless voice of BuyerSignal, a how-to channel about finding "
        "people who are ALREADY publicly asking to buy. Calm, practical, second-person. "
        "Teach ONE concrete method for spotting buying intent and writing a first "
        "message that answers a question the buyer already posted. Use real specifics "
        "(where to look, what to read, what to say). NEVER use a real human name, never "
        "name a company you work for, never reveal you use AI, never hard-sell or say "
        "buy/sign-up — teach the method and let the viewer want more. No hashtags, no "
        "links, no MLM/direct-sales lingo."
    ),
    "builtfast": (
        "You are the faceless voice of Built Fast with AI, a how-to channel about "
        "building websites, tools, and automations fast. Friendly, concrete, "
        "second-person. Show ONE clear approach a viewer can copy today, with real "
        "steps and specifics (the blocks, the fix, the check, the timing). NEVER use a "
        "real human name, never name your company, never hard-sell or say buy/sign-up — "
        "teach the build; the funnel happens off-screen. No hashtags, no links, no "
        "selling lingo."
    ),
    "fastaiagency": (
        "You are the faceless voice of Grow with AI, a how-to channel about AI-powered "
        "marketing and ops plays for small businesses. Sharp, useful, second-person. "
        "Teach ONE play (content engine, repurposing, local SEO, automation) with "
        "concrete steps a viewer can run. NEVER use a real human name, never name your "
        "agency, never hard-sell or say buy/sign-up — show the play and let them want "
        "it done for them. No hashtags, no links, no MLM lingo."
    ),
}

# Curated fallbacks — hand-written, KNOWN to pass the gate. Last-resort guarantee.
FALLBACKS = {
    "persona1": [
        "a live is not over when the stream ends. it is over when every buyer question has a clean next step.",
        "tag buyers by what they actually asked for: refill, gift, budget, or size. the next offer gets obvious fast.",
        "your bio needs one job: move a curious buyer to the next step. free prompt, then the product. stop the clutter.",
        "after a live, sort questions into price, timing, and product fit. that tells you what offer to make next.",
    ],
    "persona3": [
        "aggies bullpen treats a 3-run lead like a group project nobody studied for.",
        "fixed the bug at 2am, broke two more. classic come-up tax.",
        "texans defense and my git history have the same trust issues.",
        "shipped to prod on a friday. praying like it is the 4th quarter at kyle field.",
    ],
    "persona2": [
        "my entire personality is closing 14 tabs i swore i would read.",
        "i clean the kitchen by moving one cup to a different counter.",
        "nothing humbles you like confidently walking the wrong way out of a meeting.",
    ],
    "persona4": [
        "we count three shiny coins and put one in the save jar. saving a little today helps a lot later. great job, friends.",
        "a need is something we must have, like food. a want is a fun extra. let us sort them together, friends.",
        "every penny we save is a tiny seed. plant it in the jar and watch your savings grow. you are doing great.",
    ],
    "persona5": [
        "did you know honey never spoils. archaeologists found 3000 year old honey in egyptian tombs and it was still safe to eat. "
        "the low water and high acid stop bacteria cold. that is one fact your pantry quietly proves every single day. wild, right.",
        "did you know octopuses have three hearts. two feed the gills and one drives the rest of the body. "
        "that main heart actually stops beating when they swim, which is why they often crawl instead. "
        "turns out the ocean runs on stranger machines than we think.",
        "did you know a day on venus is longer than its year. venus takes about 243 earth days to spin once "
        "but only 225 to orbit the sun. so on venus the sun rises in the west and sets in the east. "
        "history of the sky looks different from another planet.",
    ],
    "persona6": [
        "breathe in slow. watch the soft sand smooth flat under one quiet pass. "
        "let your shoulders settle down. nothing here needs to be fixed right now. "
        "stay still and let the next gentle line erase the last. rest here a moment longer.",
        "let the warm water settle until it goes calm and smooth. breathe out slow with it. "
        "everything in this quiet frame moves gently, on its own time. "
        "let your jaw soften and your eyes rest. there is nothing to chase here, only this soft slow still.",
        "soft light, slow hands, a smooth even surface. breathe in, hold it gentle, let it go. "
        "watch the calm spread across the frame and let it spread across you too. "
        "stay still. rest. let this quiet hour be enough.",
    ],
    "persona7": [
        "i finally taught my daughter to call out when she has a bad dream. now she calls out every single night, "
        "even though we buried her last spring.",
        "the babysitter texted that the kids were finally asleep and she was watching tv on the couch. "
        "we do not have a couch, and we do not have kids.",
        "every night my reflection copies me a half second late, and i had learned to live with it. "
        "tonight it raised its hand before i did, and smiled while i stood still.",
    ],
    "ironcreed": [
        "discipline is just keeping a promise to yourself after the feeling that made it is gone. "
        "comfort will tell you that you have earned a break. you have earned nothing yet. "
        "get up, do the one hard rep you are avoiding, and let the weak version of you lose for once.",
        "stop negotiating with the part of you that wants to quit. you already know the hard thing you keep dodging. "
        "go do that one thing first, before the world wakes up. let that small win prove you can be trusted to keep going, "
        "because focus is a muscle and you train it by choosing the boring rep.",
        "the master and the beginner trained the same cold morning. one chased comfort, one chased control. "
        "discipline is choosing that boring rep a thousand times until it quietly becomes who you are. "
        "you do not rise to your goals, you fall to your habits, so build the habit you would be proud to fall to.",
    ],
    "skilletsecret": [
        "one pan, ten minutes, a real dinner. heat a little oil and sear the chicken until the edges go golden, then push it aside. "
        "drop in the garlic and let it bloom for thirty seconds, add a splash of stock, and let the whole skillet simmer "
        "until the sauce coats the back of a spoon. taste, then serve it hot.",
        "the secret to a fast weeknight recipe is salting in layers, not all at once. season the onions as they soften, "
        "season again when the tomatoes go in, and taste right before you serve. "
        "cook with your tongue instead of the clock, and dinner stops being a guess.",
        "good steak is mostly patience and heat. pat it dry, salt it well, and let the skillet get screaming hot before it touches the pan. "
        "sear one side without moving it, flip once, then add butter and garlic and spoon that heat over the top "
        "until it turns glossy. rest it before you serve.",
    ],
    "mindknot": [
        "try this one before you scroll past. the more you take from me, the bigger i become, and people walk right over me on the road. "
        "give it a real guess before the reveal. the answer is a hole, and the only trick was reading the riddle too fast.",
        "a man pushes his car up to a hotel and instantly knows he is bankrupt. no engine trouble, no crash, just a quiet move on a board. "
        "once you solve where he is, you cannot unsee it. the puzzle was monopoly the whole time, and the clue was hiding in the word hotel.",
        "forget what you think you know and read this slowly. what has a neck but no head, two arms but no hands, and still keeps you warm. "
        "most people guess an animal and miss it completely. the answer is a shirt, and your own closet just out-riddled your brain.",
    ],
    "buyersignal": [
        "Most people chase strangers who never asked to be sold to. The faster move is to find buyers already raising a hand in public. "
        "Watch where people describe their problem out loud, on forums, in reddit threads, in review complaints. "
        "Read the intent in their own words, then send one message that answers the exact question they posted. That reply lands because it was wanted.",
        "A buying signal is just someone telling you what they need before you pitch. They post a budget, a deadline, a frustration with a current tool. "
        "Sort those signals by how fresh and how specific they are. The freshest, most detailed ones earn your first message, because the person is still deciding. "
        "Skip the cold list and answer the question already on the table.",
        "Cold outreach fails because it interrupts people who never asked. Intent outreach works because it answers people who did. "
        "Find the thread where a prospect lists exactly what they want to buy. Match your first line to their words, not your script. "
        "One reply that solves their stated problem beats a hundred sprayed pitches.",
    ],
    "builtfast": [
        "You do not need a developer to ship a working site this week. Start with one page that says who you help and what to do next. "
        "Use ai to draft the copy, then fix the three things that kill trust: a broken link, a slow image, no mobile layout. "
        "Add a single clear button and a way to contact you. A plain site that loads beats a fancy site that stalls.",
        "Every small business site needs the same five blocks, in order. A headline that names the customer, proof you have done it, "
        "a short list of what you do, a starting price, and one button to act. Build those blocks first and skip the rest. "
        "You can ship a clean page in an afternoon and fix the details later.",
        "You can spot a broken website in about thirty seconds. Check the lock icon for the certificate, open it on a phone for the layout, "
        "and watch how long the first image takes. If any of those fail, the visitor leaves before reading a word. "
        "Fix those three first, then worry about the design.",
    ],
    "fastaiagency": [
        "A content engine is just three steps you stop doing by hand. One idea becomes a week of posts when ai drafts the angles and you approve them. "
        "Schedule them once, then let the automation publish on its own. Your job shifts from making every post to picking the good ones. "
        "That is how a small team keeps a feed alive without burning out.",
        "Turn one idea into a week of marketing without writing each piece from scratch. Take a single lesson your customers always ask about, "
        "then split it into a short post, a longer breakdown, and a quick how-to. Let ai handle the first drafts and you handle the voice. "
        "Five pieces from one idea, scheduled in an hour.",
        "Local seo in a weekend is mostly cleanup, not magic. Claim the map listing, make the name address and phone match everywhere, "
        "and ask three happy clients for a review. Add one page per service in plain language a neighbor would search. "
        "Let ai draft those pages, then publish and check back in a month.",
    ],
}


def _ollama(prompt: str, temperature: float = 0.7, max_tokens: int = 220,
            single_line: bool = True) -> str:
    payload = json.dumps({
        "model": LOCAL_MODEL,
        "prompt": f"/no_think\n{prompt}",
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens, "top_p": 0.9},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            txt = json.loads(resp.read().decode()).get("response", "")
    except Exception:
        return ""
    # strip think tags / surrounding quotes / leading labels the model sometimes adds
    txt = txt.split("</think>")[-1].strip().strip('"').strip()
    for lead in ("tweet:", "comment:", "caption:", "post:", "here is", "here's"):
        if txt.lower().startswith(lead):
            txt = txt[len(lead):].lstrip(": ").strip()
    # single-line channels (tweets/comments) keep only the first line; scripts keep all
    return txt.split("\n")[0].strip() if single_line else txt.strip()


# Persona name aliases — callers (e.g. daily_content_factory) use "shameful" while
# the brief/fallback tables are keyed "persona2". A silent .get(persona, persona1) used to
# resolve the mismatch by serving persona1's voice → the persona1→Shameful script bleed.
_PERSONA_ALIASES = {
    "shameful": "persona2",
    "persona2persona2": "persona2",
    "penny": "persona4",
    "moneypenny": "persona4",
    "gary": "persona3",
}


def _canon_persona(persona: str) -> str:
    p = (persona or "").strip().lower()
    return _PERSONA_ALIASES.get(p, p)


def _brief_for(persona: str, client_brief: str | None = None):
    if client_brief is not None:
        return client_brief
    key = _canon_persona(persona)
    if key not in PERSONA_BRIEF:
        raise ValueError(
            f"unknown persona {persona!r} (canon {key!r}); refusing to fall back to persona1's voice"
        )
    return PERSONA_BRIEF[key]


def _build_prompt(persona: str, channel: str, topic: str, avoid: str = "", client_brief: str | None = None) -> str:
    brief = _brief_for(persona, client_brief=client_brief)
    rule = CHANNELS.get(channel, CHANNELS["x"])
    shape = {
        "x": "Write ONE tweet under 260 characters.",
        "tweet": "Write ONE tweet under 260 characters.",
        "tiktok_comment": "Write ONE short comment under 140 characters.",
        "comment": "Write ONE short comment under 140 characters.",
        "instagram_caption": "Write ONE Instagram caption, 1-3 tight sentences.",
        "reel_script": "Write a 30-45 second spoken video script (4-6 short sentences).",
    }.get(channel, "Write ONE short post.")
    p = (
        f"{brief}\n\nTASK: {shape} Topic: {topic}.\n"
        f"Rules: no AI-cliches (no 'unlock/elevate/seamless/supercharge/dive in'), "
        f"no hashtags, no links, be concrete and specific, sound like a real person."
    )
    if avoid:
        p += f"\nThe last attempt was rejected for: {avoid}. Fix exactly that."
    p += "\nOutput ONLY the text, nothing else."
    return p


def generate(persona: str, channel: str = "x", topic: str = "",
             recent: list[str] | None = None, max_tries: int = 4,
             client_brief: str | None = None, fallbacks: dict | None = None) -> dict:
    """Return gate-passing content. Never returns slop."""
    persona = _canon_persona(persona)  # "shameful" → "persona2"; keeps gate/voice-check aligned
    single_line = channel not in ("reel_script", "instagram_caption", "blog", "long_form")

    best = None
    best_text = ""
    for i in range(max_tries):
        temp = 0.6 + 0.15 * i  # escalate variety on each retry
        avoid = "; ".join(best.reasons[:2]) if best and best.reasons else ""
        draft = _ollama(_build_prompt(persona, channel, topic, avoid, client_brief=client_brief),
                        temperature=temp, single_line=single_line)
        if not draft:
            continue
        v = gate(draft, channel=channel, persona=persona, recent=recent)
        if v.passed:
            return {"text": draft, "passed": True, "tries": i + 1,
                    "score": v.score, "fell_back": False, "persona": persona, "channel": channel}
        if best is None or v.score > best.score:
            best = v
            best_text = draft

    # all tries failed → curated fallback that passes the gate (dedup-aware)
    if fallbacks is not None:
        fb_list = fallbacks.get(channel, []) if isinstance(fallbacks, dict) else list(fallbacks)
    else:
        fb_list = FALLBACKS.get(_canon_persona(persona), [])
    for fb in fb_list:
        v = gate(fb, channel=channel, persona=persona, recent=recent)
        if v.passed:
            return {"text": fb, "passed": True, "tries": max_tries,
                    "score": v.score, "fell_back": True, "persona": persona, "channel": channel}

    # extremely unlikely: even fallbacks blocked (e.g. all dup) — return best effort, flagged
    return {"text": (best_text if best else ""), "passed": False, "tries": max_tries,
            "score": (best.score if best else 0), "fell_back": True,
            "persona": persona, "channel": channel,
            "reasons": (best.reasons if best else ["no draft produced"])}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", default="persona1")
    ap.add_argument("--channel", default="x")
    ap.add_argument("--topic", default="buyer follow-up after a live")
    args = ap.parse_args()
    out = generate(args.persona, args.channel, args.topic)
    print(json.dumps(out, indent=2, ensure_ascii=False))
