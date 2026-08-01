"""
Nostr. The crypto is coincurve (libsecp256k1 bindings), not hand-rolled, so
these tests verify we're *using* it correctly: canonical NIP-01 serialisation,
correct id, a signature that actually verifies, and no key leakage.
"""

import hashlib
import json

import pytest

from pf_autorespond import nostr

SK_HEX = "0000000000000000000000000000000000000000000000000000000000000003"
# BIP-340 test vector 0 public key
PK_HEX = "f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"


# --------------------------------------------------------------------- keys
def test_pubkey_matches_bip340_vector():
    assert nostr.NostrKey(SK_HEX).pubkey_hex == PK_HEX


def test_nsec_and_hex_give_the_same_key():
    nsec = nostr.bech32_encode("nsec", bytes.fromhex(SK_HEX))
    assert nsec.startswith("nsec1")
    assert nostr.NostrKey(nsec).pubkey_hex == PK_HEX


def test_bech32_round_trip_and_checksum():
    npub = nostr.NostrKey(SK_HEX).npub
    hrp, raw = nostr.bech32_decode(npub)
    assert hrp == "npub" and raw.hex() == PK_HEX
    with pytest.raises(ValueError, match="checksum"):
        nostr.bech32_decode(npub[:-1] + ("q" if npub[-1] != "q" else "p"))


def test_bad_keys_are_rejected():
    for bad in ("", "nope", "ab" * 16, nostr.bech32_encode("npub", bytes.fromhex(SK_HEX))):
        with pytest.raises(ValueError):
            nostr.NostrKey(bad)


def test_secret_never_appears_in_repr_or_str():
    """A stack trace in a public Actions log must not leak the nsec."""
    k = nostr.NostrKey(SK_HEX)
    for rendered in (repr(k), str(k), f"{k}"):
        assert SK_HEX not in rendered
        assert "REDACTED" in rendered
    assert SK_HEX not in json.dumps({"k": repr(k)})


# ------------------------------------------------------------------- events
def test_event_id_is_sha256_of_canonical_serialisation():
    k = nostr.NostrKey(SK_HEX)
    ev = nostr.note("hello").finalize(k)
    expected = hashlib.sha256(ev.serialize().encode()).hexdigest()
    assert ev.id == expected and len(ev.id) == 64


def test_serialisation_is_canonical_nip01():
    k = nostr.NostrKey(SK_HEX)
    ev = nostr.note("hi").finalize(k)
    parsed = json.loads(ev.serialize())
    assert parsed[0] == 0 and parsed[1] == PK_HEX and parsed[3] == 1
    # no whitespace: separators are part of the spec, and any deviation
    # changes the id, which makes relays reject the event
    assert ", " not in ev.serialize() and '": ' not in ev.serialize()


def test_signature_verifies():
    from coincurve import PublicKeyXOnly

    k = nostr.NostrKey(SK_HEX)
    ev = nostr.note("No human PK data exists for this compound.").finalize(k)
    assert len(ev.sig) == 128
    assert PublicKeyXOnly(bytes.fromhex(ev.pubkey)).verify(
        bytes.fromhex(ev.sig), bytes.fromhex(ev.id)
    )


def test_tampering_breaks_the_signature():
    from coincurve import PublicKeyXOnly

    k = nostr.NostrKey(SK_HEX)
    ev = nostr.note("original").finalize(k)
    ev.content = "tampered"
    new_digest = hashlib.sha256(ev.serialize().encode()).digest()
    assert not PublicKeyXOnly(bytes.fromhex(ev.pubkey)).verify(
        bytes.fromhex(ev.sig), new_digest
    )


def test_unicode_survives_serialisation():
    k = nostr.NostrKey(SK_HEX)
    ev = nostr.note("semaglutide 2.4 mg — µg dosing, naïve cohort").finalize(k)
    assert "µg" in json.loads(ev.serialize())[5]


# ------------------------------------------------------------- NIP-10 / 42
def test_reply_gets_a_root_marker():
    ev = nostr.note("reply", reply_to="a" * 64)
    assert ev.tags == [["e", "a" * 64, "", "root"]]


def test_deep_reply_marks_root_and_parent_separately():
    ev = nostr.note("reply", reply_to="b" * 64, root="a" * 64)
    assert ev.tags[0] == ["e", "a" * 64, "", "root"]
    assert ev.tags[1] == ["e", "b" * 64, "", "reply"]


def test_mentions_become_p_tags():
    ev = nostr.note("hi", mention_pubkeys=[PK_HEX, ""])
    assert ev.tags == [["p", PK_HEX]]


def test_auth_event_shape():
    ev = nostr.auth_event("wss://relay.test", "chal123")
    assert ev.kind == nostr.KIND_AUTH
    assert ["relay", "wss://relay.test"] in ev.tags
    assert ["challenge", "chal123"] in ev.tags


# -------------------------------------------------------------- relay pool
class FakeWS:
    """Scripted relay. `script` is a list of frames to hand back on recv()."""

    def __init__(self, script, record):
        self.script = list(script)
        self.record = record

    def settimeout(self, t): pass

    def send(self, raw):
        self.record.append(json.loads(raw))

    def recv(self):
        if not self.script:
            raise RuntimeError("no more frames")
        return json.dumps(self.script.pop(0))

    def close(self): pass


def pool_with(monkeypatch, script, record):
    import sys
    import types

    mod = types.ModuleType("websocket")
    mod.create_connection = lambda url, timeout=0: FakeWS(script, record)
    monkeypatch.setitem(sys.modules, "websocket", mod)
    return nostr.RelayPool(["wss://relay.test"], nostr.NostrKey(SK_HEX), timeout=2)


