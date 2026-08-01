"""
Approval flow. The failure modes worth money:
  * a draft billing before you approve it
  * an edited draft smuggling a URL past the gate at 13x
  * someone who isn't you approving spend
  * the same draft publishing twice because two channels both said yes
"""

from decimal import Decimal

import pytest

from pf_autorespond import llm
from pf_autorespond.channels import GitHubChannel, TelegramChannel
from pf_autorespond.engine import Engine
from pf_autorespond.publisher import (
    PublishReport,
    apply_decisions,
    collapse,
    expire,
    publish_approved,
)
from pf_autorespond.ledger import Ledger
from pf_autorespond.queue import Decision, Draft, Queue, make_id
from tests.conftest import iso, make_post
from tests.test_engine import FakeX, cfg_for, scripted  # noqa: F401

CLEAN = ("No human PK data exists for this compound, so rodent mg/kg figures "
         "don't convert to a human number at all.")


def review_cfg(tmp_path, **over):
    cfg = cfg_for(tmp_path, **over)
    cfg.mode = "review"
    cfg.queue_path = str(tmp_path / "queue.json")
    cfg.approval_ttl_hours = 12.0
    cfg.max_publish_per_run = 3
    return cfg


def make_draft(**kw) -> Draft:
    base = dict(
        id=make_id("reply", "t1", CLEAN), action="reply", target_id="t1",
        target_author="someone", target_text="a post", conversation_id="c1", text=CLEAN,
    )
    base.update(kw)
    return Draft(**base)


# ------------------------------------------------------------------ drafting
def test_review_mode_queues_without_spending(tmp_path, ledger, scripted):
    cfg = review_cfg(tmp_path)
    queue = Queue(cfg.queue_path)
    client = FakeX(ledger, mentions=[make_post(id="m1", created_at=iso(1))])
    report = Engine(cfg, client, ledger, queue=queue, channels=[]).run()

    assert len(report.queued) == 1
    assert report.published == []
    assert ledger.count_today("reply") == 0
    # only the $0.001 mention read; the $0.015 write has not happened
    assert ledger.month_spend() == Decimal("0.001")
    assert queue.pending()[0].text == report.queued[0]["text"]


def test_no_duplicate_draft_for_same_target(tmp_path, ledger, scripted):
    cfg = review_cfg(tmp_path, max_actions_per_run=1)
    queue = Queue(cfg.queue_path)
    m = make_post(id="m1", author_id="u1", conversation_id="c1", created_at=iso(1))

    Engine(cfg, FakeX(ledger, mentions=[m]), ledger, queue=queue, channels=[]).run()
    r2 = Engine(cfg, FakeX(ledger, mentions=[m]), ledger, queue=queue, channels=[]).run()

    assert len(queue.pending()) == 1
    assert r2.queued == []


def test_draft_id_is_deterministic():
    assert make_id("reply", "t1", CLEAN) == make_id("reply", "t1", CLEAN)
    assert make_id("reply", "t1", CLEAN) != make_id("quote", "t1", CLEAN)
    assert len(make_id("reply", "t1", CLEAN)) == 12   # fits Telegram's 64-byte cap


# ----------------------------------------------------------------- decisions
def test_reject_beats_approve_regardless_of_order():
    d = "x"
    for order in ([("approve", "telegram"), ("reject", "github")],
                  [("reject", "github"), ("approve", "telegram")]):
        got = collapse([Decision(d, v, via, "paul") for v, via in order])
        assert got[d].verdict == "reject"


def test_edit_beats_approve():
    got = collapse([
        Decision("x", "approve", "telegram", "paul"),
        Decision("x", "edit", "github", "paul", new_text=CLEAN),
    ])
    assert got["x"].verdict == "edit"


