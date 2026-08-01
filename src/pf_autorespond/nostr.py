"""
Nostr publishing (NIP-01, NIP-10 threading, NIP-19 keys, NIP-42 relay auth).

Why this exists: Nostr has no per-action cost and no ranking algorithm to
please. The economics that force restraint on X — $0.015 a reply, a 13x
penalty on links, a ranker that punishes anything people mute — simply do not
apply. So the same approved draft can go out here unconditionally, and links
are fine.

Signing uses **coincurve**, which is a binding to libsecp256k1 — the same C
library Bitcoin Core uses. No hand-rolled crypto. Verified against the BIP-340
test vectors in tests/test_nostr.py.

Key handling: the nsec is a bearer secret with no revocation and no recovery.
It is read from the environment only, never written to the queue, the ledger,
the logs, or any report. `NostrKey.__repr__` is deliberately redacted, because
the single most likely way to leak it is a stack trace in a public Actions log.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger(__name__)

KIND_NOTE = 1
KIND_AUTH = 22242

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


# --------------------------------------------------------------- NIP-19 keys
def _bech32_polymod(values: Iterable[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data: Iterable[int], frm: int, to: int, pad: bool = True) -> list[int]:
    acc, bits, out = 0, 0, []
    maxv = (1 << to) - 1
    for value in data:
        acc = (acc << frm) | value
        bits += frm
        while bits >= to:
            bits -= to
            out.append((acc >> bits) & maxv)
    if pad and bits:
        out.append((acc << (to - bits)) & maxv)
    return out


def bech32_decode(s: str) -> tuple[str, bytes]:
    s = s.strip()
    if s != s.lower() and s != s.upper():
        raise ValueError("mixed-case bech32")
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s):
        raise ValueError("malformed bech32")
    hrp, data_part = s[:pos], s[pos + 1:]
    try:
        data = [BECH32_CHARSET.index(c) for c in data_part]
    except ValueError as e:
        raise ValueError("invalid bech32 character") from e
    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        raise ValueError("bad bech32 checksum")
    return hrp, bytes(_convertbits(data[:-6], 5, 8, False))


def bech32_encode(hrp: str, payload: bytes) -> str:
    data = _convertbits(payload, 8, 5)
    chk = _bech32_polymod(_bech32_hrp_expand(hrp) + data + [0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(chk >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(BECH32_CHARSET[d] for d in data + checksum)


class NostrKey:
    """Accepts nsec1... or 64-char hex. Never logs itself."""

    def __init__(self, secret: str):
        s = (secret or "").strip()
        if not s:
            raise ValueError("empty nostr secret key")
        if s.startswith("nsec"):
            hrp, raw = bech32_decode(s)
            if hrp != "nsec":
                raise ValueError(f"expected nsec, got {hrp}")
        else:
            raw = bytes.fromhex(s)
        if len(raw) != 32:
            raise ValueError("secret key must be 32 bytes")
        self._raw = raw

    @property
    def pubkey_hex(self) -> str:
        from coincurve import PrivateKey

        # x-only pubkey: drop the compressed-form parity byte (BIP-340).
        return PrivateKey(self._raw).public_key.format(compressed=True)[1:].hex()

    @property
    def npub(self) -> str:
        return bech32_encode("npub", bytes.fromhex(self.pubkey_hex))

    def sign(self, digest: bytes) -> str:
        from coincurve import PrivateKey

        return PrivateKey(self._raw).sign_schnorr(digest).hex()

    def sign_event(self, event: "Event") -> "Event":
        """Same interface as BunkerSigner, so callers don't care which they hold."""
        return event.finalize(self)

    def __repr__(self) -> str:            # a stack trace in a public Actions
        return f"<NostrKey {self.npub[:12]}… secret=REDACTED>"   # log must not leak this

    __str__ = __repr__


# ------------------------------------------------------------------- events
@dataclass
class Event:
    kind: int
    content: str
    tags: list[list[str]] = field(default_factory=list)
    created_at: int = 0
    pubkey: str = ""
    id: str = ""
    sig: str = ""

    def serialize(self) -> str:
        # NIP-01 canonical form. Field order and separators are part of the
        # spec -- any deviation changes the id and the event is rejected.
        return json.dumps(
            [0, self.pubkey, self.created_at, self.kind, self.tags, self.content],
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def finalize(self, key: NostrKey) -> "Event":
        self.pubkey = key.pubkey_hex
        self.created_at = self.created_at or int(time.time())
        digest = hashlib.sha256(self.serialize().encode()).digest()
        self.id = digest.hex()
        self.sig = key.sign(digest)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "pubkey": self.pubkey, "created_at": self.created_at,
            "kind": self.kind, "tags": self.tags, "content": self.content, "sig": self.sig,
        }


