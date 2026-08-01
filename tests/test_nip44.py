"""NIP-44 v2 against the official vectors (paulmillr/nip44)."""

import json
from pathlib import Path

import pytest

from pf_autorespond import nip44

V = json.loads((Path(__file__).parent / "nip44_vectors.json").read_text())["v2"]


def _hex(s):
    return bytes.fromhex(s)


@pytest.mark.parametrize("c", V["valid"]["get_conversation_key"])
def test_conversation_key_vectors(c):
    assert nip44.conversation_key(c["sec1"], c["pub2"]).hex() == c["conversation_key"]


def test_conversation_key_is_symmetric():
    from coincurve import PrivateKey

    a = PrivateKey(); b = PrivateKey()
    ax = a.public_key.format(compressed=True)[1:].hex()
    bx = b.public_key.format(compressed=True)[1:].hex()
    assert nip44.conversation_key(a.to_hex(), bx) == nip44.conversation_key(b.to_hex(), ax)


def _pub_of(sec_hex: str) -> str:
    from coincurve import PrivateKey

    return PrivateKey(bytes.fromhex(sec_hex)).public_key.format(True)[1:].hex()


@pytest.mark.parametrize("c", V["valid"]["encrypt_decrypt"])
def test_encrypt_decrypt_vectors(c):
    ck = nip44.conversation_key(c["sec1"], _pub_of(c["sec2"]))
    assert ck.hex() == c["conversation_key"]
    assert nip44.encrypt(c["plaintext"], ck, _hex(c["nonce"])) == c["payload"]
    assert nip44.decrypt(c["payload"], ck) == c["plaintext"]
    # and the other direction, since the conversation key is symmetric
    ck2 = nip44.conversation_key(c["sec2"], _pub_of(c["sec1"]))
    assert nip44.decrypt(c["payload"], ck2) == c["plaintext"]


@pytest.mark.parametrize("c", V["valid"]["get_message_keys"]["keys"])
def test_message_key_vectors(c):
    ck = _hex(V["valid"]["get_message_keys"]["conversation_key"])
    chacha_key, chacha_nonce, hmac_key = nip44.message_keys(ck, _hex(c["nonce"]))
    assert chacha_key.hex() == c["chacha_key"]
    assert chacha_nonce.hex() == c["chacha_nonce"]
    assert hmac_key.hex() == c["hmac_key"]


@pytest.mark.parametrize("c", V["valid"]["encrypt_decrypt_long_msg"])
def test_long_message_vectors(c):
    """Exercises the 6-byte extended prefix path for plaintexts >= 65536."""
    import hashlib

    ck = _hex(c["conversation_key"])
    plaintext = c["pattern"] * c["repeat"]
    assert hashlib.sha256(plaintext.encode()).hexdigest() == c["plaintext_sha256"]
    payload = nip44.encrypt(plaintext, ck, _hex(c["nonce"]))
    assert hashlib.sha256(payload.encode()).hexdigest() == c["payload_sha256"]
    assert nip44.decrypt(payload, ck) == plaintext


@pytest.mark.parametrize("c", V["valid"]["calc_padded_len"])
def test_padding_vectors(c):
    assert nip44.calc_padded_len(c[0]) == c[1]


def test_empty_plaintext_rejected():
    """
    Only length 0 is genuinely invalid.

    The vector file also lists 65536 / 100000 / 10000000 as invalid, but that
    file predates the extended-prefix revision: the current spec sets
    max_plaintext_size to 2^32-1 and defines a 6-byte prefix for lengths at or
    above 65536 — and the file's own `encrypt_decrypt_long_msg` section proves
    it, since those cases exceed 65536 and are marked valid. We follow the
    current spec and cover the long path in test_long_message_vectors.
    """
    ck = b"\x01" * 32
    with pytest.raises(nip44.Nip44Error):
        nip44.encrypt("", ck)


@pytest.mark.parametrize("n", [65535, 65536, 65537, 100000])
def test_extended_prefix_round_trips(n):
    """
    The official vectors top out at exactly 65535 — one byte BELOW the
    extended-prefix threshold — so they never exercise the 6-byte prefix path
    at all. This is the only coverage for it, and it earned its place: it
    caught a stale 65603-byte ceiling in decrypt() that rejected every payload
    the current spec added.
    """
    from coincurve import PrivateKey

    a, b = PrivateKey(), PrivateKey()
    ck = nip44.conversation_key(a.to_hex(), b.public_key.format(True)[1:].hex())
    msg = "x" * n
    assert nip44.decrypt(nip44.encrypt(msg, ck), ck) == msg


@pytest.mark.parametrize("c", V["invalid"]["decrypt"])
def test_invalid_payloads_rejected(c):
    with pytest.raises((nip44.Nip44Error, UnicodeDecodeError)):
        nip44.decrypt(c["payload"], _hex(c["conversation_key"]))


@pytest.mark.parametrize("c", V["invalid"]["get_conversation_key"])
def test_invalid_keys_rejected(c):
    with pytest.raises(Exception):
        nip44.conversation_key(c["sec1"], c["pub2"])


def test_round_trip_with_random_nonce():
    from coincurve import PrivateKey

    a, b = PrivateKey(), PrivateKey()
    ck = nip44.conversation_key(a.to_hex(), b.public_key.format(True)[1:].hex())
    for msg in ("x", "a" * 31, "a" * 32, "a" * 33, "héllo 🔬", "n" * 5000):
        assert nip44.decrypt(nip44.encrypt(msg, ck), ck) == msg


def test_tampered_ciphertext_fails_the_mac():
    import base64

    from coincurve import PrivateKey

    a, b = PrivateKey(), PrivateKey()
    ck = nip44.conversation_key(a.to_hex(), b.public_key.format(True)[1:].hex())
    raw = bytearray(base64.b64decode(nip44.encrypt("hello", ck)))
    raw[40] ^= 0x01
    with pytest.raises(nip44.Nip44Error, match="MAC"):
        nip44.decrypt(base64.b64encode(bytes(raw)).decode(), ck)