def test_edited_draft_is_regated_and_a_pasted_url_is_refused(tmp_path):
    q = Queue(tmp_path / "q.json")
    q.add(make_draft())
    d = q.pending()[0]
    report = PublishReport()

    apply_decisions(q, [Decision(d.id, "edit", "github", "paul",
                                 new_text="Good point, full data at https://pubmed.gov/123")],
                    report)

    assert q.get(d.id).status == "rejected"
    assert "safety gate" in q.get(d.id).reject_reason
    assert report.approved == 0


def test_edited_draft_that_is_clean_gets_approved(tmp_path):
    q = Queue(tmp_path / "q.json")
    q.add(make_draft())
    d = q.pending()[0]
    new = "Sharper version: without human PK there is no exposure curve to scale from."
    report = PublishReport()

    apply_decisions(q, [Decision(d.id, "edit", "github", "paul", new_text=new)], report)

    got = q.get(d.id)
    assert got.status == "approved"
    assert got.text == new
    assert got.original_text == CLEAN     # audit trail preserved
    assert report.edited == 1


# ---------------------------------------------------------------- publishing
def test_publish_only_happens_after_approval(tmp_path, ledger):
    q = Queue(tmp_path / "q.json")
    q.add(make_draft())
    client = FakeX(ledger)
    report = PublishReport()

    publish_approved(q, client, ledger, report)          # still pending
    assert report.published == [] and ledger.month_spend() == Decimal("0")

    apply_decisions(q, [Decision(q.pending()[0].id, "approve", "telegram", "paul")], report)
    publish_approved(q, client, ledger, report)

    assert len(report.published) == 1
    assert ledger.month_spend() == Decimal("0.015")
    assert q.drafts[0].status == "published"


def test_published_draft_cannot_publish_again(tmp_path, ledger):
    q = Queue(tmp_path / "q.json")
    q.add(make_draft(status="approved"))
    client = FakeX(ledger)
    report = PublishReport()

    publish_approved(q, client, ledger, report)
    publish_approved(q, client, ledger, report)   # a second channel says yes too

    assert len(report.published) == 1
    assert ledger.month_spend() == Decimal("0.015")


def test_final_gate_blocks_a_tampered_draft(tmp_path, ledger):
    q = Queue(tmp_path / "q.json")
    q.add(make_draft(status="approved", text="Approved but see https://example.com/x"))
    client = FakeX(ledger)
    report = PublishReport()

    publish_approved(q, client, ledger, report)

    assert report.published == []
    assert q.drafts[0].status == "rejected"
    assert ledger.month_spend() == Decimal("0")


def test_budget_stop_leaves_draft_approved_for_next_run(tmp_path, ledger):
    for _ in range(3):
        ledger.record("reply")          # quota exhausted
    q = Queue(tmp_path / "q.json")
    q.add(make_draft(status="approved"))
    report = PublishReport()

    publish_approved(q, FakeX(ledger), ledger, report)

    assert report.published == []
    assert q.drafts[0].status == "approved"   # not lost, just deferred
    assert any("quota" in e for e in report.errors)


def test_expiry_drops_stale_drafts(tmp_path):
    q = Queue(tmp_path / "q.json")
    q.add(make_draft(created=iso(20)))
    q.add(make_draft(id="fresh", created=iso(1)))
    report = PublishReport()

    expire(q, 12.0, report)

    assert len(report.expired) == 1
    assert q.get("fresh").status == "pending"


# ------------------------------------------------------------------- channels
class FakeGH(GitHubChannel):
    def __init__(self, payloads, **kw):
        super().__init__(repo="paul/pf", token="t", approver_login="paul", **kw)
        self.payloads = payloads
        self.sent = []

    def _req(self, method, path, **kw):
        self.sent.append((method, path, kw))
        for key, val in self.payloads.items():
            if path.endswith(key):
                return val
        return {"number": 7}


def test_github_ignores_strangers(tmp_path):
    ch = FakeGH({
        "/comments": [{"user": {"login": "randomguy"}, "body": "/approve"}],
        "/reactions": [{"user": {"login": "randomguy"}, "content": "+1"}],
    })
    assert ch.poll([make_draft(github_issue=7)]) == []