def test_publish_success(monkeypatch):
    sent = []
    k = nostr.NostrKey(SK_HEX)
    ev = nostr.note("hello").finalize(k)
    pool = pool_with(monkeypatch, [["OK", ev.id, True, ""]], sent)
    results = pool.publish(ev)
    assert results[0].ok and not results[0].authed
    assert sent[0][0] == "EVENT"


def test_relay_rejection_is_reported(monkeypatch):
    sent = []
    k = nostr.NostrKey(SK_HEX)
    ev = nostr.note("hello").finalize(k)
    pool = pool_with(monkeypatch, [["OK", ev.id, False, "blocked: pow required"]], sent)
    r = pool.publish(ev)[0]
    assert not r.ok and "pow" in r.message


def test_nip42_auth_challenge_is_answered_and_event_retried(monkeypatch):
    """An unauthenticated bridge fails silently — this is the one to get right."""
    sent = []
    k = nostr.NostrKey(SK_HEX)
    ev = nostr.note("hello").finalize(k)
    pool = pool_with(
        monkeypatch,
        [["AUTH", "challenge-xyz"], ["OK", ev.id, True, ""]],
        sent,
    )
    r = pool.publish(ev)[0]
    assert r.ok and r.authed
    kinds = [m[0] for m in sent]
    assert kinds == ["EVENT", "AUTH", "EVENT"]      # retried after authenticating
    auth_ev = sent[1][1]
    assert auth_ev["kind"] == nostr.KIND_AUTH
    assert ["challenge", "challenge-xyz"] in auth_ev["tags"]


def test_a_dead_relay_does_not_raise(monkeypatch):
    import sys
    import types

    mod = types.ModuleType("websocket")

    def boom(url, timeout=0):
        raise OSError("connection refused")

    mod.create_connection = boom
    monkeypatch.setitem(sys.modules, "websocket", mod)
    pool = nostr.RelayPool(["wss://dead.test"], nostr.NostrKey(SK_HEX), timeout=1)
    r = pool.publish(nostr.note("x").finalize(nostr.NostrKey(SK_HEX)))[0]
    assert not r.ok and "OSError" in r.message


# ------------------------------------------------------------------- mapping
def test_event_map_round_trips(tmp_path):
    m = nostr.EventMap(tmp_path / "map.json")
    m.put("x123", "e456")
    m.put("", "ignored")
    m.save()
    assert nostr.EventMap(tmp_path / "map.json").get("x123") == "e456"
    assert nostr.EventMap(tmp_path / "map.json").get("nope") == ""


def test_content_matching_tolerates_bridge_reformatting():
    x_text = ("BPC-157's therapeutic potential is often overstated. Many claims trace "
              "back to preclinical studies, primarily in rodent models of injury.")
    notes = [
        {"id": "wrong", "content": "Semaglutide 2.4mg reduced cardiovascular events."},
        {"id": "right", "content": x_text + "\n\nhttps://x.com/PeptideFrontier/status/1"},
    ]
    assert nostr.match_by_content(notes, x_text) == "right"


def test_content_matching_refuses_a_weak_match():
    """Threading onto the wrong post is worse than not threading."""
    notes = [{"id": "a", "content": "completely unrelated text about something else"}]
    assert nostr.match_by_content(notes, "BPC-157 rodent models of injury") == ""


def test_key_from_env_is_quiet_when_unset(monkeypatch):
    monkeypatch.delenv("NOSTR_NSEC", raising=False)
    assert nostr.key_from_env() is None
    monkeypatch.setenv("NOSTR_NSEC", "garbage")
    assert nostr.key_from_env() is None       # logs, does not raise, does not echo


# --------------------------------------------------- read-only key handling
def test_pubkey_accepts_npub_and_hex():
    npub = nostr.NostrKey(SK_HEX).npub
    assert nostr.pubkey_from(npub) == PK_HEX
    assert nostr.pubkey_from(PK_HEX) == PK_HEX
    assert nostr.pubkey_from("  " + npub.upper().lower() + " ") == PK_HEX


def test_passing_an_nsec_where_an_npub_belongs_is_caught_loudly():
    """Reads never need the secret. Say so instead of silently accepting it."""
    nsec = nostr.bech32_encode("nsec", bytes.fromhex(SK_HEX))
    with pytest.raises(ValueError, match="SECRET"):
        nostr.pubkey_from(nsec)


def test_pubkey_rejects_junk():
    for bad in ("", "hello", "ab" * 10, "npub1notavalidchecksumatall"):
        with pytest.raises(ValueError):
            nostr.pubkey_from(bad)


# ----------------------------------------------------- thinking-model guard
def test_empty_completion_is_an_error_not_a_silent_skip(monkeypatch):
    """
    A thinking model that spends its whole budget reasoning returns "". Treated
    as a normal answer that would look like 'the model had nothing to say'.
    """
    from pf_autorespond import llm

    monkeypatch.setattr(llm, "_http_json",
                        lambda *a, **k: {"choices": [{"message": {"content": ""}}]})
    cfg = llm.LLMConfig(provider="openai", api_key="k", model="gemini-2.5-flash")
    with pytest.raises(llm.LLMError, match="empty completion"):
        llm.generate("sys", "user", cfg)


def test_reasoning_effort_is_forwarded_when_set(monkeypatch):
    from pf_autorespond import llm

    seen = {}

    def fake(url, headers, body, timeout):
        seen.update(body)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(llm, "_http_json", fake)
    cfg = llm.LLMConfig(provider="openai", api_key="k", reasoning_effort="none")
    llm.generate("sys", "user", cfg)
    assert seen["reasoning_effort"] == "none"
