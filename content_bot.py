#!/usr/bin/env python3
"""
Peptide Frontier — daily medical-content X bot.

Pipeline (see STRATEGY.md §4):
  1. select pillar (by weekday) + a subtopic not used recently
  2. ground it against real literature (Europe PMC, free)
  3. draft with an LLM using system_prompt.md (Gemini by default)
  4. run deterministic guardrails (length / banned phrases / no-advice / dedupe)
  5. post text-only to X (+ optional Nostr) and commit history

Design mirrors the weather-bot: GitHub Actions cron -> this script -> tweepy.
Everything is non-fatal where reasonable; a bad LLM draft falls back to a
safe template rather than crashing the run.
"""
import os
import sys
import json
import re
import time
import random
import logging
import argparse
import datetime as dt
from pathlib import Path
from difflib import SequenceMatcher

import requests

try:
    import tweepy
except ImportError:
    tweepy = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ----------------------------------------------------------------------------- config
BASE = Path(__file__).resolve().parent
TOPICS_FILE = BASE / "data" / "topics.json"
HISTORY_FILE = BASE / "data" / "posted_history.json"
SYSTEM_PROMPT_FILE = BASE / "system_prompt.md"

MAX_CHARS = 275                 # headroom under X's 280
DEDUPE_SIMILARITY = 0.72        # reject drafts this similar to a recent post
DEDUPE_WINDOW = 60              # compare against the last N posts
MAX_DRAFT_ATTEMPTS = 4

# weekday -> pillar. Study gets two slots (fresh literature is the strongest content).
PILLAR_BY_WEEKDAY = {
    0: "mechanism",   # Mon
    1: "numeracy",    # Tue
    2: "study",       # Wed
    3: "myth",        # Thu
    4: "study",       # Fri
    5: "landscape",   # Sat
    6: "numeracy",    # Sun
}

BANNED = [
    "miracle", "game-changer", "game changer", "glow up", "glow-up", "insane",
    "crazy", "secret", "life-changing", "life changing", "must-try", "must try",
    "buy ", "buy now", "cop ", "add to cart", "coupon", "discount code",
    "dm me", "link in bio", "affiliate", "use code", "shop now", "they don't want you to know",
]
# first/second-person prescriptive advice — education framing only
ADVICE_PATTERNS = [
    r"\byou should (take|dose|inject|start|stop|use)\b",
    r"\btake \d+\s?(mg|mcg|iu)\b",
    r"\bi recommend\b",
    r"\byour dose (should|is)\b",
]

log = logging.getLogger("peptide_bot")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ----------------------------------------------------------------------------- state
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_history():
    return load_json(HISTORY_FILE, {"posts": []})


def save_history(history):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def recent_hooks(history, n=DEDUPE_WINDOW):
    return [p["text"] for p in history["posts"][-n:]]


def recent_topics(history, n=25):
    return {p.get("topic", "") for p in history["posts"][-n:]}


# ----------------------------------------------------------------------------- stage 1: topic
def select_topic(pillar, history):
    topics = load_json(TOPICS_FILE, {})["pillars"]
    if pillar not in topics:
        pillar = "numeracy"
    subs = topics[pillar]["subtopics"]
    used = recent_topics(history)
    fresh = [s for s in subs if s["topic"] not in used]
    pool = fresh or subs
    choice = random.choice(pool)
    log.info("Pillar=%s | topic=%s", pillar, choice["topic"][:70])
    return pillar, choice


# ----------------------------------------------------------------------------- stage 2: grounding
def ground_europepmc(query, limit=3):
    """Fetch a few real papers from Europe PMC (free, no key). Returns fact strings."""
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {
        "query": query,
        "format": "json",
        "pageSize": limit,
        "sort": "P_PDATE_D desc",   # newest first
        "resultType": "core",
    }
    facts = []
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        for res in r.json().get("resultList", {}).get("result", []):
            title = (res.get("title") or "").strip().rstrip(".")
            journal = (res.get("journalTitle") or res.get("bookOrReportDetails", {}) or "").strip() \
                if isinstance(res.get("journalTitle"), str) else ""
            year = res.get("pubYear", "")
            if title:
                src = f"{journal} {year}".strip()
                facts.append(f"- \"{title}\"" + (f" ({src})" if src else ""))
        log.info("Grounding: %d papers for query '%s'", len(facts), query[:50])
    except Exception as e:  # non-fatal — evergreen fallback
        log.warning("Grounding failed (%s); writing evergreen from established science.", e)
    return facts