def test_github_reads_owner_approval_and_edit(tmp_path):
    ch = FakeGH({
        "/comments": [{"user": {"login": "Paul"}, "body": f"/edit {CLEAN}"}],
        "/reactions": [{"user": {"login": "paul"}, "content": "+1"}],
    })
    got = ch.poll([make_draft(github_issue=7)])
    verdicts = {d.verdict for d in got}
    assert verdicts == {"edit", "approve"}          # login match is case-insensitive
    assert collapse(got)[make_draft().id].verdict == "edit"


class FakeTG(TelegramChannel):
    def __init__(self, updates):
        super().__init__(token="t", chat_id="42")
        self.updates = updates
        self.acks = []

    def _req(self, method, **payload):
        if method == "getUpdates":
            return self.updates
        if method == "answerCallbackQuery":
            self.acks.append(payload.get("text"))
            return True
        return {"message_id": 1}


def test_telegram_ignores_other_senders():
    d = make_draft()
    ch = FakeTG([{"update_id": 1,
                  "callback_query": {"id": "c", "from": {"id": 999},
                                     "data": f"a:{d.id}"}}])
    got, offset = ch.poll([d], 0)
    assert got == [] and offset == 2
    assert ch.acks == ["not authorised"]


def test_telegram_reads_owner_taps_and_advances_offset():
    d = make_draft()
    ch = FakeTG([{"update_id": 5,
                  "callback_query": {"id": "c", "from": {"id": 42},
                                     "data": f"a:{d.id}"}}])
    got, offset = ch.poll([d], 0)
    assert [x.verdict for x in got] == ["approve"]
    # offset must advance or the next cron run replays the same tap and
    # republishes something you already approved
    assert offset == 6


def test_telegram_ignores_callbacks_for_unknown_drafts():
    ch = FakeTG([{"update_id": 1,
                  "callback_query": {"id": "c", "from": {"id": 42}, "data": "a:gone"}}])
    got, _ = ch.poll([make_draft()], 0)
    assert got == []
    assert ch.acks == ["draft no longer pending"]


# ---------------------------------------------------- persistence & announce
def test_queue_round_trips_through_disk(tmp_path):
    path = tmp_path / "q.json"
    a = Queue(path)
    a.add(make_draft(github_issue=7, telegram_message=99))
    a.drafts[0].status = "approved"
    a.save()

    b = Queue(path)
    assert len(b.drafts) == 1
    d = b.drafts[0]
    assert d.status == "approved" and d.github_issue == 7 and d.telegram_message == 99
    assert d.target_url() == "https://x.com/i/status/t1"


def test_prune_keeps_pending_but_drops_old_decided(tmp_path):
    q = Queue(tmp_path / "q.json")
    q.add(make_draft(id="old", status="published", created=iso(24 * 60)))
    q.add(make_draft(id="stillopen", status="pending", created=iso(24 * 60)))
    q.prune(keep_days=30)
    ids = {d.id for d in q.drafts}
    assert ids == {"stillopen"}     # never silently drop something awaiting you


def test_github_announce_records_issue_number():
    ch = FakeGH({})
    d = make_draft()
    ch.announce(d, 12.0)
    assert d.github_issue == 7
    method, path, kw = ch.sent[0]
    assert method == "POST" and path.endswith("/issues")
    body = kw["json"]["body"]
    assert "$0.015" in body and "expires in **12h**" in body
    assert d.text in body and "pf-draft" in kw["json"]["labels"]


def test_telegram_announce_offers_approve_and_reject():
    ch = FakeTG([])
    sent = {}
    ch._req = lambda method, **p: (sent.update({method: p}) or {"message_id": 55})
    d = make_draft()
    ch.announce(d, 12.0)
    assert d.telegram_message == 55
    buttons = sent["sendMessage"]["reply_markup"]["inline_keyboard"][0]
    labels = [b["text"] for b in buttons]
    assert "✅ Approve" in labels and "❌ Reject" in labels
    assert any(b.get("callback_data") == f"a:{d.id}" for b in buttons)