def note(
    content: str,
    *,
    reply_to: str = "",
    root: str = "",
    mention_pubkeys: Iterable[str] = (),
    extra_tags: Iterable[list[str]] = (),
) -> Event:
    """
    A kind-1 note, threaded per NIP-10 if a parent is given.

    NIP-10 marked tags: the root of the thread and the direct parent are tagged
    separately, so clients can render the thread instead of a flat pile.
    """
    tags: list[list[str]] = []
    if root and reply_to and root != reply_to:
        tags.append(["e", root, "", "root"])
        tags.append(["e", reply_to, "", "reply"])
    elif reply_to:
        tags.append(["e", reply_to, "", "root"])
    for pk in mention_pubkeys:
        if pk:
            tags.append(["p", pk])
    tags.extend(list(extra_tags))
    return Event(kind=KIND_NOTE, content=content, tags=tags)


def auth_event(relay_url: str, challenge: str) -> Event:
    """NIP-42 authentication event."""
    return Event(
        kind=KIND_AUTH,
        content="",
        tags=[["relay", relay_url], ["challenge", challenge]],
    )


# ------------------------------------------------------------------- relays
@dataclass
class PublishResult:
    relay: str
    ok: bool
    message: str = ""
    authed: bool = False


class RelayPool:
    """
    Synchronous, one connection per relay, per publish. That's the right shape
    for a cron job: no event loop to keep alive, no reconnection state to get
    wrong, and a hung relay can only cost you its timeout.

    Handles NIP-42: relays increasingly require AUTH before accepting writes,
    and an unauthenticated bridge fails *silently* — the relay simply drops the
    event. That's the failure mode most likely to bite a bridge that used to
    work, so it's handled rather than ignored.
    """

    def __init__(self, relays: list[str], key: NostrKey, timeout: int = 12):
        self.relays = relays
        self.key = key
        self.timeout = timeout

    def publish(self, event: Event) -> list[PublishResult]:
        return [self._publish_one(r, event) for r in self.relays]

    def _publish_one(self, url: str, event: Event) -> PublishResult:
        try:
            import websocket  # websocket-client
        except ImportError:
            return PublishResult(url, False, "websocket-client not installed")

        ws = None
        authed = False
        try:
            ws = websocket.create_connection(url, timeout=self.timeout)
            ws.settimeout(self.timeout)
            ws.send(json.dumps(["EVENT", event.to_dict()]))

            deadline = time.time() + self.timeout
            while time.time() < deadline:
                try:
                    raw = ws.recv()
                except Exception:
                    break
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue

                if msg and msg[0] == "OK" and len(msg) >= 3 and msg[1] == event.id:
                    return PublishResult(url, bool(msg[2]),
                                         (msg[3] if len(msg) > 3 else ""), authed)

                if msg and msg[0] == "AUTH" and len(msg) >= 2 and not authed:
                    # NIP-42 challenge. Sign it, then retry the event.
                    ev = self.key.sign_event(auth_event(url, msg[1]))
                    ws.send(json.dumps(["AUTH", ev.to_dict()]))
                    authed = True
                    ws.send(json.dumps(["EVENT", event.to_dict()]))
                    deadline = time.time() + self.timeout

                if msg and msg[0] == "NOTICE":
                    log.info("%s notice: %s", url, str(msg[1])[:160])

            return PublishResult(url, False, "timeout waiting for OK", authed)
        except Exception as e:  # noqa: BLE001 - one bad relay must not stop the rest
            return PublishResult(url, False, f"{type(e).__name__}: {e}"[:160], authed)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass


DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.nostr.band",
    "wss://relay.primal.net",
    "wss://nostr.wine",
]


