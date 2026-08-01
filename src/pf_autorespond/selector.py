"""
Candidate scoring.

We can afford roughly five published actions a day. The job here is to spend
them on the five highest-expected-return targets out of everything harvested,
and to throw away the rest without spending a cent.

Scoring follows what the open-sourced ranker actually rewards:

  freshness   - AgeFilter drops old posts before scoring, so a reply to a
                6-hour-old post gets essentially no out-of-network reach.
                This is the heaviest term FOR COLD OUTBOUND.

                It is deliberately near-flat for mentions. A reply sitting on
                your own post reaches its author by notification, and a
                notification does not expire — answering a two-day-old comment
                still lands, still shows a reader that the account engages, and
                still keeps the thread alive. Applying feed-decay to it was a
                modelling error: it let a stranger's two-hour-old post outrank
                an unanswered reply on your own thread.
  conversation- posts already accumulating replies are in threads the ranker
                is already circulating. Joining early beats joining a dead post.
  reach       - author followers, log-scaled and capped. A 2M-follower account's
                reply section is a firehose nobody reads; the 5k-50k band is
                where a reply is visible AND the audience is relevant.
  topicality  - our embedding neighbourhood is peptide science. Phoenix
                retrieval is two-tower similarity, so staying tightly on-topic
                is what gets us surfaced out-of-network at all.
  question    - a post asking something has an obvious slot for an answer, and
                answering it is the highest-probability profile_click event.

Penalties are as important as the score. Anything that raises P(mute_author)
or P(block_author) is subtracted, because those carry negative weight and
attach to US, not to the post.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .safety import Verdict, gate_target, looks_like_question
from .xclient import Post

TOPIC_TERMS = (
    "peptide", "glp-1", "glp1", "gip", "semaglutide", "tirzepatide", "retatrutide",
    "liraglutide", "cagrilintide", "survodutide", "orforglipron", "vk2735",
    "bpc-157", "bpc157", "tb-500", "tb500", "ipamorelin", "cjc-1295", "sermorelin",
    "tesamorelin", "ghrp", "ghrh", "mots-c", "epitalon", "thymosin", "melanotan",
    "pt-141", "aod-9604", "hexarelin", "kisspeptin", "selank", "semax", "dsip",
    "glutathione", "ghk-cu", "amlexanox", "incretin", "amylin", "myostatin",
    "receptor agonist", "phase 1", "phase 2", "phase 3", "phase i", "phase ii",
    "phase iii", "randomi", "placebo", "double-blind", "pharmacokinetic",
    "bioavailab", "half-life", "subcutaneous", "clinical trial", "meta-analysis",
    "nejm", "lancet", "jama", "preprint", "endpoint", "efficacy",
)

# On-topic-adjacent but low value: generic fitness/biohacking chatter.
DILUTE_TERMS = ("gym", "shredded", "bulking", "cutting", "gains", "physique", "bodybuilding")


# Scores from different kinds land on different scales, but _harvest merges
# them into ONE sorted queue and takes the top N. Without an explicit bump, a
# hot outsider post outranks an unanswered reply on our own thread -- which is
# backwards: the mention was 5x cheaper to find and converts far better.
KIND_BONUS = {"mention": 2.5, "quote": 0.3, "outsider": 0.0}


@dataclass
class Candidate:
    post: Post
    kind: str  # "mention" | "outsider" | "quote"
    score: float
    reasons: list[str]
    root_text: str = ""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Candidate {self.kind} {self.post.id} score={self.score:.2f}>"


def _age_hours(created_at: str) -> float:
    if not created_at:
        return 999.0
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 999.0
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - dt).total_seconds() / 3600.0, 0.0)


def freshness(created_at: str, half_life_h: float = 4.0, floor: float = 0.0) -> float:
    """
    Exponential decay. At 4h half-life a 12h-old post scores 0.125.

    `floor` compresses the result into [floor, 1.0]. Mentions use a floor
    because their value doesn't decay to nothing: the author is reached by
    notification whichever day you answer. Cold outbound uses floor=0, where
    the feed really does bury old posts.
    """
    decay = 0.5 ** (_age_hours(created_at) / half_life_h)
    return floor + (1.0 - floor) * decay


def topicality(text: str) -> float:
    low = text.lower()
    hits = sum(1 for t in TOPIC_TERMS if t in low)
    dilute = sum(1 for t in DILUTE_TERMS if t in low)
    return max(0.0, min(1.0, hits / 3.0) - 0.15 * dilute)


def reach_weight(followers: int, sweet_low: int = 2_000, sweet_high: int = 200_000) -> float:
    """
    Log-scaled, with the tails knocked down. Below sweet_low nobody sees the
    reply; above sweet_high the reply section is a firehose.
    """
    if followers <= 0:
        return 0.0
    f = math.log10(followers + 10) / math.log10(sweet_high + 10)
    if followers < sweet_low:
        f *= 0.4
    if followers > sweet_high:
        f *= 0.5
    return max(0.0, min(1.0, f))


def conversation_heat(metrics: dict[str, int]) -> float:
    replies = metrics.get("reply_count", 0)
    likes = metrics.get("like_count", 0)
    # A post with replies is in a thread the ranker is already circulating.
    # Cap it: 200 replies means our reply is invisible.
    if replies > 150:
        return 0.15
    return min(1.0, (replies * 2 + likes) / 40.0)


def score_candidate(
    post: Post,
    kind: str,
    *,
    max_age_hours: float = 24.0,
    freshness_half_life: float = 4.0,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    age = _age_hours(post.created_at)
    if age > max_age_hours:
        return -1.0, [f"stale ({age:.0f}h old, cutoff {max_age_hours:.0f}h)"]

    # A mention keeps at least half its urgency no matter how old, because a
    # notification doesn't expire. An outbound post genuinely does go cold.
    f = freshness(
        post.created_at, freshness_half_life, floor=0.5 if kind == "mention" else 0.0
    )
    t = topicality(post.text)
    r = reach_weight(post.author_followers)
    h = conversation_heat(post.public_metrics)
    q = 1.0 if looks_like_question(post.text) else 0.0

    if kind == "mention":
        # Someone already engaged with us. Replying keeps the thread alive,
        # which is the cheapest reach we can buy, so weight freshness and the
        # answerable-question slot, and ignore follower count almost entirely.
        score = 3.0 * f + 1.6 * q + 0.8 * t + 0.4 * r + 0.3 * h
        reasons.append("mention (owned-read, cheapest reach)")
    elif kind == "quote":
        score = 2.2 * f + 2.0 * t + 0.8 * r + 0.4 * h
        reasons.append("quote candidate")
    else:
        score = 2.0 * f + 2.0 * t + 1.2 * r + 0.9 * h + 0.6 * q
        reasons.append("cold outbound")

    # Penalties -- these map onto negatively-weighted ranker predictions.
    low = post.text.lower()
    if post.is_retweet:
        return -1.0, ["retweet, not original content"]
    if len(post.text) < 60:
        score -= 0.5
        reasons.append("thin post")
    if post.text.count("#") >= 3:
        score -= 0.6
        reasons.append("hashtag spam")
    if re.search(r"\b(giveaway|follow (?:me|back)|rt to win|drop your)\b", low):
        return -1.0, ["engagement bait"]
    if post.public_metrics.get("quote_count", 0) > post.public_metrics.get("like_count", 0) > 20:
        # Quote-dunked. Joining a pile-on is how an account gets muted.
        return -1.0, ["ratioed/pile-on"]

    score += KIND_BONUS.get(kind, 0.0)
    reasons.append(
        f"fresh={f:.2f} topic={t:.2f} reach={r:.2f} heat={h:.2f} q={q:.0f} "
        f"age={age:.1f}h bonus={KIND_BONUS.get(kind, 0.0)}"
    )
    return score, reasons


def build_candidates(
    posts: list[Post],
    kind: str,
    *,
    me_id: str,
    ledger,
    min_followers: int = 0,
    min_score: float = 1.0,
    author_cooldown_hours: float = 72.0,
    max_age_hours: float = 24.0,
    freshness_half_life: float = 4.0,
    max_replies_per_conversation: int = 2,
    conversation_gap_hours: float = 3.0,
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """Returns (accepted, rejected) where rejected is [(post_id, reason)]."""
    accepted: list[Candidate] = []
    rejected: list[tuple[str, str]] = []

    for p in posts:
        if p.author_id == me_id:
            rejected.append((p.id, "our own post"))
            continue
        if ledger.has_acted(p.id):
            rejected.append((p.id, "already acted on"))
            continue
        # The ranker collapses branches of one conversation, so a second reply
        # buys goodwill rather than reach -- worth it, but only spaced out.
        open_, why = ledger.conversation_open(
            p.conversation_id, max_replies_per_conversation, conversation_gap_hours
        )
        if not open_:
            rejected.append((p.id, f"thread: {why}"))
            continue
        if ledger.author_cooling_down(p.author_id, author_cooldown_hours):
            rejected.append((p.id, f"author cooldown ({author_cooldown_hours:.0f}h)"))
            continue

        age_days = _account_age_days(p.author_created_at)
        v: Verdict = gate_target(
            p.text,
            author_followers=p.author_followers,
            author_is_protected=p.author_protected,
            author_created_days=age_days,
            min_followers=min_followers,
        )
        if not v:
            rejected.append((p.id, f"safety: {v.reason}"))
            continue

        score, reasons = score_candidate(
            p, kind, max_age_hours=max_age_hours, freshness_half_life=freshness_half_life
        )
        if score < min_score:
            rejected.append((p.id, f"score {score:.2f} < {min_score} ({reasons[-1]})"))
            continue
        accepted.append(Candidate(post=p, kind=kind, score=score, reasons=reasons))

    accepted.sort(key=lambda c: c.score, reverse=True)
    return accepted, rejected


def _account_age_days(created_at: str) -> int:
    if not created_at:
        return 9999
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 9999
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - dt).days, 0)


def diversify(candidates: list[Candidate], limit: int) -> list[Candidate]:
    """
    One action per author AND one per conversation, per run.

    The per-conversation rule is still enforced here even though the ledger now
    allows a second reply in a thread: the second one belongs in a LATER run.
    Answering two comments on the same thread inside one 20-minute window is
    the machine-shaped pattern people actually notice.
    """
    out: list[Candidate] = []
    seen_authors: set[str] = set()
    seen_convos: set[str] = set()
    for c in candidates:
        if len(out) >= limit:
            break
        if c.post.author_id in seen_authors or c.post.conversation_id in seen_convos:
            continue
        seen_authors.add(c.post.author_id)
        seen_convos.add(c.post.conversation_id)
        out.append(c)
    return out


def apply_outbound_reserve(
    chosen: list[Candidate],
    pool: list[Candidate],
    ledger,
    reserve: int,
) -> tuple[list[Candidate], str]:
    """
    Hold a slot open for cold outbound.

    Once mentions stopped decaying like feed content they win essentially every
    slot, which quietly kills the growth lever: replying to your own commenters
    is retention, replying to strangers is reach. If today's outbound quota is
    unmet and no outsider made the cut, swap the weakest mention for the best
    outsider.
    """
    if reserve <= 0 or not chosen:
        return chosen, ""
    if ledger.count_kind_today("outsider") >= reserve:
        return chosen, ""
    if any(c.kind == "outsider" for c in chosen):
        return chosen, ""

    picked = {c.post.id for c in chosen}
    authors = {c.post.author_id for c in chosen}
    convos = {c.post.conversation_id for c in chosen}
    candidate = next(
        (c for c in pool
         if c.kind == "outsider" and c.post.id not in picked
         and c.post.author_id not in authors and c.post.conversation_id not in convos),
        None,
    )
    if candidate is None:
        return chosen, "outbound slot reserved but no eligible stranger post today"

    dropped = chosen[-1]
    chosen = chosen[:-1] + [candidate]
    return chosen, (
        f"reserved outbound slot: swapped {dropped.kind} {dropped.post.id} "
        f"(score {dropped.score:.2f}) for outsider {candidate.post.id} "
        f"(score {candidate.score:.2f})"
    )