def test_telegram_finalize_strips_buttons():
    ch = FakeTG([])
    calls = []
    ch._req = lambda method, **p: (calls.append(method) or {})
    ch.finalize(make_draft(telegram_message=55), "✅ published", "https://x.com/i/status/9")
    # buttons must be cleared or a stale message can be tapped a second time
    assert "editMessageReplyMarkup" in calls


def test_a_dead_channel_does_not_block_publishing(tmp_path, ledger):
    class Broken:
        name = "broken"

        def finalize(self, *a, **kw):
            raise RuntimeError("network down")

    q = Queue(tmp_path / "q.json")
    q.add(make_draft(status="approved"))
    report = PublishReport()
    publish_approved(q, FakeX(ledger), ledger, report, channels=[Broken()])

    assert len(report.published) == 1
    assert q.drafts[0].status == "published"


def test_engine_warns_when_no_channel_is_configured(tmp_path, ledger, scripted):
    cfg = review_cfg(tmp_path)
    queue = Queue(cfg.queue_path)
    client = FakeX(ledger, mentions=[make_post(id="m1", created_at=iso(1))])
    report = Engine(cfg, client, ledger, queue=queue, channels=[]).run()
    # the draft is still safe on disk, but you must be told you won't be pinged
    assert any("no approval channel" in e for e in report.errors)
    assert len(queue.pending()) == 1


# ---------------------------------------------------------- nostr mirroring
def _nostr_cfg(tmp_path, **over):
    from pf_autorespond.config import Nostr

    n = Nostr(enabled=True, relays=["wss://relay.test"],
              map_path=str(tmp_path / "map.json"))
    for k, v in over.items():
        setattr(n, k, v)

    class C:
        nostr = n
    return C()


def _fake_pool(monkeypatch, ok=True, published=None):
    from pf_autorespond import nostr as nostr_mod

    class Pool:
        def __init__(self, relays, key, timeout=12):
            self.relays = relays

        def publish(self, ev):
            if published is not None:
                published.append(ev)
            return [nostr_mod.PublishResult("wss://relay.test", ok, "" if ok else "rejected")]

    monkeypatch.setattr(nostr_mod, "RelayPool", Pool)


SK = "0000000000000000000000000000000000000000000000000000000000000003"


def test_nostr_mirrors_published_drafts(tmp_path, monkeypatch):
    from pf_autorespond.publisher import mirror_to_nostr

    monkeypatch.setenv("NOSTR_NSEC", SK)
    sent = []
    _fake_pool(monkeypatch, published=sent)

    q = Queue(tmp_path / "q.json")
    q.add(make_draft(status="published", published_id="x999", kind="mention"))
    report = PublishReport()
    mirror_to_nostr(q, _nostr_cfg(tmp_path), report)

    assert len(sent) == 1 and sent[0].content == CLEAN
    assert q.drafts[0].nostr_event == sent[0].id
    assert report.nostr[0]["relays_ok"] == 1


def test_nostr_publishes_even_when_x_budget_blocked(tmp_path, monkeypatch):
    """Nostr is free. A good reply shouldn't die because the wallet is empty."""
    from pf_autorespond.publisher import mirror_to_nostr

    monkeypatch.setenv("NOSTR_NSEC", SK)
    sent = []
    _fake_pool(monkeypatch, published=sent)

    q = Queue(tmp_path / "q.json")
    q.add(make_draft(status="approved", kind="mention"))   # never reached X
    report = PublishReport()
    mirror_to_nostr(q, _nostr_cfg(tmp_path), report)

    assert len(sent) == 1
    assert q.drafts[0].nostr_event