def pubkey_from(value: str) -> str:
    """
    Accept npub1… or 64-char hex and return hex.

    Reading your own notes is a public operation — it needs the PUBLIC key and
    nothing else. Requiring an nsec for a read would be asking for a secret to
    do a job that doesn't need one, which is how secrets end up in places they
    shouldn't be.
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("empty public key")
    if v.startswith("npub"):
        hrp, raw = bech32_decode(v)
        if hrp != "npub":
            raise ValueError(f"expected npub, got {hrp}")
    elif v.startswith("nsec"):
        raise ValueError("that's a SECRET key — pass the npub, reads never need the nsec")
    else:
        raw = bytes.fromhex(v)
    if len(raw) != 32:
        raise ValueError("public key must be 32 bytes")
    return raw.hex()


def check_relays(relays: list[str], timeout: int = 8) -> list[dict[str, Any]]:
    """Can we open a socket and speak NIP-01? Reads nothing, needs no key."""
    try:
        import websocket
    except ImportError:
        return [{"relay": r, "ok": False, "detail": "websocket-client not installed"}
                for r in relays]

    out = []
    for url in relays:
        t0 = time.time()
        ws = None
        try:
            ws = websocket.create_connection(url, timeout=timeout)
            ws.settimeout(timeout)
            ws.send(json.dumps(["REQ", "probe", {"kinds": [KIND_NOTE], "limit": 1}]))
            frame = json.loads(ws.recv())[0]
            out.append({"relay": url, "ok": True,
                        "ms": int((time.time() - t0) * 1000), "detail": f"first frame {frame}"})
        except Exception as e:  # noqa: BLE001
            out.append({"relay": url, "ok": False,
                        "ms": int((time.time() - t0) * 1000),
                        "detail": f"{type(e).__name__}: {str(e)[:70]}"})
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
    return out


def key_from_env() -> NostrKey | None:
    secret = os.getenv("NOSTR_NSEC", "")
    if not secret:
        return None
    try:
        return NostrKey(secret)
    except ValueError as e:
        # Never echo the value back, even on a parse failure.
        log.error("NOSTR_NSEC is set but unusable: %s", e)
        return None


# ------------------------------------------------------------ thread mapping
class EventMap:
    """
    X post id  ->  Nostr event id.

    Needed to thread replies. Your existing bridge publishes the original
    posts, so this tool doesn't know their event ids until it either publishes
    something itself or backfills from relays.
    """

    def __init__(self, path: str | os.PathLike[str]):
        from pathlib import Path

        self.path = Path(path)
        self.data: dict[str, str] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8")) or {}
            except ValueError:
                self.data = {}

    def get(self, x_id: str) -> str:
        return self.data.get(x_id, "")

    def put(self, x_id: str, event_id: str) -> None:
        if x_id and event_id:
            self.data[x_id] = event_id

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")


def fetch_own_notes(
    relays: list[str], pubkey_hex: str, limit: int = 50, timeout: int = 12
) -> list[dict[str, Any]]:
    """
    Pull your own recent kind-1 notes off relays (NIP-01 REQ/EOSE).

    Used by `nostr backfill` to learn the event ids your existing bridge
    created, so replies can thread onto them instead of floating loose.
    """
    try:
        import websocket
    except ImportError:
        return []

    seen: dict[str, dict[str, Any]] = {}
    sub = "pf" + hashlib.sha256(pubkey_hex.encode()).hexdigest()[:8]
    req = json.dumps(["REQ", sub, {"authors": [pubkey_hex], "kinds": [KIND_NOTE], "limit": limit}])

    for url in relays:
        ws = None
        try:
            ws = websocket.create_connection(url, timeout=timeout)
            ws.settimeout(timeout)
            ws.send(req)
            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = ws.recv()
                if not raw:
                    continue
                msg = json.loads(raw)
                if msg[0] == "EVENT" and len(msg) >= 3:
                    ev = msg[2]
                    seen[ev["id"]] = ev
                elif msg[0] in ("EOSE", "CLOSED"):
                    break
        except Exception as e:  # noqa: BLE001
            log.info("backfill from %s failed: %s", url, e)
        finally:
            if ws is not None:
                try:
                    ws.send(json.dumps(["CLOSE", sub]))
                    ws.close()
                except Exception:
                    pass
    return sorted(seen.values(), key=lambda e: e.get("created_at", 0), reverse=True)


def match_by_content(notes: list[dict[str, Any]], text: str, threshold: float = 0.82) -> str:
    """
    Find the Nostr event that corresponds to an X post, by content similarity.

    Bridges reformat: they append a link, truncate, or strip an emoji. Exact
    matching therefore fails on real data, so this is fuzzy and conservative --
    a wrong match threads a reply onto the wrong post, which is worse than not
    threading at all.
    """
    import difflib

    target = " ".join(text.split()).lower()
    best_id, best_score = "", 0.0
    for ev in notes:
        cand = " ".join((ev.get("content") or "").split()).lower()
        if not cand:
            continue
        score = difflib.SequenceMatcher(None, target[:240], cand[:240]).ratio()
        if score > best_score:
            best_id, best_score = ev.get("id", ""), score
    return best_id if best_score >= threshold else ""
