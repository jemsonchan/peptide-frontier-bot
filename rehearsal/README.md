# Rehearsal harness

```bash
PYTHONPATH=src:. python rehearsal/run.py
```

Runs the real pipeline — engine, selector, safety gates, ledger, queue,
publisher, Nostr signing — against fixtures. No credentials, no network, no
spend. Two substitutions only:

| Substituted | Why |
|---|---|
| X API | needs your keys; a fake client serves fixtures and records writes |
| LLM | needs an API key; a scripted responder stands in for the model |

`fixtures.py` holds your real timeline plus the traps this niche actually
produces. The script is deliberately arranged so that **not everything works
first time**: one draft invents a citation and must be killed by the critic,
one smuggles a URL and must be killed by the gate. A rehearsal where the happy
path always wins is a demo, not a test.

Edit `DRAFTS` in `run.py` to try different copy against the real gates.
