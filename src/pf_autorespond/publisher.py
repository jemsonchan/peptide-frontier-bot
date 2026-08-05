"""
The second half of the hybrid loop: collect verdicts, then spend.

Order matters. Rejections and edits are applied before approvals, so if you
👍 on your phone and then think better of it and comment `/reject` on GitHub,
the reject wins. The cautious verdict always beats the permissive one when
both are present for the same draft.

Every edited draft is re-gated. That is not paranoia about your judgement --
it is that pasting a link into a reply is the single easiest way to turn a
$0.015 post into a $0.200 one, and a human editing on a phone will do it
eventually.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import nip46
from . import nostr as nostr_mod
from . import safety
from .ledger import BudgetExceeded, Ledger
from .queue import Decision, Draft, Queue
from .xclient import XAPIError, XClient

log = logging.getLogger(__name__)

# Cautious first: a reject anywhere beats an approve anywhere.
PRECEDENCE = {"reject": 0, "edit": 1, "approve": 2}


@dataclass
class PublishReport:
    polled: int = 0
    approved: int = 0
    rejected: int = 0
    edited: int = 0
    published: list[dict[str, Any]] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    queue_summary: str = ""
    ledger_summary: str = ""
    nostr: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def collapse(decisions: list[Decision]) -> dict[str, Decision]:
    """One verdict per draft. Reject > edit > approve."""
    best: dict[str, Decision] = {}
    for d in decisions:
        cur = best.get(d.draft_id)
        if cur is None or PRECEDENCE[d.verdict] < PRECEDENCE[cur.verdict]:
            best[d.draft_id] = d
    return best


def apply_decisions(queue: Queue, decisions: list[Decision], report: PublishReport) -> None:
    for draft_id, decision in collapse(decisions).items():
        draft = queue.get(draft_id)
        if draft is None or draft.status not in ("pending", "approved"):
            continue

        if decision.verdict == "reject":
            draft.status = "rejected"
            draft.reject_reason = decision.note or "rejected"
            report.rejected += 1

        elif decision.verdict == "edit":
            # A human edit is still a draft, not a licence to publish.
            cleaned = safety.strip_risky(decision.new_text)
            verdict = safety.gate_reply(cleaned)
            if not verdict:
                draft.status = "rejected"
                draft.reject_reason = f"edit failed safety gate: {verdict.reason}"
                report.errors.append(f"{draft.id}: edit rejected — {verdict.reason}")
                report.rejected += 1
            else:
                draft.original_text = draft.original_text or draft.text
                draft.text = cleaned
                draft.status = "approved"
                report.edited += 1
                report.approved += 1
        else:
            draft.status = "approved"
            report.approved += 1

        draft.decided_by = decision.by
        draft.decided_via = decision.via
        from .queue import _utcnow

        draft.decided_at = _utcnow().isoformat()


def publish_approved(
    queue: Queue,
    client: XClient,
    ledger: Ledger,
    report: PublishReport,
    *,
    max_per_run: int = 3,
    channels: list | None = None,
) -> None:
    channels = channels or []
    for draft in queue.approved()[:max_per_run]:
        # Last-chance re-gate. The draft was gated when written, but it may
        # have been edited since, and the cost of being wrong is 13x.
        verdict = safety.gate_reply(draft.text)
        if not verdict:
            draft.status = "rejected"
            draft.reject_reason = f"failed final gate: {verdict.reason}"
            report.errors.append(f"{draft.id}: {verdict.reason}")
            _finalize(channels, draft, "❌ blocked", verdict.reason)
            continue

        try:
            ledger.require(draft.action)
        except BudgetExceeded as e:
            # Leave it approved -- the next run can publish it if it hasn't
            # expired and the budget has rolled over.
            report.errors.append(f"{draft.id}: {e}")
            break

        try:
            if draft.action == "reply":
                resp = client.reply(draft.text, draft.target_id)
            elif draft.action == "quote":
                resp = client.quote(draft.text, draft.target_id)
            else:
                resp = client.post(draft.text)
        except XAPIError as e:
            draft.status = "failed"
            draft.reject_reason = str(e)[:200]
            report.errors.append(f"{draft.id}: {e}")
            _finalize(channels, draft, "⚠️ failed", str(e)[:120])
            if e.status in (401, 402, 403):
                ledger.pause(6, f"api {e.status}")
                break
            continue

        posted_id = (resp.get("data") or {}).get("id", "")
        draft.status = "published"
        draft.published_id = posted_id
        ledger.mark_acted(draft.target_id or posted_id)
        ledger.mark_conversation(draft.conversation_id)
        report.published.append(
            {
                "draft": draft.id,
                "action": draft.action,
                "target": draft.target_id,
                "posted": posted_id,
                "via": draft.decided_via,
                "edited": bool(draft.original_text),
                "dry_run": bool(resp.get("dry_run")),
            }
        )
        url = f"https://x.com/i/status/{posted_id}" if posted_id else ""
        _finalize(channels, draft, "✅ published", url)


def expire(queue: Queue, ttl_hours: float, report: PublishReport, channels: list | None = None) -> None:
    for draft in queue.expire(ttl_hours):
        report.expired.append(draft.id)
        _finalize(
            channels or [], draft, "⌛ expired",
            f"no decision in {ttl_hours:.0f}h — target is stale, not worth $0.015",
        )


def _finalize(channels: list, draft: Draft, outcome: str, detail: str = "") -> None:
    for ch in channels:
        try:
            ch.finalize(draft, outcome, detail)
        except Exception as e:  # noqa: BLE001 - a dead channel must not block publishing
            log.warning("finalize on %s failed: %s", getattr(ch, "name", "?"), e)


# --------------------------------------------------------------------- Nostr
def mirror_to_nostr(queue: Queue, cfg, report: PublishReport) -> None:
    """
    Send approved drafts to Nostr.

    Independent of the X result on purpose. Nostr costs nothing, so a draft you
    approved should go out even if the X write was blocked by budget — losing
    good content because a wallet is empty would be silly.

    Replies to strangers are skipped by default: on Nostr their parent doesn't
    exist, so the reply reads as a non-sequitur. Replies in your own threads are
    fine, and thread properly when the parent event is known.

    Note the drafts are already written to survive this. The reply prompt
    demands that "a stranger reading this reply in isolation should learn one
    concrete thing" — which is exactly the property that makes them safe to
    publish standalone.
    """
    if not getattr(cfg, "nostr", None) or not cfg.nostr.enabled:
        return
    if getattr(cfg, "dry_run", False):
        # Dry run has to mean dry run EVERYWHERE. Nostr costs nothing, which
        # made it tempting to let it through -- but then the calibration days,
        # where you read drafts with "nothing at stake", would silently be
        # publishing every approved draft to your followers.
        report.nostr.append({"skipped_all": "dry run — nothing published to Nostr"})
        return
    key = nip46.get_signer()
    if key is None:
        return

    relays = cfg.nostr.relays or nostr_mod.DEFAULT_RELAYS
    pool = nostr_mod.RelayPool(relays, key)
    emap = nostr_mod.EventMap(cfg.nostr.map_path)

    for draft in queue.drafts:
        if draft.nostr_event:
            continue
        if draft.status not in ("published", "approved"):
            continue
        if draft.status == "approved" and not cfg.nostr.mirror_when_x_blocked:
            continue
        if draft.kind == "outsider" and not cfg.nostr.include_outsider_replies:
            report.nostr.append({"draft": draft.id, "skipped": "outsider reply, no parent on nostr"})
            continue

        # Links are fine here -- no 13x penalty, no ranker burying them -- but
        # the draft still went through the X gates, so it won't contain one.
        parent = emap.get(draft.target_id) if cfg.nostr.thread_replies else ""
        ev = key.sign_event(nostr_mod.note(draft.text, reply_to=parent))
        results = pool.publish(ev)
        ok = [r for r in results if r.ok]

        if ok:
            draft.nostr_event = ev.id
            draft.nostr_relays_ok = len(ok)
            if draft.published_id:
                emap.put(draft.published_id, ev.id)
        report.nostr.append(
            {
                "draft": draft.id,
                "event": ev.id if ok else "",
                "relays_ok": len(ok),
                "relays_total": len(results),
                "threaded": bool(parent),
                "authed": any(r.authed for r in results),
                "errors": [f"{r.relay}: {r.message}" for r in results if not r.ok][:3],
            }
        )
        if not ok:
            report.errors.append(f"{draft.id}: no relay accepted the note")

    emap.save()


def backfill_map(cfg, queue: Queue, pubkey: str = "", limit: int = 50) -> tuple[int, int]:
    """
    Learn the event ids your existing bridge created, by pulling your own
    recent notes off the relays and fuzzy-matching them to X post text.

    Takes a PUBLIC key. Reading your own notes is a public operation, so this
    never touches the nsec — falls back to deriving the pubkey from
    NOSTR_NSEC only if you didn't pass one.

    Matching is conservative: threading a reply onto the wrong post is worse
    than leaving it unthreaded.

    Returns (mapped, notes_seen).
    """
    if pubkey:
        pk = nostr_mod.pubkey_from(pubkey)
    else:
        key = nip46.get_signer()
        if key is None:
            return 0, 0
        pk = key.pubkey_hex

    relays = cfg.nostr.relays or nostr_mod.DEFAULT_RELAYS
    notes = nostr_mod.fetch_own_notes(relays, pk, limit=limit)
    emap = nostr_mod.EventMap(cfg.nostr.map_path)
    found = 0
    for draft in queue.drafts:
        if not draft.target_id or emap.get(draft.target_id) or draft.kind == "outsider":
            continue
        match = nostr_mod.match_by_content(notes, draft.target_text)
        if match:
            emap.put(draft.target_id, match)
            found += 1
    emap.save()
    return found, len(notes)


def worth_bridging(text: str, min_words: int = 8) -> tuple[bool, str]:
    """
    Is this X post worth existing as a standalone Nostr note?

    Learned from the first live-ish reconcile, which offered to republish
    "@CoachDanGo Not good" and "Full guide: https://t.co/DDn8mIZPNH". On X
    those make sense in a thread; on Nostr they are noise with your name on it.
    """
    import re

    t = (text or "").strip()
    if t.startswith("@"):
        return False, "reply fragment (starts with a handle)"
    # strip t.co and other links, then see if anything substantive is left
    stripped = re.sub(r"https?://\S+", "", t).strip()
    if len(stripped.split()) < min_words:
        return False, f"only {len(stripped.split())} words once links are removed"
    if not stripped:
        return False, "nothing but a link"
    return True, ""


@dataclass
class Gap:
    """An X post that never made it to Nostr."""
    x_id: str
    text: str
    created_at: int          # unix seconds, from X
    matched_event: str = ""  # set when it WAS found, just missing from the map


def find_gaps(
    x_posts: list,
    notes: list[dict[str, Any]],
    emap,
    *,
    threshold: float = 0.82,
) -> tuple[list[Gap], int]:
    """
    Split X posts into (never bridged, already bridged).

    Repairing the map is the cheap half of this: a post that IS on Nostr but
    absent from our map costs nothing to relink, and relinking it means future
    replies thread onto it instead of floating loose.

    Returns (gaps, repaired, skipped).
    """
    from datetime import datetime, timezone

    gaps: list[Gap] = []
    repaired = 0
    skipped: list[tuple[str, str]] = []
    for p in x_posts:
        if emap.get(p.id):
            continue
        ok, why = worth_bridging(p.text)
        if not ok:
            skipped.append((p.id, why))
            continue
        match = nostr_mod.match_by_content(notes, p.text, threshold=threshold)
        if match:
            emap.put(p.id, match)
            repaired += 1
            continue
        ts = 0
        if p.created_at:
            try:
                dt = datetime.fromisoformat(p.created_at.replace("Z", "+00:00"))
                ts = int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
            except ValueError:
                ts = 0
        gaps.append(Gap(x_id=p.id, text=p.text, created_at=ts))
    return gaps, repaired, skipped


def reconcile_nostr(
    cfg,
    client,
    ledger: Ledger,
    report: PublishReport,
    *,
    pubkey: str = "",
    lookback: int = 20,
    live: bool = False,
) -> list[Gap]:
    """
    Diff your X timeline against your Nostr notes and republish what the
    bridge dropped.

    Costs: reading your own X posts is an OWNED read at $0.001 each, and X
    deduplicates billable resources within the 24h UTC window — so a reconcile
    run after a drafting run the same day re-reads those posts for free. The
    Nostr side costs nothing at all.

    Backdating: republished notes carry the ORIGINAL X timestamp, not now().
    Otherwise a post from three days ago surfaces at the top of your followers'
    feeds as if it were new, and your Nostr timeline ends up in a different
    order from your X one. Relays occasionally reject far-past events; the
    per-relay results will tell you if yours do.

    Dry-run by default. `live=True` publishes.
    """
    if not getattr(cfg, "nostr", None) or not cfg.nostr.enabled:
        return []

    relays = cfg.nostr.relays or nostr_mod.DEFAULT_RELAYS
    emap = nostr_mod.EventMap(cfg.nostr.map_path)

    me = client.me()
    me_id = me.get("id", "")
    if not me_id:
        report.errors.append("reconcile: could not resolve authenticated user")
        return []

    x_posts = [p for p in client.own_posts(me_id, max_results=lookback) if not p.is_retweet]
    pk = nostr_mod.pubkey_from(pubkey) if pubkey else None
    if pk is None:
        key = nostr_mod.key_from_env()
        if key is None:
            report.errors.append("reconcile: need --npub or NOSTR_NSEC")
            return []
        pk = key.pubkey_hex

    notes = nostr_mod.fetch_own_notes(relays, pk, limit=100)
    gaps, repaired, skipped = find_gaps(x_posts, notes, emap)
    emap.save()

    report.nostr.append(
        {
            "reconcile": True,
            "x_posts": len(x_posts),
            "nostr_notes": len(notes),
            "already_bridged": len(x_posts) - len(gaps) - repaired,
            "map_repaired": repaired,
            "gaps": len(gaps),
            "not_worth_bridging": len(skipped),
            "skipped_detail": skipped[:10],
            "live": live,
        }
    )
    if not live or not gaps:
        return gaps

    # Writing needs a signer; everything above did not.
    key = nip46.get_signer()
    if key is None:
        report.errors.append(
            "reconcile: NOSTR_BUNKER_URI (or NOSTR_NSEC) required to publish gaps"
        )
        return gaps

    pool = nostr_mod.RelayPool(relays, key)
    for gap in gaps:
        ev = key.sign_event(nostr_mod.Event(
            kind=nostr_mod.KIND_NOTE,
            content=gap.text,
            created_at=gap.created_at or 0,   # 0 -> the signer stamps now()
        ))
        results = pool.publish(ev)
        ok = [r for r in results if r.ok]
        if ok:
            emap.put(gap.x_id, ev.id)
            gap.matched_event = ev.id
        report.nostr.append(
            {
                "republished": gap.x_id,
                "event": ev.id if ok else "",
                "backdated_to": gap.created_at,
                "relays_ok": len(ok),
                "relays_total": len(results),
                "rejected": [f"{r.relay}: {r.message}" for r in results if not r.ok][:4],
            }
        )
        if not ok:
            report.errors.append(f"reconcile {gap.x_id}: no relay accepted")
    emap.save()
    return gaps
