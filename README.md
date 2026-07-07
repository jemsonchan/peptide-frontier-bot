# Peptide Frontier — X Content Bot

Automated daily, evidence-grade peptide-education posts for [@peptidefrontier](https://x.com/peptidefrontier). Built on the same GitHub-Actions-cron pattern as the weather bots — no server, ~$6/year to run.

**What it does:** each day it picks a content pillar, grounds the topic against real literature (Europe PMC), drafts a ≤275-char post with an LLM using a strict system prompt, runs deterministic guardrails (no hype, no medical advice, no dupes), and posts text-only to X.

Full reasoning — the content vacuum, the moat, the pipeline, the stack, and 2026 compliance — is in **[STRATEGY.md](STRATEGY.md)**. The writer's rules are in **[system_prompt.md](system_prompt.md)**.

## Files
```
content_bot.py                 # the pipeline (topic -> ground -> draft -> guardrail -> post)
system_prompt.md               # the LLM's rules (the moat, encoded)
data/topics.json               # content-pillar bank
data/posted_history.json       # dedupe log (auto-committed by the workflow)
.github/workflows/peptide_bot.yml
requirements.txt / .env.example
```

## How the pipeline works
1. **Topic** — pillar chosen by weekday (Wed & Fri = "Study of the Week"), subtopic not used recently.
2. **Grounding** — Europe PMC search returns real paper titles/journals/years (free, no key). Non-fatal if it fails.
3. **Draft** — Gemini 2.5 Flash writes from `system_prompt.md` + the grounded facts.
4. **Guardrails** — length ≤275, banned-phrase list, no prescriptive advice, no links in body, near-duplicate rejection, and a source-signal requirement for study posts. Up to 4 regen attempts, then a safe template.
5. **Publish** — text-only to X (avoids the **$0.20** link surcharge); any URL is posted as a *reply*. Optional Nostr. History committed back.

## Setup

### 1. Push to GitHub
Create a repo (e.g. `peptide-frontier-bot`) and push these files.

### 2. Get a Gemini API key (free tier covers 1 post/day)
[aistudio.google.com](https://aistudio.google.com) → API key. Add as secret `GEMINI_API_KEY`.

### 3. Set up X API — note the 2026 change
The old free tier was discontinued in **Feb 2026**. New developers use **pay-per-use**: **$0.015** per text post, **$0.20** if the post contains a link. This bot posts text-only, so one post/day ≈ **$5.50/year**.

At [developer.x.com](https://developer.x.com): create a Project + App, buy a small credit balance, set App permissions to **Read and write**, then generate **API Key/Secret** and **Access Token/Secret** (regenerate the access token *after* enabling write). Add as secrets:
`PF_X_API_KEY`, `PF_X_API_SECRET`, `PF_X_ACCESS_TOKEN`, `PF_X_ACCESS_SECRET`.

**Required by X policy:** the @peptidefrontier bio must identify the account as automated, e.g.
`Decoding peptide science — cited, numerate, no hype, nothing to sell. Educational only, not medical advice. Automated 🤖`
(This bot posts original content only — never automated replies — which keeps it inside X's rules without the extra AI-reply-bot approval.)

### 4. (Optional) Nostr
Add `NOSTR_NSEC` and uncomment `basic-nostr` in `requirements.txt`.

### 5. Test before going live
From the **Actions** tab → *Peptide Frontier Daily Post* → **Run workflow** → set **dry_run = true**. Check the logs for the generated post. When happy, let the daily cron run, or run again with dry_run off.

Locally:
```bash
pip install -r requirements.txt
cp .env.example .env    # fill in keys
python content_bot.py --dry-run                 # generate + validate, no posting
python content_bot.py --pillar numeracy --dry-run
```

## Cost summary
| Item | Cost |
|---|---|
| GitHub Actions (public repo) | Free |
| Europe PMC grounding | Free |
| Gemini 2.5 Flash (~1 post/day) | < $0.01/mo |
| X pay-per-use, text-only | ~$5.50/yr |
| **Total** | **~$6/year** |

## Tuning
- Reweight pillars or edit topics in `data/topics.json`.
- Adjust voice/rules in `system_prompt.md`.
- Tighten/loosen guardrails via the constants at the top of `content_bot.py` (`MAX_CHARS`, `DEDUPE_SIMILARITY`, `BANNED`).
