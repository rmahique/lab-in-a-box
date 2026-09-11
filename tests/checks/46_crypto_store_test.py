#!/usr/bin/env python3
# Unit + REAL round-trip tests for libs/crypto_store.py — this is the actual
# live check of the Argon2id/HKDF-SHA512/AES-256-GCM/ChaCha20-Poly1305 API
# calls (see that module's own docstring: it was written against the
# documented `cryptography` API but not empirically verified before this
# test existed). Uses the real `cryptography` package installed in this test
# container (tests/Containerfile) — nothing here is mocked; if
# derive_master_key()'s Argon2id(...) call has the wrong keyword arguments
# for the installed cryptography version, THIS is where it fails, with a
# real TypeError, not a confusing failure three layers away in
# setup_credentials.py or primary.py. Run from 46_crypto_store.sh, in its
# own container — see tests/run_tests.sh.
import base64
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import crypto_store  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── real round trip: this alone proves the Argon2id/HKDF/AEAD calls work ────
plaintext = b"AWS_SECRET_ACCESS_KEY: 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'\n"
envelope = crypto_store.encrypt_cascade("correct horse battery staple", plaintext)
check("encrypt_cascade returns a dict with every expected field",
      set(envelope) >= {"encrypted", "kdf", "cipher", "salt", "nonce1", "nonce2", "ciphertext",
                        "kdf_time_cost", "kdf_memory_cost_kib", "kdf_parallelism"})
check("encrypt_cascade marks the envelope encrypted=True", envelope["encrypted"] is True)
check("envelope records the real kdf/cipher identifiers",
      envelope["kdf"] == "argon2id" and envelope["cipher"] == "aes-256-gcm+chacha20-poly1305")
for f in ("salt", "nonce1", "nonce2", "ciphertext"):
    try:
        base64.b64decode(envelope[f], validate=True)
        ok = True
    except Exception:
        ok = False
    check("envelope['{}'] is valid base64".format(f), ok)

recovered = crypto_store.decrypt_cascade("correct horse battery staple", envelope)
check("decrypt_cascade(correct passphrase) recovers the exact original plaintext",
      recovered == plaintext)

# ── wrong passphrase -> DecryptionError, never silent garbage ───────────────
died = False
try:
    crypto_store.decrypt_cascade("wrong passphrase entirely", envelope)
except crypto_store.DecryptionError:
    died = True
check("decrypt_cascade(wrong passphrase) raises DecryptionError", died)

# ── tampered ciphertext -> DecryptionError (AEAD tag check catches it) ─────
tampered = dict(envelope)
raw = bytearray(base64.b64decode(tampered["ciphertext"]))
raw[0] ^= 0xFF
tampered["ciphertext"] = base64.b64encode(bytes(raw)).decode("ascii")
died = False
try:
    crypto_store.decrypt_cascade("correct horse battery staple", tampered)
except crypto_store.DecryptionError:
    died = True
check("decrypt_cascade of a tampered ciphertext raises DecryptionError (AEAD tag mismatch)", died)

# ── malformed envelope -> DecryptionError, not a raw KeyError/TypeError ─────
died = False
try:
    crypto_store.decrypt_cascade("whatever", {"kdf": "argon2id", "cipher": envelope["cipher"]})
except crypto_store.DecryptionError:
    died = True
except Exception as e:
    print("FAIL (wrong exception type): {}".format(e))
check("decrypt_cascade of a malformed envelope raises DecryptionError, not a raw exception", died)

# ── unrecognised kdf/cipher id -> DecryptionError ───────────────────────────
died = False
try:
    crypto_store.decrypt_cascade("whatever", dict(envelope, cipher="rot13"))
except crypto_store.DecryptionError:
    died = True
check("decrypt_cascade of an envelope with an unrecognised cipher id raises DecryptionError", died)

# ── randomness: two encryptions of the same plaintext/passphrase differ ────
envelope2 = crypto_store.encrypt_cascade("correct horse battery staple", plaintext)
check("two encryptions of the same plaintext use different salts (real randomness, not reused)",
      envelope["salt"] != envelope2["salt"])
check("two encryptions of the same plaintext use different nonces",
      envelope["nonce1"] != envelope2["nonce1"] and envelope["nonce2"] != envelope2["nonce2"])
check("...but both still decrypt correctly with the right passphrase",
      crypto_store.decrypt_cascade("correct horse battery staple", envelope2) == plaintext)

# ── HKDF domain separation: the two subkeys actually differ ────────────────
master = crypto_store.derive_master_key("pw", b"0123456789ABCDEF")
aes_key, chacha_key = crypto_store.split_keys(master, b"0123456789ABCDEF")
check("split_keys() derives two DIFFERENT 32-byte subkeys from one master key",
      len(aes_key) == 32 and len(chacha_key) == 32 and aes_key != chacha_key)
check("derive_subkey() is deterministic for the same (master_key, info, salt)",
      crypto_store.derive_subkey(master, crypto_store._AES_INFO, b"0123456789ABCDEF") == aes_key)


# ── prompt_passphrase(): confirm=True retries on mismatch, no-echo prompts ──
_answers = iter(["first", "second-doesnt-match", "final", "final"])
crypto_store.getpass.getpass = lambda prompt="": next(_answers)
result = crypto_store.prompt_passphrase(confirm=True)
check("prompt_passphrase(confirm=True) retries until the two entries match", result == "final")

crypto_store.getpass.getpass = lambda prompt="": "single-answer"
check("prompt_passphrase(confirm=False) returns the first answer, no confirmation asked",
      crypto_store.prompt_passphrase() == "single-answer")


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all crypto_store checks passed")
