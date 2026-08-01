import pytest

from pf_autorespond.selector import (
    build_candidates,
    diversify,
    freshness,
    reach_weight,
    score_candidate,
    topicality,
)
from tests.conftest import iso, make_post


def test_freshness_decays():
    assert freshness(iso(0)) > 0.99
    assert 0.45 < freshness(iso(4)) < 0.55      # one half-life
    assert freshness(iso(24)) < 0.02


def test_topicality_rewards_domain_terms():
    assert topicality("semaglutide phase 3 endpoint") > 0.9
    assert topicality("nice weather today") == 0.0
    # gym chatter dilutes an otherwise on-topic post
    assert topicality("peptide gains bulking gym shredded") < topicality("peptide trial data n=")


def test_reach_weight_penalises_both_tails():
    assert reach_weight(50) < reach_weight(20_000)
    assert reach_weight(5_000_000) < reach_weight(20_000)
    assert reach_weight(0) == 0.0


def test_stale_posts_are_rejected_outright():
    score, reasons = score_candidate(make_post(created_at=iso(48)), "outsider")
    assert score < 0 and "stale" in reasons[0]


def test_retweets_and_bait_rejected():
    rt = make_post(referenced=[{"type": "retweeted", "id": "9"}])
    assert score_candidate(rt, "outsider")[0] < 0
    bait = make_post(text="Giveaway! RT to win, follow me and drop your handle below")
    assert score_candidate(bait, "outsider")[0] < 0


def test_pile_on_rejected():
    ratioed = make_post(public_metrics={"reply_count": 30, "like_count": 25, "quote_count": 60})
    assert score_candidate(ratioed, "outsider")[0] < 0


def test_mentions_score_higher_than_cold_outbound():
    p = make_post()
    assert score_candidate(p, "mention")[0] > score_candidate(p, "outsider")[0]


def test_thread_cap_is_enforced(ledger):
    posts = [make_post(id="1", conversation_id="c1"),
             make_post(id="2", conversation_id="c1", author_id="a2")]
    ledger.mark_conversation("c1")
    acc, rej = build_candidates(posts, "outsider", me_id="me", ledger=ledger,
                                min_score=0, max_replies_per_conversation=1)
    assert acc == []
    assert all("thread:" in r[1] and "cap 1" in r[1] for r in rej)


def test_selector_default_matches_the_shipped_config(ledger):
    """
    The selector default silently applies when a caller forgets the kwarg.
    It was 1 while the config said 2, so a missed kwarg in engine._harvest
    capped threads at one reply with no error. Keep them in lockstep.
    """
    import inspect

    from pf_autorespond.config import Selection

    sig = inspect.signature(build_candidates)
    assert (sig.parameters["max_replies_per_conversation"].default
            == Selection().max_replies_per_conversation)
    assert (sig.parameters["conversation_gap_hours"].default
            == Selection().conversation_gap_hours)


