"""NIP-46 bunker signing, against a scripted signer. No network."""

import json
import secrets

import pytest

from pf_autorespond import nip44, nip46
from pf_autorespond.nostr import Event, NostrKey

SIGNER_SK = "0000000000000000000000000000000000000000000000000000000000000003"
SIGNER = NostrKey(SIGNER_SK)
USER_SK = "0000000000000000000000000000000000000000000000000000000000000007"
USER = NostrKey(USER_SK)

URI = f"bunker://{SIGNER.pubkey_hex}?relay=wss://relay.nsec.app&secret=tok123"


# ---------------------------------------------------------------- URI parse
def test_parse_bunker_uri():
    u = nip46.BunkerURI.parse(URI)
    assert u.signer_pubkey == SIGNER.pubkey_hex
    assert u.relays == ["wss://relay.nsec.app"]
    assert u.secret == "tok123"


def test_parse_accepts_npub_form_and_multiple_relays():
    u = nip46.BunkerURI.parse(
        f"bunker://{SIGNER.npub}?relay=wss://a.test&relay=wss://b.test"
    )
    assert u.signer_pubkey == SIGNER.pubkey_hex
    assert u.relays == ["wss://a.test", "wss://b.test"]
    assert u.secret == ""


@pytest.mark.parametrize("bad", [
    "", "not a uri", "https://relay.test",
    f"bunker://{SIGNER.pubkey_hex}",                       # no relay
    "bunker://zzzz?relay=wss://a.test",                    # bad pubkey
])
def test_bad_uris_rejected(bad):
    with pytest.raises(nip46.BunkerError):
        nip46.BunkerURI.parse(bad)


# ------------------------------------------------------------- fake signer
class FakeSignerWS:
    """
    A relay + signer in one. Decrypts each request, answers per `behaviour`.
    """

    def __init__(self, behaviour="ok", use_nip44=True):
        self.behaviour = behaviour
        self.use_nip44 = use_nip44
        self.outbox = []
        self.seen_methods = []
        self._client_pk = None

    def settimeout(self, t): pass
    def close(self): pass

    def _conv(self):
        return nip44.conversation_key(SIGNER_SK, self._client_pk)

    def send(self, raw):
        msg = json.loads(raw)
        if msg[0] == "REQ":
            self._client_pk = msg[2]["#p"][0]
            return
        ev = msg[1]
        self._client_pk = ev["pubkey"]
        if self.use_nip44:
            body = json.loads(nip44.decrypt(ev["content"], self._conv()))
        else:
            body = json.loads(nip46._nip04_decrypt(ev["content"], SIGNER_SK, self._client_pk))
        self.seen_methods.append(body["method"])
        self.outbox.append(self._respond(body))

    def _respond(self, body):
        method, rid = body["method"], body["id"]
        if self.behaviour == "error":
            payload = {"id": rid, "error": "user rejected"}
        elif self.behaviour == "auth_url":
            payload = {"id": rid, "result": "auth_url"}
        elif method == "connect":
            payload = {"id": rid, "result": "ack"}
        elif method == "get_public_key":
            payload = {"id": rid, "result": USER.pubkey_hex}
        elif method == "ping":
            payload = {"id": rid, "result": "pong"}
        elif method == "sign_event":
            unsigned = json.loads(body["params"][0])
            ev = Event(kind=unsigned["kind"], content=unsigned["content"],
                       tags=unsigned["tags"], created_at=unsigned["created_at"])
            if self.behaviour == "tamper":
                ev.content = "something you never approved"
            ev.finalize(USER)
            payload = {"id": rid, "result": json.dumps(ev.to_dict())}
        else:
            payload = {"id": rid, "error": f"unknown method {method}"}

        plain = json.dumps(payload, separators=(",", ":"))
        content = (
            nip44.encrypt(plain, self._conv()) if self.use_nip44
            else nip46._nip04_encrypt(plain, SIGNER_SK, self._client_pk)
        )
        out = Event(kind=nip46.KIND_RPC, content=content,
                    tags=[["p", self._client_pk]]).finalize(SIGNER)
        return json.dumps(["EVENT", "bunker", out.to_dict()])

    def recv(self):
        if not self.outbox:
            raise RuntimeError("nothing to receive")
        return self.outbox.pop(0)


def bunker(monkeypatch, behaviour="ok", use_nip44=True, timeout=5):
    import sys
    import types

    ws = FakeSignerWS(behaviour, use_nip44)
    mod = types.ModuleType("websocket")
    mod.create_connection = lambda url, timeout=0: ws
    monkeypatch.setitem(sys.modules, "websocket", mod)
    b = nip46.BunkerSigner(uri=nip46.BunkerURI.parse(URI), timeout=timeout)
    return b, ws