# ----------------------------------------------------------------------------- stage 3: drafting
def read_system_prompt():
    try:
        return SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "You write cited, numerate, neutral peptide-education posts under 275 chars. No advice, no selling, no hype."


def build_user_prompt(pillar, topic, facts, avoid):
    facts_block = "\n".join(facts) if facts else "(No fresh papers retrieved — write an evergreen post from established, non-controversial science and stay conservative.)"
    avoid_block = "\n".join(f"- {h}" for h in avoid[-12:]) if avoid else "(none)"
    return (
        f"PILLAR: {pillar}\n"
        f"TOPIC: {topic}\n\n"
        f"GROUNDING FACTS (anchor to these; name the source for 'study' posts):\n{facts_block}\n\n"
        f"RECENT HOOKS TO NOT REPEAT:\n{avoid_block}\n\n"
        f"Write one post now. Output only the post text, <=275 characters."
    )


def draft_with_gemini(system_prompt, user_prompt):
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        return None
    try:
        # New google-genai SDK (the old google.generativeai is end-of-life).
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.9,
                max_output_tokens=400,
                # gemini-2.5-flash is a thinking model; thinking tokens would
                # otherwise eat the output budget and truncate the post.
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return (resp.text or "").strip()
    except Exception as e:
        log.warning("Gemini draft failed: %s", e)
        return None


def draft_with_openai(system_prompt, user_prompt):
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.8,
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.warning("OpenAI draft failed: %s", e)
        return None


def draft_template(pillar, topic, facts):
    """Deterministic, safe fallback so a run never fails for lack of an LLM.
    Intentionally plain; the LLM is what makes posts good."""
    src = ""
    if facts:
        m = re.search(r"\(([^)]+)\)\s*$", facts[0])
        if m:
            src = f" (see {m.group(1)})"
    lead = topic.split(" — ")[0].split(":")[0].strip()
    text = f"{lead}. What the evidence actually shows — decoded, not hyped{src}. Educational, not medical advice."
    return text[:MAX_CHARS]


def generate_draft(system_prompt, user_prompt, pillar, topic, facts):
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    order = {"gemini": [draft_with_gemini, draft_with_openai],
             "openai": [draft_with_openai, draft_with_gemini]}.get(provider, [draft_with_gemini, draft_with_openai])
    for fn in order:
        out = fn(system_prompt, user_prompt)
        if out:
            return clean_draft(out)
    log.warning("No LLM available — using safe template fallback.")
    return clean_draft(draft_template(pillar, topic, facts))


def clean_draft(text):
    text = text.strip().strip('"').strip()
    # strip accidental leading label like "Post:" and code fences
    text = re.sub(r"^```.*?\n|\n```$", "", text).strip()
    text = re.sub(r"^(post|draft|output)\s*:\s*", "", text, flags=re.I).strip()
    return text


# ----------------------------------------------------------------------------- stage 4: guardrails
def guardrail_check(text, history, pillar):
    reasons = []
    if not text:
        return False, ["empty"]
    if len(text) > MAX_CHARS:
        reasons.append(f"too long ({len(text)}>{MAX_CHARS})")
    low = text.lower()
    for b in BANNED:
        if b in low:
            reasons.append(f"banned phrase: '{b.strip()}'")
    for pat in ADVICE_PATTERNS:
        if re.search(pat, low):
            reasons.append(f"prescriptive advice: /{pat}/")
    if "http://" in low or "https://" in low:
        reasons.append("contains a link (must be text-only; link goes in a reply)")
    # near-duplicate check
    for prev in recent_hooks(history):
        if SequenceMatcher(None, low, prev.lower()).ratio() >= DEDUPE_SIMILARITY:
            reasons.append("near-duplicate of a recent post")
            break
    if pillar == "study":
        # require some source signal (a year, journal-ish, or 'trial'/'phase')
        if not re.search(r"\b(19|20)\d{2}\b|phase\s?\d|trial|journal|lancet|jama|nejm|lilly|novo", low):
            reasons.append("study post lacks a source/attribution signal")
    return (len(reasons) == 0), reasons