def test_outsider_replies_are_skipped_by_default(tmp_path, monkeypatch):
    from pf_autorespond.publisher import mirror_to_nostr

    monkeypatch.setenv("NOSTR_NSEC", SK)
    sent = []
    _fake_pool(monkeypatch, published=sent)

    q = Queue(tmp_path / "q.json")
    q.add(make_draft(status="published", kind="outsider"))
    report = PublishReport()
    mirror_to_nostr(q, _nostr_cfg(tmp_path), report)

    assert sent == []
    assert "no parent on nostr" in report.nostr[0]["skipped"]


def test_nostr_threads_onto_a_known_parent(tmp_path, monkeypatch):
    from pf_autorespond import nostr as nostr_mod
    from pf_autorespond.publisher import mirror_to_nostr

    monkeypatch.setenv("NOSTR_NSEC", SK)
    sent = []
    _fake_pool(monkeypatch, published=sent)

    m = nostr_mod.EventMap(tmp_path / "map.json")
    m.put("t1", "e" * 64)
    m.save()

    q = Queue(tmp_path / "q.json")
    q.add(make_draft(status="published", kind="mention"))
    report = PublishReport()
    mirror_to_nostr(q, _nostr_cfg(tmp_path), report)

    assert sent[0].tags == [["e", "e" * 64, "", "root"]]
    assert report.nostr[0]["threaded"] is True


def test_nostr_never_double_publishes(tmp_path, monkeypatch):
    from pf_autorespond.publisher import mirror_to_nostr

    monkeypatch.setenv("NOSTR_NSEC", SK)
    sent = []
    _fake_pool(monkeypatch, published=sent)

    q = Queue(tmp_path / "q.json")
    q.add(make_draft(status="published", kind="mention"))
    cfg = _nostr_cfg(tmp_path)
    mirror_to_nostr(q, cfg, PublishReport())
    mirror_to_nostr(q, cfg, PublishReport())

    assert len(sent) == 1


def test_nostr_is_a_noop_without_a_key(tmp_path, monkeypatch):
    from pf_autorespond.publisher import mirror_to_nostr

    monkeypatch.delenv("NOSTR_NSEC", raising=False)
    monkeypatch.delenv("NOSTR_BUNKER_URI", raising=False)
    q = Queue(tmp_path / "q.json")
    q.add(make_draft(status="published", kind="mention"))
    report = PublishReport()
    mirror_to_nostr(q, _nostr_cfg(tmp_path), report)
    assert report.nostr == [] and not q.drafts[0].nostr_event


def test_rejected_drafts_never_reach_nostr(tmp_path, monkeypatch):
    """Rejecting on X must reject everywhere. Nostr is free, not unsupervised."""
    from pf_autorespond.publisher import mirror_to_nostr

    monkeypatch.setenv("NOSTR_NSEC", SK)
    sent = []
    _fake_pool(monkeypatch, published=sent)

    q = Queue(tmp_path / "q.json")
    q.add(make_draft(id="r", status="rejected", kind="mention"))
    q.add(make_draft(id="e", status="expired", kind="mention"))
    q.add(make_draft(id="p", status="pending", kind="mention"))
    mirror_to_nostr(q, _nostr_cfg(tmp_path), PublishReport())

    assert sent == []


def test_relay_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    from pf_autorespond.publisher import mirror_to_nostr

    monkeypatch.setenv("NOSTR_NSEC", SK)
    _fake_pool(monkeypatch, ok=False)

    q = Queue(tmp_path / "q.json")
    q.add(make_draft(status="published", kind="mention"))
    report = PublishReport()
    mirror_to_nostr(q, _nostr_cfg(tmp_path), report)

    assert not q.drafts[0].nostr_event      # retryable next run
    assert any("no relay accepted" in e for e in report.errors)