def test_every_harvest_path_passes_the_thread_settings():
    """All three harvest branches must pass them, or one path silently differs."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src/pf_autorespond/engine.py"
    body = src.read_text()
    assert body.count("max_replies_per_conversation=sel.max_replies_per_conversation") == 3
    assert body.count("conversation_gap_hours=sel.conversation_gap_hours") == 3
    assert len(re.findall(r"build_candidates\(", body)) == 3


def test_second_thread_reply_allowed_but_not_back_to_back(ledger):
    """
    Answering both commenters is worth goodwill; answering them 30 seconds
    apart is the pattern people notice. Cap of 2, minimum 3h gap.
    """
    posts = [make_post(id="2", conversation_id="c1", author_id="a2")]
    ledger.mark_conversation("c1")          # just replied

    acc, rej = build_candidates(posts, "outsider", me_id="me", ledger=ledger,
                                min_score=0, max_replies_per_conversation=2,
                                conversation_gap_hours=3.0)
    assert acc == [] and "m to go" in rej[0][1]      # too soon

    # pretend the first reply was 4h ago
    ledger.conversation_log["c1"] = [iso_hours_ago(4)]
    acc, rej = build_candidates(posts, "outsider", me_id="me", ledger=ledger,
                                min_score=0, max_replies_per_conversation=2,
                                conversation_gap_hours=3.0)
    assert len(acc) == 1

    # but never a third
    ledger.conversation_log["c1"] = [iso_hours_ago(9), iso_hours_ago(4)]
    acc, rej = build_candidates(posts, "outsider", me_id="me", ledger=ledger,
                                min_score=0, max_replies_per_conversation=2,
                                conversation_gap_hours=3.0)
    assert acc == [] and "cap 2" in rej[0][1]


def iso_hours_ago(h):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


def test_outbound_slot_is_reserved_when_mentions_sweep(ledger):
    """
    Regression on the fix that made mentions stop decaying: they then won every
    slot, which silently switched the account from growth to retention only.
    """
    from pf_autorespond.selector import apply_outbound_reserve

    mentions = [make_post(id=f"m{i}", author_id=f"u{i}", conversation_id=f"cm{i}",
                          created_at=iso(2)) for i in range(3)]
    outsiders = [make_post(id="o1", author_id="uo", conversation_id="co", created_at=iso(2))]
    macc, _ = build_candidates(mentions, "mention", me_id="me", ledger=ledger,
                               min_score=0, max_age_hours=96, freshness_half_life=36.0)
    oacc, _ = build_candidates(outsiders, "outsider", me_id="me", ledger=ledger, min_score=0)
    pool = sorted(macc + oacc, key=lambda c: c.score, reverse=True)

    chosen = diversify(pool, 2)
    assert all(c.kind == "mention" for c in chosen)      # mentions sweep, as designed

    chosen, note = apply_outbound_reserve(chosen, pool, ledger, reserve=1)
    assert any(c.kind == "outsider" for c in chosen)
    assert "swapped" in note


def test_reserve_is_a_noop_once_todays_outbound_happened(ledger):
    from pf_autorespond.selector import apply_outbound_reserve

    ledger.record_kind("outsider")
    mentions = [make_post(id="m1", conversation_id="cm1", created_at=iso(2))]
    macc, _ = build_candidates(mentions, "mention", me_id="me", ledger=ledger,
                               min_score=0, max_age_hours=96, freshness_half_life=36.0)
    chosen, note = apply_outbound_reserve(macc, macc, ledger, reserve=1)
    assert all(c.kind == "mention" for c in chosen) and note == ""


def test_author_cooldown_respected(ledger):
    ledger.touch_author("a1")
    acc, rej = build_candidates([make_post()], "outsider", me_id="me", ledger=ledger, min_score=0)
    assert acc == []
    assert "cooldown" in rej[0][1]


def test_already_acted_skipped(ledger):
    ledger.mark_acted("1")
    acc, rej = build_candidates([make_post(id="1")], "outsider", me_id="me", ledger=ledger, min_score=0)
    assert acc == [] and "already acted" in rej[0][1]


def test_own_posts_skipped(ledger):
    acc, rej = build_candidates([make_post(author_id="me")], "outsider", me_id="me", ledger=ledger, min_score=0)
    assert acc == [] and "own post" in rej[0][1]


def test_diversify_one_per_author_and_conversation(ledger):
    posts = [
        make_post(id="1", author_id="a1", conversation_id="c1"),
        make_post(id="2", author_id="a1", conversation_id="c2"),
        make_post(id="3", author_id="a2", conversation_id="c3"),
    ]
    acc, _ = build_candidates(posts, "outsider", me_id="me", ledger=ledger, min_score=0)
    picked = diversify(acc, limit=3)
    assert len({c.post.author_id for c in picked}) == len(picked) == 2


def test_ordering_is_by_score(ledger):
    posts = [
        make_post(id="1", author_id="a1", conversation_id="c1", created_at=iso(20)),
        make_post(id="2", author_id="a2", conversation_id="c2", created_at=iso(0.5)),
    ]
    acc, _ = build_candidates(posts, "outsider", me_id="me", ledger=ledger, min_score=0)
    assert acc[0].post.id == "2"


def test_mentions_do_not_decay_like_feed_content(ledger):
    """
    Regression: a 51h-old reply on our own post scored 0.05 on freshness and
    lost to a 2h-old stranger's post. But a reply reaches its author by
    notification, which doesn't expire. Found in rehearsal, 2026-08-01.
    """
    old_mention = make_post(id="m", conversation_id="cm", created_at=iso(51))
    fresh_outsider = make_post(id="o", author_id="a2", conversation_id="co", created_at=iso(2))

    mention_score, _ = score_candidate(old_mention, "mention", max_age_hours=96,
                                       freshness_half_life=36.0)
    outsider_score, _ = score_candidate(fresh_outsider, "outsider", max_age_hours=24,
                                        freshness_half_life=4.0)
    assert mention_score > outsider_score


def test_mention_still_expires_eventually(ledger):
    stale = make_post(id="m", created_at=iso(120))
    score, reasons = score_candidate(stale, "mention", max_age_hours=96,
                                     freshness_half_life=36.0)
    assert score < 0 and "stale" in reasons[0]


def test_freshness_floor_only_applies_to_mentions():
    assert freshness(iso(100), 4.0) < 0.001            # outbound goes cold
    assert freshness(iso(100), 36.0, floor=0.5) > 0.5  # mentions keep a floor
    assert freshness(iso(0), 36.0, floor=0.5) == pytest.approx(1.0, abs=1e-4)
