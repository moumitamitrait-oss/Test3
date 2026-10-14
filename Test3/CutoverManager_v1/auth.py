"""
Local username/password authentication for the SAP BTP-IS Cutover
Automation & Management Platform.

This is a lightweight access gate for a LOCALLY-RUN app, not a substitute
for real single sign-on. It stops someone from casually opening the app
and using it without a password. It does NOT stop someone who already has
file access to the machine this runs on: they could read this file's
hashes directly (though never recover the actual passwords from them),
or simply edit app.py to remove the login check entirely, since this is
plain, readable Python running locally, not a sandboxed or compiled
binary. For a tool gating production SAP cutovers, treat this as a
baseline deterrent, not a hard security boundary -- real single sign-on
(Streamlit's own st.login(), backed by your organization's actual
identity provider) is the properly robust answer, and worth moving to
when your organization's identity team can register the app.

Passwords are NEVER stored in plaintext, here or anywhere else in this
app. Only a PBKDF2-HMAC-SHA256 hash and a random, per-user salt are
stored, in auth_users.json (gitignored, same as environments.json).
Credentials are managed exclusively through manage_users.py, run
directly on this machine -- there is deliberately no "add a user" screen
inside the running app itself, since exposing user management through
the same web UI the login screen protects would undermine the point of
having a login screen at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Optional

AUTH_FILE = Path(__file__).resolve().parent / "auth_users.json"

# OWASP's current (2023+) minimum recommendation for PBKDF2-HMAC-SHA256.
# This runs once per login attempt, not in a hot loop, so a few hundred
# milliseconds of compute is imperceptible to a person logging in, while
# meaningfully raising the cost of offline brute-forcing if this file is
# ever read by someone it shouldn't be.
PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16
_HASH_NAME = "sha256"

# Used only to compute a dummy hash for a username that doesn't exist, so
# that checking a real username and a nonexistent one take the same
# amount of time -- otherwise the response time itself would leak
# whether a given username is valid, before the password is even
# considered (a form of username enumeration).
_DUMMY_SALT = b"\x00" * _SALT_BYTES


def _derive_hash(password: str, salt: bytes) -> str:
    """PBKDF2-HMAC-SHA256 of password with salt, hex-encoded."""
    return hashlib.pbkdf2_hmac(_HASH_NAME, password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


def generate_salt() -> bytes:
    return os.urandom(_SALT_BYTES)


def load_users(path: Path = AUTH_FILE) -> dict:
    """{username: {"salt": hex str, "hash": hex str}, ...}. Returns {} if
    the file doesn't exist yet (fresh install, before anyone has run
    manage_users.py) -- this is a normal, expected state, not an error."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_users(users: dict, path: Path = AUTH_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, sort_keys=True)


def add_user(username: str, password: str, path: Path = AUTH_FILE) -> None:
    """Adds a new user, or overwrites an existing one's password (same
    Name-already-exists-means-update convention used elsewhere in this
    app, e.g. upsert_user_credential in sap_cpi_client.py)."""
    users = load_users(path)
    salt = generate_salt()
    users[username] = {"salt": salt.hex(), "hash": _derive_hash(password, salt)}
    save_users(users, path)


def remove_user(username: str, path: Path = AUTH_FILE) -> bool:
    """True if username existed and was removed; False if it didn't
    exist (nothing to remove, not an error)."""
    users = load_users(path)
    if username not in users:
        return False
    del users[username]
    save_users(users, path)
    return True


def verify_credentials(username: str, password: str, path: Path = AUTH_FILE) -> bool:
    """True only if username exists AND password matches its stored hash.
    Always performs a real PBKDF2 computation, even for a username that
    doesn't exist (against a fixed dummy salt) -- so a wrong username and
    a wrong password are indistinguishable from the time this takes, not
    just from the error message shown. The final comparison uses
    hmac.compare_digest (constant-time), not ==, so a partially-correct
    guess can't be detected by how long the comparison itself takes."""
    users = load_users(path)
    record: Optional[dict] = users.get(username)
    if record is None:
        _derive_hash(password, _DUMMY_SALT)  # always costs the same as a real check
        return False
    salt = bytes.fromhex(record["salt"])
    computed = _derive_hash(password, salt)
    return hmac.compare_digest(computed, record["hash"])


def has_any_users(path: Path = AUTH_FILE) -> bool:
    """False on a fresh install before anyone has run manage_users.py --
    used by the login screen to show a clear setup message instead of a
    login form nobody could possibly pass yet."""
    return len(load_users(path)) > 0