def test_backfill_needs_only_a_public_key(tmp_path, monkeypatch):
    """The nsec must never be required to read your own public notes."""
    from pf_autorespond import nostr as nostr_mod
    from pf_autorespond.publisher import backfill_map

    monkeypatch.delenv("NOSTR_NSEC", raising=False)   # no secret available at all
    monkeypatch.delenv("NOSTR_BUNKER_URI", raising=False)
    x_text = ("BPC-157's therapeutic potential is often overstated. Many claims "
              "trace back to preclinical studies, primarily in rodent models.")
    monkeypatch.setattr(
        nostr_mod, "fetch_own_notes",
        lambda relays, pk, limit=50: [
            {"id": "e" * 64, "content": x_text + "\n\nhttps://x.com/i/status/9",
             "created_at": 1},
        ],
    )

    q = Queue(tmp_path / "q.json")
    q.add(make_draft(kind="mention", target_id="x9", target_text=x_text))
    npub = nostr_mod.NostrKey(SK).npub

    mapped, seen = backfill_map(_nostr_cfg(tmp_path), q, pubkey=npub)

    assert (mapped, seen) == (1, 1)
    assert nostr_mod.EventMap(tmp_path / "map.json").get("x9") == "e" * 64


def test_backfill_refuses_to_map_a_weak_match(tmp_path, monkeypatch):
    from pf_autorespond import nostr as nostr_mod
    from pf_autorespond.publisher import backfill_map

    monkeypatch.delenv("NOSTR_NSEC", raising=False)
    monkeypatch.delenv("NOSTR_BUNKER_URI", raising=False)
    monkeypatch.setattr(
        nostr_mod, "fetch_own_notes",
        lambda relays, pk, limit=50: [{"id": "f" * 64, "content": "unrelated", "created_at": 1}],
    )
    q = Queue(tmp_path / "q.json")
    q.add(make_draft(kind="mention", target_id="x9", target_text="BPC-157 rodent models"))
    mapped, seen = backfill_map(_nostr_cfg(tmp_path), q,
                                pubkey=nostr_mod.NostrKey(SK).npub)
    assert (mapped, seen) == (0, 1)


# ------------------------------------------------------------------ reconcile
# Modelled on the real gap found on 2026-08-01: 4 X posts, 3 on Nostr, the
# VK2735 post missing entirely.
X_POSTS = [
    ("t_bpc", "BPC-157's therapeutic potential is often overstated. Many claims trace "
              "back to preclinical studies, primarily in rodent models of injury.",
     "2026-07-29T11:43:00Z"),
    ("t_vk",  "Viking's oral VK2735 showed a 13.1% weight loss at 12 weeks in a Phase 1 "
              "trial (2026). This dual GLP-1/GIP agonist's oral form expands options.",
     "2026-07-29T15:53:00Z"),
    ("t_dose", "More peptide = faster results is a common myth. GLP-1 agonists show a "
               "dose-response plateau.", "2026-07-30T15:54:00Z"),
]
NOSTR_NOTES = [
    {"id": "a" * 64, "content": X_POSTS[0][1] + "\n\nhttps://x.com/i/status/1", "created_at": 1},
    {"id": "c" * 64, "content": X_POSTS[2][1], "created_at": 3},
]


def _x_posts():
    return [make_post(id=i, text=t, created_at=c, author_id="me") for i, t, c in X_POSTS]


def test_reconcile_finds_the_dropped_post(tmp_path, monkeypatch):
    from pf_autorespond import nostr as nostr_mod
    from pf_autorespond.publisher import reconcile_nostr

    monkeypatch.setattr(nostr_mod, "fetch_own_notes",
                        lambda relays, pk, limit=100: NOSTR_NOTES)
    client = FakeX(Ledger.load(tmp_path / "l.json"))
    client._own = _x_posts()
    report = PublishReport()

    gaps = reconcile_nostr(
        _nostr_cfg(tmp_path), client, client.ledger, report,
        pubkey=nostr_mod.NostrKey(SK).npub, live=False,
    )

    assert [g.x_id for g in gaps] == ["t_vk"]
    head = report.nostr[0]
    assert head["x_posts"] == 3 and head["nostr_notes"] == 2 and head["gaps"] == 1
    assert head["map_repaired"] == 2          # the two that DID bridge get relinked


