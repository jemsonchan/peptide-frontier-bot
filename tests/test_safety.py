import pytest

from pf_autorespond.safety import gate_reply, gate_target

GOOD = (
    "Retatrutide's 24.2% figure comes from a Phase 2 trial at 48 weeks, n=338. "
    "Tirzepatide's SURMOUNT-1 ran 72 weeks, so the timelines aren't comparable.",
    "No human PK data exists for BPC-157, so rodent doses don't convert to a "
    "human number at all. That gap is the whole problem with the dosing debate.",
    "The plateau is real: above 2.4mg semaglutide, adverse events rise faster "
    "than efficacy. Trials pick the inflection point, not the ceiling.",
)


@pytest.mark.parametrize("text", GOOD)
def test_good_replies_pass(text):
    v = gate_reply(text)
    assert v, v.reason


@pytest.mark.parametrize(
    "text,tag",
    [
        ("Full data here: https://pubmed.ncbi.nlm.nih.gov/12345", "url"),
        ("See pubmed.ncbi.nlm.nih.gov for the trial", "url"),
        ("Read more at example.com/study", "url"),
        ("Great write-up www.nature.com", "url"),
    ],
)
def test_urls_are_hard_rejected(text, tag):
    """$0.200 vs $0.015, and the ranker buries links. Never publishable."""
    v = gate_reply(text)
    assert not v
    assert tag in v.tags


@pytest.mark.parametrize(
    "text",
    [
        "Most people run 250mcg twice daily for eight weeks.",
        "Standard is 5mg/week titrated up.",
        "Try 2iu ED and see how you respond.",
    ],
)
def test_dosing_protocols_rejected(text):
    v = gate_reply(text)
    assert not v
    assert "dosing" in v.tags or "advice" in v.tags


@pytest.mark.parametrize(
    "text",
    [
        "I recommend semaglutide for that.",
        "You should take it with food.",
        "DM me and I'll explain.",
        "Buy from a reputable supplier.",
        "It's completely safe with no side effects.",
    ],
)
def test_advice_and_sales_phrasing_rejected(text):
    assert not gate_reply(text)


def test_voice_violations_rejected():
    assert not gate_reply("Great point! Totally agree with this take.")
    assert not gate_reply("This is an absolutely insane result 🔥")
    assert not gate_reply("Solid data #peptides #GLP1")
    assert not gate_reply("As an AI I can't verify that claim.")
    assert not gate_reply("Nice @someguy, the Phase 2 data supports that reading.")


def test_length_limit():
    assert not gate_reply("x" * 300)


def test_target_gate_blocks_distress():
    v = gate_target(
        "I think I overdosed on this stuff and my chest hurts, what do I do",
        author_followers=5000,
    )
    assert not v
    assert "distress" in v.tags


def test_target_gate_blocks_vendors():
    v = gate_target(
        "Fresh restock of research chemicals, not for human consumption. "
        "DM for price, crypto only, domestic shipping.",
        author_followers=5000,
    )
    assert not v
    assert "vendor" in v.tags


def test_target_gate_blocks_bait_and_hostility():
    v = gate_target(
        "You're a bot account and a big pharma shill, nobody believes this garbage",
        author_followers=5000,
    )
    assert not v
    assert "hostile" in v.tags


def test_target_gate_blocks_personal_advice_requests():
    v = gate_target(
        "Should I switch from semaglutide to tirzepatide given my situation here?",
        author_followers=5000,
    )
    assert not v
    assert "advice_request" in v.tags


def test_target_gate_blocks_vulnerable_context():
    v = gate_target(
        "My daughter is 15 and wants to try one of these for weight loss, thoughts",
        author_followers=5000,
    )
    assert not v
    assert "regulated" in v.tags


def test_target_gate_blocks_new_accounts_and_protected():
    good = "Interesting Phase 2 readout on retatrutide, the effect size is large."
    assert not gate_target(good, author_followers=5000, author_created_days=3)
    assert not gate_target(good, author_followers=5000, author_is_protected=True)
    assert not gate_target(good, author_followers=10, min_followers=500)


def test_target_gate_accepts_a_real_target():
    v = gate_target(
        "The retatrutide Phase 2 numbers look strong but the trial ran 48 weeks "
        "versus SURMOUNT-1 at 72, which people keep ignoring.",
        author_followers=8000,
        author_created_days=900,
        min_followers=500,
    )
    assert v, v.reason
