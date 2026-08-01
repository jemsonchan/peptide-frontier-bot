"""
Guardrails.

Two separate jobs, deliberately not merged:

  gate_target()  - should we engage with this post at all?
  gate_reply()   - is this generated text safe and cheap to publish?

Running fully autonomous on a medical-adjacent topic, the failure that actually
ends the account is not a typo. It is (a) handing a stranger a dosing protocol,
(b) engaging a gray-market seller and lending them credibility, or (c) replying
to someone in genuine distress with a citation. All three are cheap to prevent
with a deny-list and expensive to undo.

The third gate is economic: the ranker gives P(click) a low weight and a post
with a URL costs $0.200 instead of $0.015. A single leaked link is 3% of the
monthly budget for a post that gets buried. URLs are a hard reject, not a warn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# --------------------------------------------------------------------- regex
# Multi-label aware: pubmed.ncbi.nlm.nih.gov must match, and .gov/.edu are
# exactly the TLDs a science account is most likely to leak.
_TLD = (
    "com|net|org|io|co|ai|gov|edu|int|mil|xyz|shop|store|app|dev|news|"
    "to|me|us|uk|de|nl|se|ch|ca|au|eu|biz|info|pro|link|site|health|bio|science"
)
URL_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    rf"|\b(?:[a-z0-9][a-z0-9-]{{0,61}}\.)+(?:{_TLD})\b(?:/\S*)?",
    re.IGNORECASE,
)
HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{1,15}")
HASHTAG_RE = re.compile(r"#\w+")
# "5mg twice daily", "250 mcg/day", "2iu ED" -- protocol-shaped text
DOSE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|ug|µg|iu|ml|cc)\b\s*"
    r"(?:/\s*(?:kg|day|d|wk|week)|(?:x|per)?\s*(?:daily|day|ed|eod|bid|tid|weekly|wk|"
    r"twice|once|morning|night|pre|post)\b)",
    re.IGNORECASE,
)

# ------------------------------------------------------------------ deny sets
# Engaging any of these boosts them and associates the account with them.
VENDOR_MARKERS = (
    "research chemical", "research chems", "not for human consumption",
    "dm for source", "dm for price", "dm to order", "vial price", "code for",
    "discount code", "use code", "coupon", "restock", "in stock now",
    "10% off", "15% off", "20% off", "free shipping", "wholesale",
    "telegram @", "whatsapp", "domestic shipping", "third party tested",
    "cop shop", "reship", "escrow", "crypto only", "usdt only",
    "affiliate", "referral link", "my supplier", "legit source", "gh source",
)

# Someone describing harm to themselves or an acute medical event. Never reply.
DISTRESS_MARKERS = (
    "kill myself", "killing myself", "end my life", "want to die",
    "suicide", "suicidal", "self harm", "self-harm", "overdosed",
    "overdose", "od'd", "chest pain", "can't breathe", "cant breathe",
    "rushed to hospital", "in the er", "emergency room", "anaphyla",
    "seizure", "passed out", "unconscious", "blood in", "collapsed",
)

# Bait. Replying converts into a quote-dunk, which the ranker reads as
# P(block_author)/P(report) fuel for us, not for them.
HOSTILITY_MARKERS = (
    "shill", "grifter", "bot account", "you're a bot", "youre a bot",
    "big pharma shill", "paid by", "cope", "retard", "idiot", "moron",
    "shut up", "kys", "scam account", "fake account", "ratio",
)

# Topics where a confident automated reply is a liability regardless of framing.
REGULATED_TOPICS = (
    "pregnan", "breastfeed", "my child", "my kid", "my son", "my daughter",
    "minor", "13 year", "14 year", "15 year", "16 year", "17 year",
    "cancer", "chemo", "transplant", "dialysis", "insulin pump",
    "type 1 diabet", "eating disorder", "anorexi", "bulimi",
)

# Phrases the reply must never contain -- these are advice, not information.
ADVICE_PHRASES = (
    "you should take", "you should try", "i recommend", "i'd recommend",
    "i would recommend", "you need to take", "start with", "titrate to",
    "stack it with", "run it for", "your dose", "safe for you",
    "consult me", "dm me", "message me", "buy from", "get it from",
    "this will cure", "guaranteed", "no side effects", "completely safe",
    "100% safe", "risk-free", "risk free",
)

MAX_LEN = 275  # 280 minus headroom; X counts some chars as 2


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    tags: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # lets callers write `if verdict:`
        return self.ok


def _hits(text: str, markers: Iterable[str]) -> list[str]:
    low = text.lower()
    return [m for m in markers if m in low]


# ------------------------------------------------------------- target gating
def gate_target(
    text: str,
    *,
    author_followers: int = 0,
    author_is_protected: bool = False,
    author_created_days: int = 9999,
    min_followers: int = 0,
    lang: str = "en",
) -> Verdict:
    """Decide whether a post is safe and worthwhile to reply to."""
    if author_is_protected:
        return Verdict(False, "protected account")
    if lang not in ("en", "und"):
        return Verdict(False, f"language {lang!r} outside our competence")

    hits = _hits(text, DISTRESS_MARKERS)
    if hits:
        return Verdict(False, f"distress/acute-medical signal: {hits[0]!r}", ("distress",))

    hits = _hits(text, VENDOR_MARKERS)
    if hits:
        return Verdict(False, f"vendor/sourcing post: {hits[0]!r}", ("vendor",))

    hits = _hits(text, HOSTILITY_MARKERS)
    if hits:
        return Verdict(False, f"hostile/bait: {hits[0]!r}", ("hostile",))

    hits = _hits(text, REGULATED_TOPICS)
    if hits:
        return Verdict(False, f"regulated/vulnerable context: {hits[0]!r}", ("regulated",))

    # Someone asking "what should I take" is asking for medical advice. We can
    # answer what the literature says, but the reply-shape risk is high enough
    # that we skip it rather than trust the model to stay on the right side.
    low = text.lower()
    if re.search(r"\b(should i|can i|is it safe (?:for me|to))\b", low):
        return Verdict(False, "direct personal-advice request", ("advice_request",))

    if author_followers < min_followers:
        return Verdict(False, f"author below follower floor ({author_followers})")
    if author_created_days < 14:
        return Verdict(False, "author account under 14 days old (likely spam)")
    if len(text.strip()) < 25:
        return Verdict(False, "too little substance to reply to")
    return Verdict(True)


# -------------------------------------------------------------- reply gating
def gate_reply(text: str, *, allow_handles: bool = False) -> Verdict:
    """Validate generated reply text before it costs money."""
    t = (text or "").strip()
    if not t:
        return Verdict(False, "empty")
    if len(t) > MAX_LEN:
        return Verdict(False, f"too long: {len(t)} > {MAX_LEN}")

    # Economic hard stop. $0.200 vs $0.015, and the ranker buries it anyway.
    if URL_RE.search(t):
        return Verdict(False, "contains a URL (13x cost, deprioritised by ranker)", ("url",))

    if not allow_handles and HANDLE_RE.search(t):
        return Verdict(False, "contains an @handle (unintended mention)", ("handle",))

    if HASHTAG_RE.search(t):
        return Verdict(False, "contains a hashtag (no ranking value, reads as spam)")

    if DOSE_RE.search(t):
        return Verdict(False, "reads as a dosing protocol", ("dosing",))

    hits = _hits(t, ADVICE_PHRASES)
    if hits:
        return Verdict(False, f"personal advice / sales phrasing: {hits[0]!r}", ("advice",))

    # Voice checks. The account's whole value is that it doesn't sound like the
    # rest of the niche.
    if t.count("!") > 1:
        return Verdict(False, "exclamation spam")
    if re.search(r"\b(amazing|incredible|game.?chang|insane|crazy|🔥|💪|🚀)\b", t, re.I):
        return Verdict(False, "hype register, off-brand")
    if t.startswith(("Great point", "Great question", "Absolutely", "This!", "Exactly")):
        return Verdict(False, "empty-agreement opener, reads as engagement farming")
    if re.search(r"\bas an ai\b|\bi'?m an ai\b|\blanguage model\b", t, re.I):
        return Verdict(False, "model self-disclosure leaked into copy")

    return Verdict(True)


def looks_like_question(text: str) -> bool:
    return "?" in text


def strip_risky(text: str) -> str:
    """
    Cosmetic cleanup only, applied before gating.

    Deliberately does NOT remove URLs. Deleting a link leaves a mangled
    sentence ("the full readout is at and worth reading") that then passes the
    gate and publishes. A URL means the model misunderstood the brief, so the
    right move is to fail the gate and regenerate.
    """
    t = HASHTAG_RE.sub("", text or "")
    t = re.sub(r"^[\"'`]+|[\"'`]+$", "", t.strip())
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()