def test_reconcile_dry_run_publishes_nothing(tmp_path, monkeypatch):
    from pf_autorespond import nostr as nostr_mod
    from pf_autorespond.publisher import reconcile_nostr

    monkeypatch.setenv("NOSTR_NSEC", SK)
    monkeypatch.setattr(nostr_mod, "fetch_own_notes",
                        lambda relays, pk, limit=100: NOSTR_NOTES)
    sent = []
    _fake_pool(monkeypatch, published=sent)
    client = FakeX(Ledger.load(tmp_path / "l.json"))
    client._own = _x_posts()

    reconcile_nostr(_nostr_cfg(tmp_path), client, client.ledger, PublishReport(),
                    pubkey=nostr_mod.NostrKey(SK).npub, live=False)
    assert sent == []


def test_reconcile_live_republishes_with_the_original_timestamp(tmp_path, monkeypatch):
    """
    Backdating matters: stamping now() would surface a three-day-old post at
    the top of followers' feeds and put the Nostr timeline out of order.
    """
    from pf_autorespond import nostr as nostr_mod
    from pf_autorespond.publisher import reconcile_nostr

    monkeypatch.setenv("NOSTR_NSEC", SK)
    monkeypatch.setattr(nostr_mod, "fetch_own_notes",
                        lambda relays, pk, limit=100: NOSTR_NOTES)
    sent = []
    _fake_pool(monkeypatch, published=sent)
    client = FakeX(Ledger.load(tmp_path / "l.json"))
    client._own = _x_posts()
    report = PublishReport()

    reconcile_nostr(_nostr_cfg(tmp_path), client, client.ledger, report,
                    pubkey=nostr_mod.NostrKey(SK).npub, live=True)

    assert len(sent) == 1
    assert "VK2735" in sent[0].content
    assert sent[0].created_at == 1785340380      # 2026-07-29T15:53:00Z
    assert nostr_mod.EventMap(tmp_path / "map.json").get("t_vk") == sent[0].id


def test_reconcile_is_idempotent(tmp_path, monkeypatch):
    from pf_autorespond import nostr as nostr_mod
    from pf_autorespond.publisher import reconcile_nostr

    monkeypatch.setenv("NOSTR_NSEC", SK)
    monkeypatch.setattr(nostr_mod, "fetch_own_notes",
                        lambda relays, pk, limit=100: NOSTR_NOTES)
    sent = []
    _fake_pool(monkeypatch, published=sent)
    client = FakeX(Ledger.load(tmp_path / "l.json"))
    client._own = _x_posts()
    cfg = _nostr_cfg(tmp_path)
    npub = nostr_mod.NostrKey(SK).npub

    reconcile_nostr(cfg, client, client.ledger, PublishReport(), pubkey=npub, live=True)
    gaps2 = reconcile_nostr(cfg, client, client.ledger, PublishReport(), pubkey=npub, live=True)

    assert len(sent) == 1 and gaps2 == []     # map remembers; never double-posts


def test_reconcile_reads_are_owned_and_dedup(tmp_path, monkeypatch):
    """Own-timeline reads are $0.001, and free on a second pass the same day."""
    from decimal import Decimal

    from pf_autorespond import nostr as nostr_mod
    from pf_autorespond.publisher import reconcile_nostr

    monkeypatch.setattr(nostr_mod, "fetch_own_notes", lambda relays, pk, limit=100: [])
    led = Ledger.load(tmp_path / "l.json")
    client = FakeX(led)
    client._own = _x_posts()
    cfg = _nostr_cfg(tmp_path)
    npub = nostr_mod.NostrKey(SK).npub

    reconcile_nostr(cfg, client, led, PublishReport(), pubkey=npub, live=False)
    assert led.month_spend() == Decimal("0.003")     # 3 own posts x $0.001

    reconcile_nostr(cfg, client, led, PublishReport(), pubkey=npub, live=False)
    assert led.month_spend() == Decimal("0.003")     # 24h UTC dedup -> free


