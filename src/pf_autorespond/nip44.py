"""
NIP-44 v2 encrypted payloads.

Needed only for NIP-46 remote signing: the bunker transport encrypts every
request and response. Implemented against the spec text rather than copied,
and verified against the official test vectors from paulmillr/nip44 in
tests/test_nip44.py — all 30+ valid cases plus every invalid case, because the
failure mode of hand-written crypto is silent, not loud.

Primitives come from `cryptography` (ChaCha20, HKDF, HMAC) and coincurve
(secp256k1). The only thing assembled here is the scheme itself.

One trap worth naming: NIP-44 needs the RAW x-coordinate of the ECDH shared
point. coincurve's `PrivateKey.ecdh()` returns sha256 of the compressed point,
which is a different value and produces payloads nothing else can decrypt. We
use point multiplication and take x directly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

VERSION = 2
MIN_PLAINTEXT = 1
MAX_PLAINTEXT = 4294967295
EXTENDED_PREFIX_THRESHOLD = 65536
SALT = b"nip44-v2"

# The spec says implementations SHOULD set their own payload ceiling to avoid a
# decode-bomb, since decryption needs several times the payload size in memory.
# NIP-46 requests are a few hundred bytes, so 1 MiB is generous and still safe.
# (Do NOT hardcode 65603 here: that was the pre-extended-prefix maximum, and it
# silently rejects every long payload the current spec permits.)
MAX_PAYLOAD_BYTES = 1024 * 1024


class Nip44Error(ValueError):
    pass


# ------------------------------------------------------------------- hkdf
def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


# ------------------------------------------------------------------- ecdh
def conversation_key(private_hex: str, public_hex: str) -> bytes:
    """
    HKDF-extract(salt='nip44-v2', ikm=shared_x).

    Symmetric by construction: conv(a, B) == conv(b, A).
    """
    from coincurve import PrivateKey, PublicKey

    priv = PrivateKey(bytes.fromhex(private_hex))
    # x-only pubkeys are implicitly even-y (BIP-340), so prefix 0x02.
    pub = PublicKey(b"\x02" + bytes.fromhex(public_hex))
    shared_point = pub.multiply(priv.secret)
    shared_x = shared_point.format(compressed=False)[1:33]   # raw x, NOT hashed
    return _hkdf_extract(SALT, shared_x)


def message_keys(conv_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    if len(conv_key) != 32:
        raise Nip44Error("conversation key must be 32 bytes")
    if len(nonce) != 32:
        raise Nip44Error("nonce must be 32 bytes")
    okm = _hkdf_expand(conv_key, nonce, 76)
    return okm[0:32], okm[32:44], okm[44:76]   # chacha key, chacha nonce, hmac key


# ---------------------------------------------------------------- padding
def calc_padded_len(unpadded_len: int) -> int:
    if unpadded_len <= 32:
        return 32
    next_power = 1 << (math.floor(math.log2(unpadded_len - 1)) + 1)
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * (((unpadded_len - 1) // chunk) + 1)


def pad(plaintext: str) -> bytes:
    unpadded = plaintext.encode("utf-8")
    n = len(unpadded)
    if n < MIN_PLAINTEXT or n > MAX_PLAINTEXT:
        raise Nip44Error(f"invalid plaintext length {n}")
    if n >= EXTENDED_PREFIX_THRESHOLD:
        prefix = b"\x00\x00" + n.to_bytes(4, "big")
    else:
        prefix = n.to_bytes(2, "big")
    padded_len = calc_padded_len(n)
    return prefix + unpadded + bytes(padded_len - n)


def unpad(padded: bytes) -> str:
    if len(padded) < 2:
        raise Nip44Error("padded payload too short")
    if padded[0] == 0 and padded[1] == 0:
        # extended format: 2 zero bytes then a u32. A u16 length of 0 is
        # otherwise invalid, which is what makes this unambiguous.
        if len(padded) < 6:
            raise Nip44Error("truncated extended prefix")
        n = int.from_bytes(padded[2:6], "big")
        body = padded[6:]
        header = 6
    else:
        n = int.from_bytes(padded[0:2], "big")
        body = padded[2:]
        header = 2
    plaintext = body[:n]
    if n < MIN_PLAINTEXT or len(plaintext) != n:
        raise Nip44Error("invalid padding")
    if len(padded) != header + calc_padded_len(n):
        raise Nip44Error("invalid padding length")
    return plaintext.decode("utf-8")


# ------------------------------------------------------------------ chacha
def _chacha20(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    # RFC 8439 with counter 0. `cryptography` takes a 16-byte nonce that is
    # counter||nonce, so a zero counter is four leading zero bytes.
    algo = algorithms.ChaCha20(key, b"\x00\x00\x00\x00" + nonce12)
    enc = Cipher(algo, mode=None).encryptor()
    return enc.update(data) + enc.finalize()


# ----------------------------------------------------------------- payload
def encrypt(plaintext: str, conv_key: bytes, nonce: bytes | None = None) -> str:
    nonce = nonce or secrets.token_bytes(32)
    ck, cn, hk = message_keys(conv_key, nonce)
    ciphertext = _chacha20(ck, cn, pad(plaintext))
    mac = hmac.new(hk, nonce + ciphertext, hashlib.sha256).digest()  # AAD = nonce
    return base64.b64encode(bytes([VERSION]) + nonce + ciphertext + mac).decode()


def decrypt(payload: str, conv_key: bytes) -> str:
    if not payload:
        raise Nip44Error("empty payload")
    if payload[0] == "#":
        raise Nip44Error("unsupported encryption version")
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as e:
        raise Nip44Error(f"invalid base64: {e}") from e
    if len(raw) < 99 or len(raw) > MAX_PAYLOAD_BYTES:
        raise Nip44Error(f"invalid payload size {len(raw)}")
    if raw[0] != VERSION:
        raise Nip44Error(f"unknown version {raw[0]}")
    nonce, ciphertext, mac = raw[1:33], raw[33:-32], raw[-32:]
    ck, cn, hk = message_keys(conv_key, nonce)
    expected = hmac.new(hk, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):   # constant time
        raise Nip44Error("invalid MAC")
    return unpad(_chacha20(ck, cn, ciphertext))
