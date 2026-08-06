"""
Orchestration.

Fixed order per run, cheapest signal first:

  1. mentions      ($0.001/resource, owned read) -- always
  2. list posts    ($0.005/resource)             -- only if budget allows
  3. search        ($0.005/resource)             -- only if the list came up dry

Then: score everything together, take the top N that fit the remaining budget
and quotas, generate, critique, publish. Every step can bail without spending.

The run is intentionally small. Four runs a day at max_actions_per_run=2 gives
at most 8 published actions, and the quotas cap it lower than that. Spreading
across the day also avoids the burst pattern that reads as automation and
trips the author-diversity attenuation.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import llm, prompts, safety
from .config import Config
from .ledger import BudgetExceeded, Ledger
from .queue import Draft, Queue, make_id
from .selector import Candidate, apply_outbound_reserve, build_candidates, diversify
from .xclient import Post, XAPIError, XClient

log = logging.getLogger(__name__)


def sel_reserve(cfg) -> int:
    return getattr(cfg.selection, "reserved_outbound_per_day", 0)


@dataclass
class RunReport:
    started: str = ""
    dry_run: bool = True
    harvested: dict[str, int] = field(default_factory=dict)
    considered: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)
    published: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    spend_before: str = "0"
    spend_after: str = "0"
    errors: list[str] = field(default_factory=list)
    ledger_summary: str = ""
    queued: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "review"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class Engine:
    def __init__(
        self,
        cfg: Config,
        client: XClient,
        ledger: Ledger,
        queue: Queue | None = None,
        channels: list | None = None,
    ):
        self.cfg = cfg
        self.client = client
        self.ledger = ledger
        self.queue = queue
        self.channels = channels or []
        self.report = RunReport(
            started=datetime.now(timezone.utc).isoformat(),
            dry_run=cfg.dry_run,
            mode=getattr(cfg, "mode", "review"),
        )

    @property
    def review_mode(self) -> bool:
        return getattr(self.cfg, "mode", "review") == "review" and self.queue is not None

    # ------------------------------------------------------------------ run
    def run(self) -> RunReport:
        self.report.spend_before = str(self.ledger.month_spend())

        if self.ledger.is_paused():
            self.report.errors.append(
                f"paused until {self.ledger.paused_until}: {self.ledger.pause_reason}"
            )
            self.report.spend_after = str(self.ledger.month_spend())
            self.report.ledger_summary = self.ledger.summary()
            return self.report

        # Reserve: stop acting while there is still a float in the wallet, so
        # running out is a decision rather than a surprise mid-conversation.
        if self.ledger.remaining_month() <= self.cfg.budget.reserve_usd:
            self.report.errors.append(
                f"reserve reached: ${self.ledger.remaining_month()} left, "
                f"reserve ${self.cfg.budget.reserve_usd}"
            )
            self.report.spend_after = str(self.ledger.month_spend())
            self.report.ledger_summary = self.ledger.summary()
            return self.report

        try:
            me = self.client.me()
            me_id = me.get("id", "")
        except XAPIError as e:
            self.report.errors.append(f"auth/me failed: {e}")
            return self.report
        if not me_id:
            self.report.errors.append("could not resolve authenticated user id")
            return self.report

        candidates = self._harvest(me_id)
        self.report.considered = len(candidates)
        if not candidates:
            self.report.spend_after = str(self.ledger.month_spend())
            self.report.ledger_summary = self.ledger.summary()
            return self.report

        chosen = diversify(candidates, self.cfg.max_actions_per_run)
        chosen, note = apply_outbound_reserve(
            chosen, candidates, self.ledger, sel_reserve(self.cfg)
        )
        if note:
            self.report.errors.append(note) if "no eligible" in note else None
            log.info("%s", note)
            self.report.harvested["reserve"] = 1
        for cand in chosen:
            try:
                self._act(cand, me_id)
            except BudgetExceeded as e:
                self.report.errors.append(f"budget stop: {e}")
                break
            except XAPIError as e:
                self.report.errors.append(f"api error on {cand.post.id}: {e}")
                if e.status in (401, 402, 403):
                    self.ledger.pause(6, f"api {e.status}")
                    break
            except Exception as e:  # noqa: BLE001 - never let one target kill the run
                self.report.errors.append(f"{type(e).__name__} on {cand.post.id}: {e}")
            # Human-ish spacing between actions inside a run.
            gap = getattr(self.cfg, "gap_seconds_max", 0.0)
            if not self.cfg.dry_run and not self.review_mode and gap > 0 and cand is not chosen[-1]:
                time.sleep(random.uniform(getattr(self.cfg, "gap_seconds_min", 0.0), gap))

        self.report.spend_after = str(self.ledger.month_spend())
        self.report.ledger_summary = self.ledger.summary()
        return self.report

    # -------------------------------------------------------------- harvest
    def _harvest(self, me_id: str) -> list[Candidate]:
        sel = self.cfg.selection
        all_candidates: list[Candidate] = []

        # 1. Mentions. Cheapest thing we buy, and the replies sitting unanswered
        #    on our own posts are the highest-value targets on the platform.
        try:
            mentions = self.client.mentions(me_id, max_results=self.cfg.harvest.mentions_max)
            self.report.harvested["mentions"] = len(mentions)
            acc, rej = build_candidates(
                mentions, "mention",
                me_id=me_id, ledger=self.ledger,
                min_score=sel.min_score_mention,
                author_cooldown_hours=sel.author_cooldown_hours,
                # Own threads stay warm far longer than the feed does.
                max_age_hours=sel.mention_max_age_hours,
                freshness_half_life=sel.mention_half_life,
                max_replies_per_conversation=sel.max_replies_per_conversation,
                conversation_gap_hours=sel.conversation_gap_hours,
            )
            # Attach what WE said, so the reply answers the real point.
            if acc:
                try:
                    mine = self.client.own_posts(
                        me_id, max_results=self.cfg.harvest.own_posts_max
                    )
                    roots = {p.conversation_id: p.text for p in mine}
                    roots.update({p.id: p.text for p in mine})
                    for c in acc:
                        c.root_text = roots.get(c.post.conversation_id, "")
                except (XAPIError, BudgetExceeded) as e:
                    self.report.errors.append(f"own_posts: {e}")
            all_candidates += acc
            self.report.rejected += rej
        except (XAPIError, BudgetExceeded) as e:
            self.report.errors.append(f"mentions: {e}")

        # 2. Curated list. Only if we can still afford to act on what we find --
        #    reading candidates we cannot afford to reply to is pure waste.
        min_write = Decimal("0.015")
        read_cost = Decimal("0.005") * self.cfg.harvest.list_posts_max
        if (
            self.cfg.harvest.list_id
            and self.ledger.remaining_quota("read_list_posts") > 0
            and self.ledger.remaining_day() > read_cost + min_write
            and self.ledger.remaining_quota("reply") + self.ledger.remaining_quota("quote") > 0
        ):
            try:
                posts = self.client.list_posts(
                    self.cfg.harvest.list_id, max_results=self.cfg.harvest.list_posts_max
                )
                self.report.harvested["list"] = len(posts)
                acc, rej = build_candidates(
                    posts, "outsider",
                    me_id=me_id, ledger=self.ledger,
                    min_followers=sel.min_followers_outsider,
                    min_score=sel.min_score_outsider,
                    author_cooldown_hours=sel.author_cooldown_hours,
                    max_age_hours=sel.max_age_hours,
                    freshness_half_life=sel.freshness_half_life,
                    max_replies_per_conversation=sel.max_replies_per_conversation,
                    conversation_gap_hours=sel.conversation_gap_hours,
                )
                all_candidates += acc
                self.report.rejected += rej
            except (XAPIError, BudgetExceeded) as e:
                self.report.errors.append(f"list: {e}")
        else:
            self.report.harvested["list"] = 0

        # 3. Search, only as a fallback when the list produced nothing usable.
        if (
            not any(c.kind == "outsider" for c in all_candidates)
            and self.cfg.harvest.search_queries
            and self.ledger.remaining_quota("read_search") > 0
            and self.ledger.remaining_day() > read_cost + min_write
        ):
            q = random.choice(self.cfg.harvest.search_queries)
            try:
                posts = self.client.search_recent(q, max_results=self.cfg.harvest.search_max)
                self.report.harvested["search"] = len(posts)
                acc, rej = build_candidates(
                    posts, "outsider",
                    me_id=me_id, ledger=self.ledger,
                    min_followers=sel.min_followers_outsider,
                    min_score=sel.min_score_outsider,
                    author_cooldown_hours=sel.author_cooldown_hours,
                    max_age_hours=sel.max_age_hours,
                    freshness_half_life=sel.freshness_half_life,
                    max_replies_per_conversation=sel.max_replies_per_conversation,
                    conversation_gap_hours=sel.conversation_gap_hours,
                )
                all_candidates += acc
                self.report.rejected += rej
            except (XAPIError, BudgetExceeded) as e:
                self.report.errors.append(f"search: {e}")

        all_candidates.sort(key=lambda c: c.score, reverse=True)
        return all_candidates

    # ------------------------------------------------------------------ act
    def _act(self, cand: Candidate, me_id: str) -> None:
        action = "reply" if cand.kind in ("mention", "outsider") else "quote"
        # Check affordability even in review mode: drafting something you
        # could never afford to publish wastes your attention, not just credits.
        self.ledger.require(action)

        if self.review_mode and (
            self.queue.has_pending_for(cand.post.id)
            or self.queue.has_pending_in_conversation(cand.post.conversation_id)
        ):
            self.report.skipped.append(
                {"id": cand.post.id, "reason": "already awaiting your decision"}
            )
            return

        system, context = self._build_prompt(cand)
        text, why = self._generate(system, context)
        if text is None:
            # `why` distinguishes model-skip / gate / critic / llm-error. The
            # old catch-all message made a run of zero drafts unreadable.
            self.report.skipped.append(
                {"id": cand.post.id, "kind": cand.kind, "target": cand.post.author_handle,
                 "reason": why}
            )
            return

        if self.review_mode:
            self._enqueue(cand, action, text)
            return

        if action == "reply":
            resp = self.client.reply(text, cand.post.id)
        else:
            resp = self.client.quote(text, cand.post.id)

        self.ledger.mark_acted(cand.post.id)
        self.ledger.mark_conversation(cand.post.conversation_id)
        self.ledger.touch_author(cand.post.author_id)
        self.ledger.record_kind(cand.kind)
        self.report.published.append(
            {
                "id": resp.get("data", {}).get("id", ""),
                "action": action,
                "target": cand.post.id,
                "target_author": cand.post.author_handle,
                "score": round(cand.score, 2),
                "text": text,
                "dry_run": bool(resp.get("dry_run")),
            }
        )

    def _enqueue(self, cand: Candidate, action: str, text: str) -> None:
        """Write the draft and ask you. Costs $0 in X credits until approved."""
        draft = Draft(
            id=make_id(action, cand.post.id, text),
            action=action,
            target_id=cand.post.id,
            target_author=cand.post.author_handle,
            target_text=cand.post.text,
            conversation_id=cand.post.conversation_id,
            text=text,
            score=round(cand.score, 2),
            kind=cand.kind,
        )
        self.queue.add(draft)

        for ch in self.channels:
            try:
                ch.announce(draft, self.cfg.approval_ttl_hours)
            except Exception as e:  # noqa: BLE001 - one dead channel must not lose the draft
                self.report.errors.append(f"announce via {getattr(ch, 'name', '?')}: {e}")

        if not self.channels:
            self.report.errors.append(
                "no approval channel configured — draft is in state/queue.json only"
            )

        # Reserve the slot so the next run in the same day doesn't re-target
        # this post while you're still deciding.
        self.ledger.touch_author(cand.post.author_id)
        self.ledger.record_kind(cand.kind)
        # NOT mark_conversation: a queued draft is not a reply yet. The queue's
        # has_pending_in_conversation stops a second draft for the same thread
        # while this one is undecided; the ledger's conversation log counts
        # replies that actually published. Marking here too double-counted, so
        # a cap of 2 was consumed by a single reply.
        self.report.queued.append(
            {
                "id": draft.id,
                "action": action,
                "target": cand.post.id,
                "target_author": cand.post.author_handle,
                "score": draft.score,
                "text": text,
                "github_issue": draft.github_issue,
            }
        )

    def _build_prompt(self, cand: Candidate) -> tuple[str, str]:
        p = cand.post
        if cand.kind == "mention":
            return prompts.REPLY_TO_MENTION, prompts.mention_context(
                root_text=cand.root_text or "(your original post)",
                reply_text=p.text,
                author_handle=p.author_handle or "someone",
            )
        if cand.kind == "quote":
            return prompts.QUOTE_POST, prompts.quote_context(p.text, p.author_handle)
        return prompts.REPLY_TO_OUTSIDER, prompts.outsider_context(
            p.text, p.author_handle, p.author_followers
        )

    def _generate(self, system: str, context: str) -> tuple[str | None, str]:
        """
        Generate -> clean -> gate -> critique.

        Returns (text, reason). `text` is None when nothing publishable came
        out, and `reason` says which stage stopped it -- the model declining,
        a safety gate, the critic, or the API. Those four have completely
        different fixes, so collapsing them into one message (as this did
        until 2026-08-06) makes a zero-draft run impossible to diagnose.
        """
        last = "no attempts made"
        for attempt in range(self.cfg.max_regenerations + 1):
            try:
                raw = llm.generate(system, context)
            except llm.LLMError as e:
                self.report.errors.append(f"llm: {e}")
                return None, f"llm error: {str(e)[:100]}"
            if llm.is_skip(raw):
                return None, f"model declined: {llm.skip_reason(raw)}"

            text = safety.strip_risky(raw)
            verdict = safety.gate_reply(text)
            if not verdict:
                last = f"gate: {verdict.reason}"
                log.info("gate rejected (attempt %d): %s", attempt + 1, verdict.reason)
                self.report.skipped.append({"reason": last, "draft": text[:120]})
                continue

            if self.cfg.use_critic:
                passed, why = llm.critique(text, context)
                if not passed:
                    last = f"critic: {why}"
                    log.info("critic rejected (attempt %d): %s", attempt + 1, why)
                    self.report.skipped.append({"reason": last, "draft": text[:120]})
                    continue
            return text, ""
        return None, f"exhausted {self.cfg.max_regenerations + 1} attempts; last: {last}"


# ------------------------------------------------------------------ logging
def append_log(path: str | Path, report: RunReport) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report.to_dict(), default=str) + "\n")