def test_reconcile_needs_no_secret_to_report_gaps(tmp_path, monkeypatch):
    from pf_autorespond import nostr as nostr_mod
    from pf_autorespond.publisher import reconcile_nostr

    monkeypatch.delenv("NOSTR_NSEC", raising=False)
    monkeypatch.delenv("NOSTR_BUNKER_URI", raising=False)
    monkeypatch.setattr(nostr_mod, "fetch_own_notes",
                        lambda relays, pk, limit=100: NOSTR_NOTES)
    client = FakeX(Ledger.load(tmp_path / "l.json"))
    client._own = _x_posts()
    report = PublishReport()

    gaps = reconcile_nostr(_nostr_cfg(tmp_path), client, client.ledger, report,
                           pubkey=nostr_mod.NostrKey(SK).npub, live=False)
    assert len(gaps) == 1 and not report.errors


def test_reconcile_live_without_a_signer_fails_loudly(tmp_path, monkeypatch):
    from pf_autorespond import nostr as nostr_mod
    from pf_autorespond.publisher import reconcile_nostr

    monkeypatch.delenv("NOSTR_NSEC", raising=False)
    monkeypatch.delenv("NOSTR_BUNKER_URI", raising=False)
    monkeypatch.setattr(nostr_mod, "fetch_own_notes",
                        lambda relays, pk, limit=100: NOSTR_NOTES)
    client = FakeX(Ledger.load(tmp_path / "l.json"))
    client._own = _x_posts()
    report = PublishReport()

    reconcile_nostr(_nostr_cfg(tmp_path), client, client.ledger, report,
                    pubkey=nostr_mod.NostrKey(SK).npub, live=True)
    assert any("NOSTR_BUNKER_URI" in e for e in report.errors)


def test_queueing_does_not_consume_the_thread_reply_cap(tmp_path, ledger, scripted):
    """
    Regression: mark_conversation fired at enqueue AND at publish, so one
    reply used up a cap of two and the second commenter on a thread was never
    answered. Found in the day rehearsal, 2026-08-01.
    """
    cfg = review_cfg(tmp_path, max_actions_per_run=1)
    queue = Queue(cfg.queue_path)
    m1 = make_post(id="m1", author_id="u1", conversation_id="cbpc", created_at=iso(2))
    Engine(cfg, FakeX(ledger, mentions=[m1]), ledger, queue=queue, channels=[]).run()

    assert ledger.conversation_replies("cbpc") == 0     # queued, not replied

    d = queue.pending()[0]
    report = PublishReport()
    apply_decisions(queue, [Decision(d.id, "approve", "telegram", "281152522")], report)
    publish_approved(queue, FakeX(ledger), ledger, report)

    assert ledger.conversation_replies("cbpc") == 1     # exactly one, not two
    open_, _ = ledger.conversation_open("cbpc", max_replies=2, gap_hours=0)
    assert open_                                        # second commenter still reachable


def test_second_commenter_on_a_thread_gets_answered(tmp_path, ledger, scripted):
    cfg = review_cfg(tmp_path, max_actions_per_run=1)
    cfg.selection.max_replies_per_conversation = 2
    cfg.selection.conversation_gap_hours = 0.0
    queue = Queue(cfg.queue_path)

    m1 = make_post(id="m1", author_id="u1", conversation_id="cbpc", created_at=iso(2))
    m2 = make_post(id="m2", author_id="u2", conversation_id="cbpc", created_at=iso(3))

    Engine(cfg, FakeX(ledger, mentions=[m1, m2]), ledger, queue=queue, channels=[]).run()
    d = queue.pending()[0]
    rep = PublishReport()
    apply_decisions(queue, [Decision(d.id, "approve", "telegram", "x")], rep)
    publish_approved(queue, FakeX(ledger), ledger, rep)

    # next run: the other commenter is now reachable
    Engine(cfg, FakeX(ledger, mentions=[m1, m2]), ledger, queue=queue, channels=[]).run()
    targets = {d.target_id for d in queue.drafts}
    assert targets == {"m1", "m2"}
