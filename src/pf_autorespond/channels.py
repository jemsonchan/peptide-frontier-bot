"""
Approval channels.

Two, with different jobs:

  GitHubChannel   -- system of record. One issue per draft, in your own repo,
                     no third party in the loop. Supports EDITS, because a
                     comment can carry arbitrary text. Full audit trail in git.

  TelegramChannel -- the push. Inline Approve/Reject buttons on your phone, so
                     a yes/no takes two seconds. Deliberately does NOT support
                     editing: Telegram inline keyboards can't collect free text
                     without a webhook, and bolting on a reply-parser would be
                     a second, worse edit path. Edits happen on GitHub.

Both are polled from a scheduled workflow, so approval-to-publish is bounded by
the poll interval (20 min by default), not instant. That is a feature at this
budget: it gives you a window to change your mind before anything is billed.

SECURITY: only the configured approver can decide. GitHub checks the commenter/
reactor login against `approver_login`; Telegram checks the callback sender id
against `chat_id`. Without those checks, a public repo issue would let anyone
spend your credits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from .queue import Decision, Draft

log = logging.getLogger(__name__)

APPROVE_WORDS = ("/approve", "/ok", "/yes", "approve")
REJECT_WORDS = ("/reject", "/no", "/skip", "reject")
EDIT_PREFIX = "/edit"

GH_API = "https://api.github.com"
TG_API = "https://api.telegram.org"


def _body(draft: Draft, ttl_hours: float) -> str:
    url = draft.target_url()
    target = f"[{draft.target_id}]({url})" if url else "(new post)"
    return (
        f"**{draft.action.upper()}** to @{draft.target_author or '?'} — {target}\n\n"
        f"> {draft.target_text[:400] or '(n/a)'}\n\n"
        f"---\n\n"
        f"### Draft\n\n{draft.text}\n\n"
        f"---\n\n"
        f"`{len(draft.text)}` chars · score `{draft.score:.2f}` · kind `{draft.kind}` · "
        f"costs **$0.015** if approved · expires in **{ttl_hours:.0f}h**\n\n"
        f"👍 approve · 👎 reject · or comment `/edit <new text>`\n\n"
        f"<sub>draft `{draft.id}`</sub>"
    )


# ------------------------------------------------------------------- GitHub
@dataclass
class GitHubChannel:
    repo: str            # "owner/name"
    token: str
    approver_login: str
    label: str = "pf-draft"
    timeout: int = 30

    name = "github"

    def _req(self, method: str, path: str, **kw) -> Any:
        r = requests.request(
            method,
            f"{GH_API}{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=self.timeout,
            **kw,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"github {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def announce(self, draft: Draft, ttl_hours: float) -> None:
        issue = self._req(
            "POST",
            f"/repos/{self.repo}/issues",
            json={
                "title": f"[{draft.action}] to @{draft.target_author or '?'} · {draft.id}",
                "body": _body(draft, ttl_hours),
                "labels": [self.label],
            },
        )
        draft.github_issue = issue["number"]

    def poll(self, drafts: list[Draft]) -> list[Decision]:
        by_issue = {d.github_issue: d for d in drafts if d.github_issue}
        if not by_issue:
            return []
        decisions: list[Decision] = []
        for number, draft in by_issue.items():
            try:
                decisions.extend(self._poll_one(number, draft))
            except RuntimeError as e:
                log.warning("github poll failed for #%s: %s", number, e)
        return decisions

    def _poll_one(self, number: int, draft: Draft) -> list[Decision]:
        out: list[Decision] = []

        # Comments first: an explicit /edit or /reject should beat a stale 👍.
        for c in self._req("GET", f"/repos/{self.repo}/issues/{number}/comments"):
            login = (c.get("user") or {}).get("login", "")
            if login.lower() != self.approver_login.lower():
                continue  # only you can spend your credits
            body = (c.get("body") or "").strip()
            low = body.lower()
            if low.startswith(EDIT_PREFIX):
                new_text = body[len(EDIT_PREFIX):].strip()
                if new_text:
                    out.append(Decision(draft.id, "edit", self.name, login, new_text=new_text))
            elif any(low.startswith(w) for w in REJECT_WORDS):
                out.append(Decision(draft.id, "reject", self.name, login, note=body[:100]))
            elif any(low.startswith(w) for w in APPROVE_WORDS):
                out.append(Decision(draft.id, "approve", self.name, login))

        for r in self._req("GET", f"/repos/{self.repo}/issues/{number}/reactions"):
            login = (r.get("user") or {}).get("login", "")
            if login.lower() != self.approver_login.lower():
                continue
            content = r.get("content")
            if content in ("+1", "rocket", "hooray"):
                out.append(Decision(draft.id, "approve", self.name, login))
            elif content in ("-1", "confused"):
                out.append(Decision(draft.id, "reject", self.name, login))
        return out

    def finalize(self, draft: Draft, outcome: str, detail: str = "") -> None:
        if not draft.github_issue:
            return
        try:
            self._req(
                "POST",
                f"/repos/{self.repo}/issues/{draft.github_issue}/comments",
                json={"body": f"**{outcome}**{(' — ' + detail) if detail else ''}"},
            )
            self._req(
                "PATCH",
                f"/repos/{self.repo}/issues/{draft.github_issue}",
                json={"state": "closed"},
            )
        except RuntimeError as e:
            log.warning("github finalize failed: %s", e)


# ----------------------------------------------------------------- Telegram
@dataclass
class TelegramChannel:
    token: str
    chat_id: str
    timeout: int = 30

    name = "telegram"

    def _req(self, method: str, **payload) -> Any:
        r = requests.post(
            f"{TG_API}/bot{self.token}/{method}", json=payload, timeout=self.timeout
        )
        data = r.json() if r.text else {}
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {str(data)[:300]}")
        return data.get("result")

    def announce(self, draft: Draft, ttl_hours: float) -> None:
        url = draft.target_url()
        text = (
            f"*{draft.action.upper()}* to @{_esc(draft.target_author or '?')}\n"
            f"_{_esc(draft.target_text[:200])}_\n\n"
            f"{_esc(draft.text)}\n\n"
            f"{len(draft.text)} chars · score {draft.score:.2f} · $0.015 if approved\n"
            f"expires in {ttl_hours:.0f}h · edit on GitHub"
        )
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "✅ Approve", "callback_data": f"a:{draft.id}"},
                    {"text": "❌ Reject", "callback_data": f"r:{draft.id}"},
                ] + ([{"text": "🔗 Target", "url": url}] if url else [])]
            },
        }
        res = self._req("sendMessage", **payload)
        draft.telegram_message = (res or {}).get("message_id", 0)

    def poll(self, drafts: list[Draft], offset: int = 0) -> tuple[list[Decision], int]:
        """
        Long-poll disabled (timeout=0) -- this runs in a cron job, not a daemon.
        Returns (decisions, new_offset); persist the offset or you reprocess
        the same callbacks forever.
        """
        known = {d.id for d in drafts}
        try:
            updates = self._req("getUpdates", offset=offset, timeout=0, allowed_updates=["callback_query"]) or []
        except RuntimeError as e:
            log.warning("telegram poll failed: %s", e)
            return [], offset

        out: list[Decision] = []
        new_offset = offset
        for u in updates:
            new_offset = max(new_offset, u.get("update_id", 0) + 1)
            cq = u.get("callback_query")
            if not cq:
                continue
            sender = str((cq.get("from") or {}).get("id", ""))
            data = cq.get("data", "")
            if sender != str(self.chat_id):
                log.warning("ignoring telegram callback from %s", sender)
                self._ack(cq.get("id"), "not authorised")
                continue
            verdict, _, draft_id = data.partition(":")
            if draft_id not in known:
                self._ack(cq.get("id"), "draft no longer pending")
                continue
            if verdict == "a":
                out.append(Decision(draft_id, "approve", self.name, sender))
                self._ack(cq.get("id"), "approved — publishing")
            elif verdict == "r":
                out.append(Decision(draft_id, "reject", self.name, sender))
                self._ack(cq.get("id"), "rejected")
        return out, new_offset

    def _ack(self, callback_id: str | None, text: str) -> None:
        if not callback_id:
            return
        try:
            self._req("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except RuntimeError:
            pass

    def finalize(self, draft: Draft, outcome: str, detail: str = "") -> None:
        if not draft.telegram_message:
            return
        try:
            # Strip the buttons so a stale message can't be tapped twice.
            self._req(
                "editMessageReplyMarkup",
                chat_id=self.chat_id,
                message_id=draft.telegram_message,
                reply_markup={"inline_keyboard": []},
            )
            self._req(
                "sendMessage",
                chat_id=self.chat_id,
                text=f"{outcome}{(' — ' + detail) if detail else ''}",
                reply_to_message_id=draft.telegram_message,
                disable_web_page_preview=True,
            )
        except RuntimeError as e:
            log.warning("telegram finalize failed: %s", e)

    def notify(self, text: str) -> None:
        try:
            self._req("sendMessage", chat_id=self.chat_id, text=text,
                      disable_web_page_preview=True)
        except RuntimeError as e:
            log.warning("telegram notify failed: %s", e)


def _esc(s: str) -> str:
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


def build_channels(cfg) -> list:
    """Whichever channels are configured. Missing credentials = channel off."""
    import os

    out = []
    repo = os.getenv("GITHUB_REPOSITORY", "")
    gh_token = os.getenv("GITHUB_TOKEN", "")
    approver = os.getenv("PF_APPROVER", "") or (repo.split("/")[0] if repo else "")
    if repo and gh_token and approver:
        out.append(GitHubChannel(repo=repo, token=gh_token, approver_login=approver))

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat:
        out.append(TelegramChannel(token=tg_token, chat_id=tg_chat))
    return out