# -------------------------------------------------------------- happy path
def test_connect_returns_the_user_pubkey(monkeypatch):
    b, ws = bunker(monkeypatch)
    assert b.connect() == USER.pubkey_hex
    assert b.npub == USER.npub
    assert ws.seen_methods == ["connect", "get_public_key"]


def test_sign_event_round_trip(monkeypatch):
    from coincurve import PublicKeyXOnly

    b, _ = bunker(monkeypatch)
    ev = b.sign_event(Event(kind=1, content="No human PK data exists."))
    assert ev.pubkey == USER.pubkey_hex and len(ev.sig) == 128
    assert PublicKeyXOnly(bytes.fromhex(ev.pubkey)).verify(
        bytes.fromhex(ev.sig), bytes.fromhex(ev.id)
    )


def test_nip04_signer_still_works(monkeypatch):
    """Older signers answer in NIP-04; a silent decrypt failure looks like a hang."""
    b, ws = bunker(monkeypatch, use_nip44=False)
    b._use_nip44 = False
    assert b.connect() == USER.pubkey_hex


def test_ping(monkeypatch):
    b, _ = bunker(monkeypatch)
    assert b.ping() is True


# ------------------------------------------------------------- the security
def test_a_tampered_signature_is_rejected(monkeypatch):
    """
    The signer returns a VALID signature over content we never sent. Trusting
    the response blindly would publish something the user never approved.
    """
    b, _ = bunker(monkeypatch, behaviour="tamper")
    with pytest.raises(nip46.BunkerError, match="doesn't match the content"):
        b.sign_event(Event(kind=1, content="what we actually wrote"))


def test_signer_rejection_surfaces(monkeypatch):
    b, _ = bunker(monkeypatch, behaviour="error")
    with pytest.raises(nip46.BunkerError, match="user rejected"):
        b.connect()


def test_auth_url_gives_an_actionable_message(monkeypatch):
    b, _ = bunker(monkeypatch, behaviour="auth_url")
    with pytest.raises(nip46.BunkerError, match="preauthoris"):
        b.connect()


def test_bunker_never_exposes_a_raw_sign_method():
    """Raw signing capability must stay in the signer, not in this process."""
    assert not hasattr(nip46.BunkerSigner, "sign")
    assert hasattr(nip46.BunkerSigner, "sign_event")


def test_repr_leaks_nothing(monkeypatch):
    b, _ = bunker(monkeypatch)
    b.connect()
    r = repr(b)
    assert SIGNER_SK not in r and USER_SK not in r
    assert b._client._raw.hex() not in r      # ephemeral client key stays hidden


def test_client_key_is_ephemeral_and_unique():
    a = nip46.BunkerSigner(uri=nip46.BunkerURI.parse(URI))
    c = nip46.BunkerSigner(uri=nip46.BunkerURI.parse(URI))
    assert a._client.pubkey_hex != c._client.pubkey_hex


# ------------------------------------------------------------- selection
def test_bunker_wins_over_a_local_nsec(monkeypatch):
    """The safer option must never lose to the convenient one by accident."""
    monkeypatch.setenv("NOSTR_BUNKER_URI", URI)
    monkeypatch.setenv("NOSTR_NSEC", USER_SK)
    assert isinstance(nip46.get_signer(), nip46.BunkerSigner)


def test_falls_back_to_local_nsec(monkeypatch):
    monkeypatch.delenv("NOSTR_BUNKER_URI", raising=False)
    monkeypatch.setenv("NOSTR_NSEC", USER_SK)
    assert isinstance(nip46.get_signer(), NostrKey)


def test_no_signer_configured(monkeypatch):
    monkeypatch.delenv("NOSTR_BUNKER_URI", raising=False)
    monkeypatch.delenv("NOSTR_NSEC", raising=False)
    assert nip46.get_signer() is None


def test_a_broken_bunker_uri_does_not_silently_fall_through(monkeypatch, caplog):
    monkeypatch.setenv("NOSTR_BUNKER_URI", "bunker://garbage")
    monkeypatch.delenv("NOSTR_NSEC", raising=False)
    assert nip46.get_signer() is None
    assert any("unusable" in r.message for r in caplog.records)


def test_timeout_message_names_the_relay(monkeypatch):
    import sys
    import types

    class Silent(FakeSignerWS):
        def send(self, raw): pass
        def recv(self):
            raise TimeoutError("timed out")

    mod = types.ModuleType("websocket")
    mod.create_connection = lambda url, timeout=0: Silent()
    monkeypatch.setitem(sys.modules, "websocket", mod)
    b = nip46.BunkerSigner(uri=nip46.BunkerURI.parse(URI), timeout=1)
    with pytest.raises(nip46.BunkerError, match="connection lost|no response"):
        b.connect()
