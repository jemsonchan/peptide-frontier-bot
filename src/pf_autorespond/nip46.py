"""
NIP-46 remote signing ("bunker" / Nostr Connect).

The point: GitHub never holds your nsec. Your key stays in Amber, nsec.app, or
whatever signer you already use. This tool holds a *connection string*, which
you can revoke from the signer at any time without losing your identity.

That difference matters more on Nostr than anywhere else. An X API token is
revocable; a leaked nsec is not. There is no rotation — a compromised nsec
means abandoning the npub and every follower attached to it. So the one secret
that cannot be rotated is the one secret we refuse to store.

How it works: the tool generates a throwaway local keypair, and talks to your
signer over a relay using kind-24133 events with NIP-44 encrypted bodies. It
sends an unsigned event; your signer decides whether to sign it. On a phone
signer you get a prompt; on nsec.app you pre-authorise a permission set.

    bunker://<signer-pubkey-hex>?relay=wss://relay.nsec.app&secret=<token>

Transport falls back to NIP-04 if a signer answers in the older format --
Amber and nsec.app both speak NIP-44 now, but older builds are still out there
and a silent decrypt failure would look like a hang.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import nip44
from .nostr import Event, NostrKey

log = logging.getLogger(__name__)

KIND_RPC = 24133
DEFAULT_PERMS = "sign_event:1,sign_event:22242,get_public_key"


class BunkerError(RuntimeError):
    pass


# ------------------------------------------------------------------- NIP-04
def _nip04_encrypt(plaintext: str, priv_hex: str, pub_hex: str) -> str:
    import base64

    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = _shared_x(priv_hex, pub_hex)
    iv = secrets.token_bytes(16)
    padder = sym_padding.PKCS7(128).padder()
    data = padder.update(plaintext.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(data) + enc.finalize()
    return f"{base64.b64encode(ct).decode()}?iv={base64.b64encode(iv).decode()}"


def _nip04_decrypt(payload: str, priv_hex: str, pub_hex: str) -> str:
    import base64

    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if "?iv=" not in payload:
        raise BunkerError("not a NIP-04 payload")
    ct_b64, iv_b64 = payload.split("?iv=", 1)
    key = _shared_x(priv_hex, pub_hex)
    dec = Cipher(algorithms.AES(key), modes.CBC(base64.b64decode(iv_b64))).decryptor()
    data = dec.update(base64.b64decode(ct_b64)) + dec.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return (unpadder.update(data) + unpadder.finalize()).decode()


def _shared_x(priv_hex: str, pub_hex: str) -> bytes:
    from coincurve import PrivateKey, PublicKey

    priv = PrivateKey(bytes.fromhex(priv_hex))
    pub = PublicKey(b"\x02" + bytes.fromhex(pub_hex))
    return pub.multiply(priv.secret).format(compressed=False)[1:33]


# ------------------------------------------------------------------- bunker
@dataclass
class BunkerURI:
    signer_pubkey: str
    relays: list[str]
    secret: str = ""

    @classmethod
    def parse(cls, uri: str) -> "BunkerURI":
        u = (uri or "").strip()
        if not u.startswith("bunker://"):
            raise BunkerError("expected a bunker:// URI (get it from your signer)")
        parsed = urlparse(u)
        pk = (parsed.netloc or parsed.path.lstrip("/")).strip()
        if len(pk) != 64:
            try:
                from .nostr import pubkey_from

                pk = pubkey_from(pk)
            except Exception as e:
                raise BunkerError(f"bad signer pubkey in bunker URI: {e}") from e
        q = parse_qs(parsed.query)
        relays = [r for r in q.get("relay", []) if r]
        if not relays:
            raise BunkerError("bunker URI has no ?relay= parameter")
        return cls(signer_pubkey=pk, relays=relays, secret=(q.get("secret") or [""])[0])


@dataclass
class BunkerSigner:
    """
    Drop-in replacement for NostrKey: exposes `pubkey_hex` and `sign_event`.

    Deliberately does NOT expose a `sign(digest)` method. The whole point is
    that raw signing capability lives in your signer, not in this process.
    """

    uri: BunkerURI
    timeout: int = 60
    _client: NostrKey = field(default=None, repr=False)
    _ws: Any = field(default=None, repr=False)
    _user_pubkey: str = ""
    _use_nip44: bool = True

    def __post_init__(self):
        if self._client is None:
            # Ephemeral, per-process. Never persisted: it authenticates this
            # tool to your signer, and nothing else.
            self._client = NostrKey(secrets.token_bytes(32).hex())

    @classmethod
    def from_env(cls) -> "BunkerSigner | None":
        uri = os.getenv("NOSTR_BUNKER_URI", "")
        if not uri:
            return None
        try:
            return cls(uri=BunkerURI.parse(uri))
        except BunkerError as e:
            log.error("NOSTR_BUNKER_URI unusable: %s", e)
            return None

    def __repr__(self) -> str:
        return f"<BunkerSigner signer={self.uri.signer_pubkey[:12]}… user={self._user_pubkey[:12] or '?'}…>"

    # ------------------------------------------------------------ transport
    def _conv_key(self) -> bytes:
        return nip44.conversation_key(self._client._raw.hex(), self.uri.signer_pubkey)

    def _wrap(self, body: dict) -> Event:
        plain = json.dumps(body, separators=(",", ":"))
        if self._use_nip44:
            content = nip44.encrypt(plain, self._conv_key())
        else:
            content = _nip04_encrypt(plain, self._client._raw.hex(), self.uri.signer_pubkey)
        ev = Event(kind=KIND_RPC, content=content, tags=[["p", self.uri.signer_pubkey]])
        return ev.finalize(self._client)

    def _unwrap(self, content: str) -> dict:
        for use44 in (self._use_nip44, not self._use_nip44):
            try:
                plain = (
                    nip44.decrypt(content, self._conv_key())
                    if use44
                    else _nip04_decrypt(content, self._client._raw.hex(), self.uri.signer_pubkey)
                )
                self._use_nip44 = use44
                return json.loads(plain)
            except Exception:
                continue
        raise BunkerError("could not decrypt signer response (NIP-44 and NIP-04 both failed)")

    def _open(self):
        if self._ws is not None:
            return
        try:
            import websocket
        except ImportError as e:
            raise BunkerError("websocket-client not installed") from e
        last = None
        for relay in self.uri.relays:
            try:
                ws = websocket.create_connection(relay, timeout=self.timeout)
                ws.settimeout(self.timeout)
                ws.send(json.dumps([
                    "REQ", "bunker",
                    {"kinds": [KIND_RPC], "#p": [self._client.pubkey_hex], "limit": 0},
                ]))
                self._ws = ws
                return
            except Exception as e:  # noqa: BLE001
                last = e
        raise BunkerError(f"no bunker relay reachable: {last}")

    def _rpc(self, method: str, params: list[str]) -> str:
        self._open()
        req_id = secrets.token_hex(8)
        self._ws.send(json.dumps(["EVENT", self._wrap(
            {"id": req_id, "method": method, "params": params}
        ).to_dict()]))

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                raw = self._ws.recv()
            except Exception as e:  # noqa: BLE001
                raise BunkerError(f"bunker connection lost: {e}") from e
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not msg or msg[0] != "EVENT" or len(msg) < 3:
                continue
            body = self._unwrap(msg[2].get("content", ""))
            if body.get("id") != req_id:
                continue
            if body.get("error"):
                raise BunkerError(f"{method}: {body['error']}")
            result = body.get("result", "")
            if result == "auth_url":
                # Signer wants browser confirmation before it will answer.
                raise BunkerError(
                    "signer requires interactive approval (auth_url). Approve the "
                    "connection in your signer once, with permissions preauthorised, "
                    "then re-run."
                )
            return result
        raise BunkerError(
            f"{method}: no response in {self.timeout}s — is your signer online "
            f"and connected to {', '.join(self.uri.relays)}?"
        )

    # --------------------------------------------------------------- public
    def connect(self) -> str:
        params = [self.uri.signer_pubkey]
        if self.uri.secret:
            params.append(self.uri.secret)
            params.append(DEFAULT_PERMS)
        self._rpc("connect", params)
        self._user_pubkey = self._rpc("get_public_key", [])
        if len(self._user_pubkey) != 64:
            raise BunkerError(f"signer returned a bad pubkey: {self._user_pubkey[:40]}")
        return self._user_pubkey

    @property
    def pubkey_hex(self) -> str:
        return self._user_pubkey or self.connect()

    @property
    def npub(self) -> str:
        from .nostr import bech32_encode

        return bech32_encode("npub", bytes.fromhex(self.pubkey_hex))

    def sign_event(self, event: Event) -> Event:
        """Hand an unsigned event to the signer and get it back signed."""
        event.pubkey = self.pubkey_hex
        event.created_at = event.created_at or int(time.time())
        unsigned = {
            "kind": event.kind, "content": event.content, "tags": event.tags,
            "created_at": event.created_at, "pubkey": event.pubkey,
        }
        signed_raw = self._rpc("sign_event", [json.dumps(unsigned, separators=(",", ":"))])
        try:
            signed = json.loads(signed_raw)
        except ValueError as e:
            raise BunkerError(f"signer returned non-JSON: {signed_raw[:120]}") from e
        event.id = signed.get("id", "")
        event.sig = signed.get("sig", "")
        event.created_at = signed.get("created_at", event.created_at)
        if not event.id or not event.sig:
            raise BunkerError("signer response missing id/sig")
        _verify(event)
        return event

    def ping(self) -> bool:
        return self._rpc("ping", []) in ("pong", "")

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None


def _verify(event: Event) -> None:
    """
    Never trust a remote signer's output blindly. Recompute the id and check
    the signature: a buggy or hostile signer returning a valid signature over
    DIFFERENT content would otherwise publish something you never approved.
    """
    import hashlib

    from coincurve import PublicKeyXOnly

    digest = hashlib.sha256(event.serialize().encode()).digest()
    if digest.hex() != event.id:
        raise BunkerError("signer returned an id that doesn't match the content")
    try:
        ok = PublicKeyXOnly(bytes.fromhex(event.pubkey)).verify(
            bytes.fromhex(event.sig), digest
        )
    except Exception as e:  # noqa: BLE001
        raise BunkerError(f"signature malformed: {e}") from e
    if not ok:
        raise BunkerError("signature does not verify against the returned pubkey")


def get_signer():
    """
    Bunker first, local nsec second.

    If both are set the bunker wins — the safer option should never lose to
    the more convenient one by accident.
    """
    from .nostr import key_from_env

    bunker = BunkerSigner.from_env()
    if bunker is not None:
        return bunker
    return key_from_env()