# ----------------------------------------------------------------------------- stage 5: publish
def get_x_client():
    keys = ["PF_X_API_KEY", "PF_X_API_SECRET", "PF_X_ACCESS_TOKEN", "PF_X_ACCESS_SECRET"]
    vals = [os.getenv(k, "") for k in keys]
    if not all(vals) or tweepy is None:
        return None
    return tweepy.Client(
        consumer_key=vals[0], consumer_secret=vals[1],
        access_token=vals[2], access_token_secret=vals[3],
    )


def post_to_x(client, text):
    log.info("Posting to X (%d chars)...", len(text))
    try:
        resp = client.create_tweet(text=text)
        tid = resp.data["id"]
        log.info("SUCCESS: X post published — id %s", tid)
        return tid
    except Exception as e:  # non-fatal, mirrors weather-bot
        log.warning("X post failed (%s). Non-fatal.", e)
        if hasattr(e, "response") and e.response is not None:
            log.warning("HTTP %s: %s", e.response.status_code, e.response.text[:300])
        return None


def post_source_reply(client, tweet_id, source_url):
    """Citations go in a REPLY, not the body — avoids the $0.20 link surcharge on the main post."""
    if not (client and tweet_id and source_url):
        return
    try:
        client.create_tweet(text=f"Source: {source_url}", in_reply_to_tweet_id=tweet_id)
        log.info("Posted source reply.")
    except Exception as e:
        log.warning("Source reply failed (%s). Non-fatal.", e)


def post_to_nostr(text):
    nsec = os.getenv("NOSTR_NSEC", "")
    if not nsec:
        return
    try:
        from basic_nostr import NostrClient
        with NostrClient(nsec) as n:
            n.make_post(text)
        log.info("SUCCESS: Nostr post published.")
    except Exception as e:
        log.warning("Nostr post failed (%s). Non-fatal.", e)


# ----------------------------------------------------------------------------- orchestration
def run(pillar=None, dry_run=False, source_url=""):
    history = load_history()
    if not pillar:
        pillar = PILLAR_BY_WEEKDAY[dt.datetime.utcnow().weekday()]
    pillar, choice = select_topic(pillar, history)

    facts = ground_europepmc(choice["query"])
    system_prompt = read_system_prompt()
    avoid = recent_hooks(history)

    text, reasons = None, ["not attempted"]
    for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
        user_prompt = build_user_prompt(pillar, choice["topic"], facts, avoid)
        candidate = generate_draft(system_prompt, user_prompt, pillar, choice["topic"], facts)
        ok, reasons = guardrail_check(candidate, history, pillar)
        log.info("Attempt %d: %s | %s", attempt, "PASS" if ok else "FAIL: " + "; ".join(reasons),
                 candidate[:90] if candidate else "")
        if ok:
            text = candidate
            break
        avoid = avoid + ([candidate] if candidate else [])

    if not text:
        # last resort: guaranteed-safe template
        text = clean_draft(draft_template(pillar, choice["topic"], facts))
        ok, reasons = guardrail_check(text, history, pillar)
        if not ok:
            log.error("Even template failed guardrails: %s. Skipping this run.", reasons)
            return False

    log.info("FINAL POST (%d chars):\n%s", len(text), text)

    if dry_run:
        log.info("[DRY RUN] Not posting. Not writing history.")
        return True

    client = get_x_client()
    tweet_id = None
    if client:
        tweet_id = post_to_x(client, text)
        if tweet_id and source_url:
            post_source_reply(client, tweet_id, source_url)
    else:
        log.warning("X credentials not set — skipping X post.")

    post_to_nostr(text)

    history["posts"].append({
        "date": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "pillar": pillar,
        "topic": choice["topic"],
        "text": text,
        "tweet_id": tweet_id,
    })
    save_history(history)
    log.info("Done. History now holds %d posts.", len(history["posts"]))
    return True


def main():
    setup_logging()
    ap = argparse.ArgumentParser(description="Peptide Frontier daily content bot")
    ap.add_argument("--pillar", choices=list(PILLAR_BY_WEEKDAY.values()) + [None], default=None,
                    help="override the weekday pillar (mechanism/numeracy/study/myth/landscape)")
    ap.add_argument("--dry-run", action="store_true", help="generate + validate but do not post")
    ap.add_