# Peptide Frontier — auto-responder

Cost-aware auto-reply, auto-quote and candidate selection for
[@PeptideFrontier](https://x.com/PeptideFrontier), built to run on GitHub
Actions inside a **$6 X API credit balance**, with **you holding the final
call on every post**.

The design constraint that shaped everything: since 6 Feb 2026 the X API is
**pay-per-usage**. There is no free tier and no monthly allowance — every call
is a withdrawal. So the budget ledger is checked *before* each action, not
reconciled after, and the whole system is built to spend its money on the four
or five highest-value engagements per day rather than the most.

---

## The economics, in one table

| What | Unit price | Notes |
|---|---|---|
| Read your own mentions | **$0.001** / post | "Owned read" — 5x cheaper than any other read |
| Read someone else's post | $0.005 / post | Billed **per post returned**, not per request |
| Read a user object | $0.010 / user | Avoid; we never expand these |
| Post / reply / quote | $0.015 / request | |
| **Post containing a URL** | **$0.200** / request | **13.3x.** Hard-banned in `safety.py` |
| Like / repost / follow | $0.015 / request | Same price as a reply, for the weakest ranking signal |

Two consequences drive the whole architecture:

1. **Replying to your own commenters is the cheapest reach on the platform.**
   Sourcing a mention costs $0.001. Sourcing a stranger's post costs $0.005.
   Same $0.015 to reply to either. Mentions get an explicit priority bonus in
   the selector for exactly this reason.
2. **A URL costs 13x and gets buried anyway.** The open-sourced ranker gives
   `P(click)` a low weight — off-platform links are deprioritised. So a link is
   the worst possible trade: 13x the price for less distribution. `gate_reply()`
   rejects any draft containing one, and `_create_post()` rejects it again at
   the transport boundary.

Resources are **deduplicated within a 24h UTC window** — re-reading the same
mention later the same day is free. The ledger models this, which is why four
runs a day costs barely more than one.

Run the numbers for your config before spending anything:

```bash
PYTHONPATH=src python -m pf_autorespond.cli estimate
```

```
line item             WORST/day  EXPECT/day   detail
--------------------------------------------------------------------------
mentions read        $   0.0320 $    0.0164   8/run x4 runs
list posts read      $   0.0500 $    0.0500   10 x1/day
own timeline read    $   0.0200 $    0.0102   5/run x4 runs
replies              $   0.0450 $    0.0450   3/day
quotes               $   0.0150 $    0.0150   1/day
original posts       $   0.0150 $    0.0150   1/day
likes                $   0.0000 $    0.0000   0/day
--------------------------------------------------------------------------
DAILY                $   0.1770 $    0.1516
MONTHLY (x31)        $    5.487 $     4.701

  ok: at expected burn, $6.00 lasts ~40 days
```

It exits non-zero if the config can't fit the budget, so CI catches a bad edit
before X does.

---

## The hybrid loop

Default mode is `review`: the bot drafts, you decide, and **nothing is billed
until you approve**. One action is split across two workflow runs —

```
autorespond.yml   draft → gate → critique → enqueue → notify you     $0
       ↓
   (you tap ✅ on Telegram, or 👍 / /edit on GitHub)
       ↓
publish.yml       re-gate → publish → record cost                    $0.015
```

This makes review mode **cheaper than running autonomously**, not dearer. A
draft you reject costs a fraction of a cent of LLM tokens and zero X credits.
Only approvals reach the API.

### Two channels, different jobs

| | Telegram | GitHub Issues |
|---|---|---|
| Approve / reject | ✅ / ❌ inline buttons | 👍 / 👎 reaction, or `/approve`, `/reject` |
| Edit the text | — | `/edit <new text>` in a comment |
| Role | the push, on your phone | system of record, in git history |
| Third party sees drafts | yes | no |

Telegram deliberately can't edit: inline keyboards can't collect free text
without a webhook, and a second edit path would be one more thing to get
wrong. Fast yes/no on the phone, considered edits on GitHub.

**A reject always beats an approve.** If you tap ✅ then think better of it and
comment `/reject`, the reject wins regardless of arrival order
(`publisher.PRECEDENCE`).

**Every edit is re-gated.** Pasting a link into a reply is the easiest way to
turn a $0.015 post into a $0.200 one, and a human editing on a phone will do it
eventually. An edit containing a URL is rejected with the reason, not published:

```
status : rejected
reason : edit failed safety gate: contains a URL (13x cost, deprioritised by ranker)
spend  : $0.000000
```

**Drafts expire after 12 hours.** A reply approved 20 hours late is worse than
no reply — the target has aged past the ranker's `AgeFilter`, so you would pay
full price for near-zero distribution. Expiry is budget protection, not tidiness.

Approval-to-publish is bounded by the 20-minute poll (plus an instant trigger
on GitHub issue events). That lag is a feature: it's a window to change your
mind before anything is billed.

Switch to full autonomy any time with repo variable `PF_MODE=auto`.

### Approving from the terminal

```bash
PYTHONPATH=src python -m pf_autorespond.cli queue            # what's waiting
PYTHONPATH=src python -m pf_autorespond.cli decide <id> approve
PYTHONPATH=src python -m pf_autorespond.cli decide <id> edit --text "..."
PYTHONPATH=src python -m pf_autorespond.cli publish          # poll + publish
```

---

## Nostr

Approved drafts mirror to Nostr automatically. There is no per-action cost and
no ranking algorithm to please, so everything that forces restraint on X is
simply absent here — links are fine, volume is fine.

```
approved draft ──┬─→ X       $0.015, gated, rate-limited, ranked
                 └─→ Nostr   $0.00,  ungated, unranked
```

Deliberate asymmetries:

- **Nostr publishes even when the X budget blocked the post.** A good reply
  shouldn't die because the wallet is empty (`mirror_when_x_blocked`).
- **Rejecting still rejects everywhere.** Free doesn't mean unsupervised — only
  drafts that reached `approved` or `published` are mirrored.
- **Replies to strangers are skipped** (`include_outsider_replies: false`).
  Their parent post doesn't exist on Nostr, so the reply reads as a
  non-sequitur. Replies in your own threads mirror fine, and thread properly
  via NIP-10 when the parent event is known.

Standalone publishing works here because of a property the reply prompt already
enforces: *"a stranger reading this reply in isolation should learn one concrete
thing."* A draft written to survive without its parent on X survives without it
on Nostr too.

### Repairing bridge gaps

Bridges drop things. Yours dropped one post in four on 29 Jul — and the two
that *did* bridge weren't in our map, so replies would have floated loose
instead of threading.

`nostr reconcile` diffs your X timeline against your Nostr notes, relinks
what's already there, and republishes what's missing:

```
  X posts checked   : 4
  Nostr notes found : 3
  already bridged   : 0
  map repaired      : 3  (free — future replies now thread onto these)
  gaps              : 1

  2026-07-29 15:53 UTC  would republish
      Viking's oral VK2735 showed a 13.1% weight loss at 12 weeks in a Phase 1…

  dry run — add --live to republish
```

Notes:

- **Dry run by default.** Reporting the gap and fixing it are separate decisions.
- **Republished notes keep the original X timestamp.** Stamping `now()` would
  push a three-day-old post to the top of your followers' feeds and leave your
  Nostr timeline in a different order from your X one.
- **Repairing the map is the free half** and happens even in dry run — a post
  that *is* on Nostr but missing from our map costs nothing to relink, and
  relinking means future replies thread onto it.
- **Idempotent.** The map remembers; a second run republishes nothing.
- **Reads only need the npub.** `--live` is the only part that touches the nsec.
- Cost: 20 owned reads at $0.001 = **$0.02**, and those reads dedup for free
  against the drafting run within the same UTC day.

Runs weekly inside `watch.yml`. Set repo variable `NOSTR_RECONCILE_LIVE=1` to
republish instead of just reporting.

### Threading onto your existing bridge

Your bridge publishes the original posts, so this tool doesn't know their event
ids. `nostr backfill` pulls your recent notes off the relays and fuzzy-matches
them to X post text (bridges truncate and append links, so exact matching fails
on real data). Matching is conservative — threading onto the *wrong* post is
worse than not threading:

```bash
# reads — no secret key needed, ever
PYTHONPATH=src python -m pf_autorespond.cli nostr check
PYTHONPATH=src python -m pf_autorespond.cli nostr notes    --npub npub1...
PYTHONPATH=src python -m pf_autorespond.cli nostr backfill  --npub npub1...
PYTHONPATH=src python -m pf_autorespond.cli nostr reconcile --npub npub1...

# writes — these need NOSTR_NSEC
PYTHONPATH=src python -m pf_autorespond.cli nostr whoami
PYTHONPATH=src python -m pf_autorespond.cli nostr test
PYTHONPATH=src python -m pf_autorespond.cli nostr mirror
```

Reads take the **public** key. Requiring an nsec to list your own public notes
would be asking for a secret to do a job that doesn't need one — which is
exactly how secrets end up somewhere they shouldn't be. Pass an nsec where an
npub belongs and it errors loudly rather than accepting it.

Relay reachability varies a lot by network — datacenter IPs get 503s and 403s
from relays that work fine from a phone. `nostr check` tells you what's actually
reachable from wherever you're running it:

```
FAIL wss://relay.damus.io      503 Service Unavailable
ok   wss://nos.lol             245ms
FAIL wss://relay.nostr.band    timed out
ok   wss://relay.primal.net    275ms
FAIL wss://nostr.wine          403 Forbidden      (paid relay — needs your key)
```

### Keys and relays

Set `NOSTR_NSEC` as a repo secret — `nsec1…` or 64-char hex, both accepted.
It's read from the environment only and never written to the queue, the ledger,
a log, or a report. `NostrKey.__repr__` is redacted on purpose: a stack trace in
a public Actions log is the most likely way to leak it.

Signing is **coincurve**, a binding to libsecp256k1 — the same C library Bitcoin
Core uses. No hand-rolled crypto. Verified against the BIP-340 test vectors in
`tests/test_nostr.py`.

**NIP-42 is handled.** Relays increasingly require AUTH before accepting writes,
and an unauthenticated bridge fails *silently* — the relay just drops the event.
On an `AUTH` challenge the pool signs a kind-22242 event and retries. That's the
failure most likely to bite a bridge that used to work, which is why it's tested
rather than assumed.

---

## Weekly change watcher

`watch.yml` runs Mondays and diffs three pages against committed snapshots:

- `xai-org/x-algorithm` README — xAI committed to ~4-weekly updates with
  developer notes, so ranking changes appear here before they appear in your
  analytics.
- X API pricing — the docs say rates are "subject to change", and at a $6
  balance a rate change is the difference between 40 days and 4.
- X API rate limits — quieter, but a cut can silently break the run.

It **only opens an issue when something actually changed**, and flags anything
touching prices, weights, rate limits or deprecations as 🔴 critical. Timestamps,
star counts and build hashes are filtered out as noise — a watcher that pings
every week gets muted, and then you miss the week that mattered.

Costs nothing: plain HTTP to public pages, no X credits, no LLM.

## What it actually does, per run

1. **Read mentions** ($0.001/post) — replies sitting on your own posts.
2. **Read your own recent posts** ($0.001/post) — so a reply answers what you
   actually said, rather than guessing from the commenter's text alone.
3. **Read a curated List** ($0.005/post) — *only* if there's still reply quota
   left. Paying to find candidates you can't afford to answer is pure waste.
4. **Score and filter** everything into one queue, then take the top 2.
5. **Generate → gate → critique → publish.**

Four runs a day at 2 actions each is an 8-action ceiling; the daily quotas cut
it to ~4. That's deliberate. At this budget, being selective isn't a compromise
— it's the strategy.

## Selection

Scoring follows what the [open-sourced ranker](https://github.com/xai-org/x-algorithm)
rewards:

- **freshness** (4h half-life) — the `AgeFilter` drops old posts before scoring,
  so replying to a 12-hour-old post buys almost no out-of-network reach.
- **topicality** — Phoenix retrieval is two-tower embedding similarity. Staying
  tightly inside peptide science is what gets the account surfaced at all.
- **reach**, log-scaled with both tails knocked down — a 2M-follower reply
  section is a firehose nobody reads; 5k–50k is where a reply is visible *and*
  the audience is relevant.
- **conversation heat**, capped — join threads the ranker is already
  circulating, but not pile-ons.

Hard rejects (score < 0): retweets, engagement bait, ratioed pile-ons, anything
stale.

State-based rejects, all free: already acted on, **already in this conversation**
(mirrors X's `DedupConversationFilter` — a second reply in one thread costs
$0.015 and adds ~zero reach), author within a 72h cooldown, our own post.

## Safety

Running fully autonomous on a medical-adjacent topic, the failure that ends the
account isn't a typo. `gate_target()` refuses to engage:

| Refused | Why |
|---|---|
| Distress / acute medical ("overdosed", "chest pain") | Never the right responder |
| Vendors ("DM for price", "research chemicals", "crypto only") | Engaging boosts them and associates you with them |
| Hostility and bait ("shill", "bot account", "cope") | Converts into a quote-dunk; feeds `P(block_author)` |
| Pregnancy, minors, cancer, eating disorders | Liability regardless of framing |
| "Should I…" personal advice requests | Not answerable without practising medicine |
| Accounts < 14 days old, protected accounts | Spam |

`gate_reply()` then refuses to publish text containing a URL, hashtag, @handle,
dosing pattern (`250mcg twice daily`), advice phrasing ("you should take",
"I recommend"), sales phrasing, hype register, empty-agreement openers, or
model self-disclosure.

Then a second LLM pass (`prompts.CRITIC`, temperature 0) reviews the draft and
**fails closed** — if the critic is unreachable, nothing publishes. Its main job
is catching invented citations, which is the one failure that would do lasting
damage to an account whose entire pitch is *cited*.

A rejected draft costs a fraction of a cent to regenerate. A bad published
reply costs $0.015 **and** trains the ranker that this account produces content
people mute. That asymmetry is why the gates are strict.

---

## Rehearsal

```bash
PYTHONPATH=src:. python rehearsal/run.py
```

Runs the whole pipeline against fixtures — no credentials, no network, no
spend. See `rehearsal/README.md`. It earns its keep: the 2026-08-01 run caught
a scoring bug that ranked strangers' posts above unanswered replies on your own
threads.

## Setup

**1. Install**

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src:. pytest -q          # 301 tests, no network
```

**2. Create a private X List** of 20–40 accounts worth replying to —
researchers, trial-watchers, endocrinology and obesity-medicine people. Paste
its ID into `config.yaml` as `harvest.list_id`. A curated list is far cheaper
and higher-signal than search: same $0.005/post, much better hit rate.

**3. Telegram** — run `python setup_telegram.py`. It validates the token,
discovers your chat id, sends a live test with Approve/Reject buttons to
confirm the callback round-trip, and writes both secrets via `gh`. The only
manual step is the @BotFather conversation, which needs your Telegram account.

**4. Repository secrets** (Settings → Secrets and variables → Actions):

| Secret | |
|---|---|
| `X_CONSUMER_KEY` / `X_CONSUMER_SECRET` | X app, OAuth 1.0a |
| `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET` | Read **and write** permission |
| `LLM_API_KEY` | Anthropic by default |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TELEGRAM_CHAT_ID` | your numeric user id — or run `python setup_telegram.py` |
| `NOSTR_NSEC` | `nsec1…` or hex; omit to disable Nostr |

`GITHUB_TOKEN` is provided automatically by Actions; no setup needed.
Optional variable `PF_APPROVER` overrides who may approve (defaults to the
repo owner). **Only that login can spend your credits** — a stranger's 👍 on
a public issue is ignored.

Optional repo *variables*: `LLM_PROVIDER` (`anthropic` \| `openai`),
`LLM_MODEL`, `LLM_BASE_URL` (point at Groq / xAI / DeepSeek / OpenRouter),
`PF_LIVE`, and `PF_MODE` (`review` \| `auto`).

**5. Set your spending limit in the X Developer Console.** Set it to $6 and
leave auto-recharge **off**. The ledger is a soft guard that trusts its own
arithmetic; the console limit is the one X enforces. Belt and braces.

**6. Dry-run for a few days.** It ships with `PF_DRY_RUN=1` and `mode: review`.
Reads still cost (~$0.05/day) because you need real mentions to judge real
drafts — only the publish is suppressed. You'll get the full approval flow with
nothing at stake, which is the cheapest way to calibrate the prompts.

**7. Go live** by setting repo variable `PF_LIVE=1`. Going live is a deliberate,
auditable change — not a flag you can typo into.

## Operating it

```bash
PYTHONPATH=src python -m pf_autorespond.cli status     # budget, quotas, pauses
PYTHONPATH=src python -m pf_autorespond.cli estimate   # cost model
PYTHONPATH=src python -m pf_autorespond.cli run --dry-run
PYTHONPATH=src python -m pf_autorespond.cli pause --hours 48 --reason "..."
PYTHONPATH=src python -m pf_autorespond.cli resume
```

`pause` is the kill switch — it writes to the ledger, so it survives across
runs and every subsequent workflow refuses to act until you `resume`.

State lives in `state/ledger.json`, committed back by the workflow after every
run. That gives durable spend tracking (the Actions cache can be evicted
mid-month) plus a full audit trail in git history. The workflow uses
`concurrency: cancel-in-progress: false` — two runs writing the ledger at once
would lose spend records, and a cancelled run can publish without recording
the cost.

## Watch these first

- **Does the reply get a reply?** `P(reply)` is the heaviest positive signal.
- **Profile clicks and follows** — the only mechanism that actually grows this
  account. Likes are near-worthless and cost the same as a reply, which is why
  `quotas.like` ships at 0.
- **Mutes and blocks.** These carry *negative* weight and attach to you, not to
  the post. A single block reportedly outweighs several likes. If replies stop
  landing, pause and read the last 20 drafts before spending more.

## Layout

```
src/pf_autorespond/
  pricing.py    X API cost table + rate limits (verified 2026-08-01)
  ledger.py     budget, quotas, dedup, cooldowns, kill switch  [95% covered]
  safety.py     target and reply gates                          [95% covered]
  selector.py   candidate scoring                               [88% covered]
  xclient.py    X API v2 client with per-call cost accounting   [68% covered]
  llm.py        Anthropic / OpenAI-compatible generation + critic
  prompts.py    voice, style rules, hard limits, critic
  engine.py     orchestration (auto + review modes)
  queue.py      pending drafts, expiry, dedup                    [95% covered]
  channels.py   GitHub Issues + Telegram approval channels
  publisher.py  verdict precedence, re-gating, publishing
  watch.py      weekly algorithm/pricing diff                    [90% covered]
  nostr.py      NIP-01/10/19/42 — signing, relays, threading     [93% covered]
  cli.py        run / publish / queue / decide / watch / status / pause / estimate
```

Not tweepy, deliberately: under pay-per-usage you need to know exactly how many
resources a call will bill *before* issuing it, and tweepy's convenience layer
hides pagination and field expansion — both billed per resource.

## Licence

Private. Not medical advice; neither is anything it publishes.
