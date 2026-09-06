"""
Phase 7 — Auth & Token Key Rotation Tests (test_auth_rotation.py).

Covers:
1. _signers() returns [current, previous] when previous key is configured, [current] when None.
2. Setting SESSION_SECRET_KEY_PREVIOUS='old' while SESSION_SECRET_KEY='new':
   - Cookie signed with 'old' verifies correctly (returns UUID).
   - Cookie signed with 'new' verifies correctly.
   - Cookie signed with untrusted key raises HTTPException(401).
   - Expired cookie raises HTTPException(401) (via time/freeze_time).
3. Full key rotation cycle: Key 1 active -> Key 2 active with Key 1 as previous -> Key 1 dropped.
4. _verify_state_cookie:
   - State cookie signed with previous key verifies correctly during rotation.
   - State cookie with tampered signature raises HTTPException(400).
   - State cookie with mismatching request_state raises HTTPException(400) (hmac.compare_digest called).
5. _encrypt_token / _decrypt_token round-trip:
   - Real Fernet key: encrypt then decrypt returns original token.
   - TOKEN_ENCRYPTION_KEY=None: encrypt returns plaintext, decrypt returns plaintext.
   - Corrupt base64 key: _get_fernet() raises and caplog records error.
   - Token key rotation: ciphertext under Key A fails decryption under Key B without leaking ciphertext.
6. Cookie security attribute symmetry:
   - _set_session_cookie and _clear_session_cookie produce IDENTICAL security attributes
     (path, samesite, secure, httponly) so browsers successfully delete the cookie.
   - _set_state_cookie and _clear_state_cookie produce IDENTICAL security attributes.
7. End-to-end endpoint validation:
   - GET /auth/me with session cookie signed by previous key returns 200 with user data.
   - GET /auth/me with session cookie signed by old key after previous key dropped returns 401.
"""

from __future__ import annotations

import logging
import time
import uuid
from unittest.mock import MagicMock, patch

import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException, Response
from freezegun import freeze_time
from itsdangerous import TimestampSigner
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    _SESSION_COOKIE_NAME,
    _SESSION_MAX_AGE,
    _STATE_COOKIE_NAME,
    _STATE_MAX_AGE,
    _clear_session_cookie,
    _clear_state_cookie,
    _decrypt_token,
    _encrypt_token,
    _get_fernet,
    _set_session_cookie,
    _set_state_cookie,
    _sign_state,
    _sign_user_id,
    _signers,
    _verify_session_cookie,
    _verify_state_cookie,
)
from app.config import settings
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# 1. Signers pool structure
# ---------------------------------------------------------------------------


def test_signers_pool_single_key_and_dual_key() -> None:
    """_signers() returns [current] when previous is None, [current, previous] when set."""
    orig_curr = settings.session_secret_key
    orig_prev = settings.session_secret_key_previous

    try:
        # Case A: Only current key configured
        settings.session_secret_key = "primary-key-1"
        settings.session_secret_key_previous = None

        signers = _signers()
        assert len(signers) == 1
        assert signers[0].secret_key == b"primary-key-1"

        # Case B: Dual-key rotation mode
        settings.session_secret_key = "primary-key-2"
        settings.session_secret_key_previous = "previous-key-1"

        signers_dual = _signers()
        assert len(signers_dual) == 2
        assert signers_dual[0].secret_key == b"primary-key-2"
        assert signers_dual[1].secret_key == b"previous-key-1"
    finally:
        settings.session_secret_key = orig_curr
        settings.session_secret_key_previous = orig_prev


# ---------------------------------------------------------------------------
# 2. Session cookie verification with rotated keys
# ---------------------------------------------------------------------------


def test_verify_session_cookie_with_previous_key() -> None:
    """Cookie signed with previous key verifies correctly when dual-key rotation is active."""
    orig_curr = settings.session_secret_key
    orig_prev = settings.session_secret_key_previous

    old_key = "old-rotation-key-secret-123"
    new_key = "new-rotation-key-secret-456"
    test_user_id = uuid.uuid4()

    try:
        settings.session_secret_key = new_key
        settings.session_secret_key_previous = old_key

        # Sign cookie with old key
        old_signer = TimestampSigner(old_key)
        old_cookie = old_signer.sign(str(test_user_id)).decode()

        # Verification must succeed and return the correct UUID
        verified_id = _verify_session_cookie(old_cookie)
        assert verified_id == test_user_id

        # Cookie signed with new key also verifies
        new_cookie = _sign_user_id(test_user_id)
        verified_new_id = _verify_session_cookie(new_cookie)
        assert verified_new_id == test_user_id
    finally:
        settings.session_secret_key = orig_curr
        settings.session_secret_key_previous = orig_prev


def test_verify_session_cookie_untrusted_key_raises_401() -> None:
    """Cookie signed with a key NOT in the pool raises HTTPException(401)."""
    orig_curr = settings.session_secret_key
    orig_prev = settings.session_secret_key_previous

    test_user_id = uuid.uuid4()
    try:
        settings.session_secret_key = "legit-key-current"
        settings.session_secret_key_previous = "legit-key-previous"

        rogue_signer = TimestampSigner("untrusted-attacker-key")
        rogue_cookie = rogue_signer.sign(str(test_user_id)).decode()

        with pytest.raises(HTTPException) as exc_info:
            _verify_session_cookie(rogue_cookie)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid or expired session"
    finally:
        settings.session_secret_key = orig_curr
        settings.session_secret_key_previous = orig_prev


def test_verify_session_cookie_expired_raises_401() -> None:
    """Cookie older than max_age raises HTTPException(401) on verification."""
    orig_curr = settings.session_secret_key
    orig_prev = settings.session_secret_key_previous

    test_user_id = uuid.uuid4()
    try:
        settings.session_secret_key = "key-for-expiry-test"
        settings.session_secret_key_previous = None

        with freeze_time("2026-09-01 10:00:00"):
            cookie = _sign_user_id(test_user_id)

        # 14 days and 1 second later (> _SESSION_MAX_AGE)
        with freeze_time("2026-09-15 10:00:01"):
            with pytest.raises(HTTPException) as exc_info:
                _verify_session_cookie(cookie)
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "Invalid or expired session"
    finally:
        settings.session_secret_key = orig_curr
        settings.session_secret_key_previous = orig_prev


def test_session_key_rotation_full_cycle() -> None:
    """Test full rotation lifecycle:
    1. Key A active -> cookie A created.
    2. Rotate to Key B, Key A becomes previous -> cookie A still valid, cookie B valid.
    3. Drop Key A (rotation window expired) -> cookie A rejected, cookie B still valid.
    """
    orig_curr = settings.session_secret_key
    orig_prev = settings.session_secret_key_previous

    key_a = "secret-key-phase-a"
    key_b = "secret-key-phase-b"
    user_id = uuid.uuid4()

    try:
        # Phase 1: Key A active
        settings.session_secret_key = key_a
        settings.session_secret_key_previous = None
        cookie_a = _sign_user_id(user_id)
        assert _verify_session_cookie(cookie_a) == user_id

        # Phase 2: Rotate to Key B (Key A is previous)
        settings.session_secret_key = key_b
        settings.session_secret_key_previous = key_a
        cookie_b = _sign_user_id(user_id)

        # Both cookies must be accepted during the rotation window
        assert _verify_session_cookie(cookie_a) == user_id
        assert _verify_session_cookie(cookie_b) == user_id

        # Phase 3: Retirement — remove Key A
        settings.session_secret_key = key_b
        settings.session_secret_key_previous = None

        # Cookie B remains valid
        assert _verify_session_cookie(cookie_b) == user_id

        # Cookie A is now rejected
        with pytest.raises(HTTPException) as exc_info:
            _verify_session_cookie(cookie_a)
        assert exc_info.value.status_code == 401
    finally:
        settings.session_secret_key = orig_curr
        settings.session_secret_key_previous = orig_prev


# ---------------------------------------------------------------------------
# 3. State cookie verification with rotated keys & tampering
# ---------------------------------------------------------------------------


def test_verify_state_cookie_with_previous_key() -> None:
    """Signed OAuth state from older key verifies during key rotation window."""
    orig_curr = settings.session_secret_key
    orig_prev = settings.session_secret_key_previous

    old_key = "old-oauth-key-1"
    new_key = "new-oauth-key-2"
    raw_state = "legit_random_oauth_state_123"

    try:
        settings.session_secret_key = new_key
        settings.session_secret_key_previous = old_key

        old_signed_state = TimestampSigner(old_key).sign(raw_state).decode()

        # Verification succeeds without raising
        _verify_state_cookie(old_signed_state, raw_state)
    finally:
        settings.session_secret_key = orig_curr
        settings.session_secret_key_previous = orig_prev


def test_verify_state_cookie_tampered_value_raises_400() -> None:
    """Signed state with tampered signature raises HTTPException(400)."""
    raw_state = "valid_state_string"
    signed_state = _sign_state(raw_state)
    tampered = signed_state + "corrupt_bytes"

    with pytest.raises(HTTPException) as exc_info:
        _verify_state_cookie(tampered, raw_state)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Authentication failed"


def test_verify_state_cookie_mismatch_state_raises_400() -> None:
    """State cookie matching signature but mismatching request_state raises 400 (CSRF guard).
    Asserts hmac.compare_digest was invoked during verification."""
    raw_state_cookie = "cookie_state_value_123"
    incoming_request_state = "different_attacker_state_456"

    signed_cookie = _sign_state(raw_state_cookie)

    with patch("hmac.compare_digest", wraps=__import__("hmac").compare_digest) as mock_compare:
        with pytest.raises(HTTPException) as exc_info:
            _verify_state_cookie(signed_cookie, incoming_request_state)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Authentication failed"
        assert mock_compare.call_count >= 1


# ---------------------------------------------------------------------------
# 4. Fernet token encryption/decryption round-trip & rotation
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_token_roundtrip_with_fernet() -> None:
    """With a real Fernet key set, encrypt then decrypt returns the original plaintext."""
    orig_key = settings.token_encryption_key
    fernet_key = Fernet.generate_key().decode()

    try:
        settings.token_encryption_key = fernet_key
        plaintext = "gho_test_access_token_super_secret_999"

        encrypted = _encrypt_token(plaintext)
        assert encrypted != plaintext
        assert not encrypted.startswith("gho_")

        decrypted = _decrypt_token(encrypted)
        assert decrypted == plaintext
    finally:
        settings.token_encryption_key = orig_key


def test_encrypt_decrypt_token_plaintext_when_key_none() -> None:
    """With TOKEN_ENCRYPTION_KEY=None (test mode), encrypt and decrypt return plaintext."""
    orig_key = settings.token_encryption_key

    try:
        settings.token_encryption_key = None
        plaintext = "gho_plaintext_mode_token_123"

        assert _encrypt_token(plaintext) == plaintext
        assert _decrypt_token(plaintext) == plaintext
    finally:
        settings.token_encryption_key = orig_key


def test_invalid_fernet_key_raises_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Corrupt base64 key in settings.token_encryption_key causes _get_fernet() to raise and log."""
    orig_key = settings.token_encryption_key

    try:
        settings.token_encryption_key = "corrupt-not-base64-key!!!"
        with caplog.at_level(logging.ERROR, logger="app.auth"):
            with pytest.raises(Exception):
                _get_fernet()

        assert any("TOKEN_ENCRYPTION_KEY is set but invalid" in r.message for r in caplog.records)
    finally:
        settings.token_encryption_key = orig_key


def test_token_encryption_key_rotation_handling() -> None:
    """When TOKEN_ENCRYPTION_KEY is rotated to a new key, old ciphertext fails to decrypt,
    raising ValueError('Token decryption failed') without leaking ciphertext in exception message."""
    orig_key = settings.token_encryption_key

    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()
    raw_token = "gho_token_to_rotate_123456789"

    try:
        settings.token_encryption_key = key_a
        ciphertext_a = _encrypt_token(raw_token)

        # Rotate to Key B
        settings.token_encryption_key = key_b
        with pytest.raises(ValueError) as exc_info:
            _decrypt_token(ciphertext_a)

        assert str(exc_info.value) == "Token decryption failed"
        # Invariant: ciphertext and raw token must NEVER leak in exception message
        assert raw_token not in str(exc_info.value)
        assert ciphertext_a not in str(exc_info.value)
    finally:
        settings.token_encryption_key = orig_key


# ---------------------------------------------------------------------------
# 5. Cookie security attributes symmetry (set vs clear)
# ---------------------------------------------------------------------------


def test_session_cookie_attributes_identical_set_and_clear() -> None:
    """_set_session_cookie and _clear_session_cookie must produce identical security attributes
    (path, samesite, secure, httponly) so browsers reliably delete the cookie."""
    user_id = uuid.uuid4()
    resp_set = MagicMock(spec=Response)
    resp_clear = MagicMock(spec=Response)

    _set_session_cookie(resp_set, user_id)
    _clear_session_cookie(resp_clear)

    set_kwargs = resp_set.set_cookie.call_args.kwargs
    clear_kwargs = resp_clear.delete_cookie.call_args.kwargs

    assert set_kwargs["key"] == _SESSION_COOKIE_NAME
    assert clear_kwargs["key"] == _SESSION_COOKIE_NAME

    # Security attributes must match exactly
    for attr in ("path", "samesite", "secure", "httponly"):
        assert set_kwargs[attr] == clear_kwargs[attr], (
            f"Mismatched cookie attribute '{attr}': set={set_kwargs[attr]} vs clear={clear_kwargs[attr]}"
        )

    assert set_kwargs["httponly"] is True
    assert set_kwargs["secure"] is True
    assert set_kwargs["samesite"].lower() == "none"
    assert set_kwargs["path"] == "/"


def test_state_cookie_attributes_identical_set_and_clear() -> None:
    """_set_state_cookie and _clear_state_cookie must produce identical security attributes."""
    resp_set = MagicMock(spec=Response)
    resp_clear = MagicMock(spec=Response)

    _set_state_cookie(resp_set, "signed_state_value")
    _clear_state_cookie(resp_clear)

    set_kwargs = resp_set.set_cookie.call_args.kwargs
    clear_kwargs = resp_clear.delete_cookie.call_args.kwargs

    assert set_kwargs["key"] == _STATE_COOKIE_NAME
    assert clear_kwargs["key"] == _STATE_COOKIE_NAME

    for attr in ("path", "samesite", "secure", "httponly"):
        assert set_kwargs[attr] == clear_kwargs[attr], (
            f"Mismatched cookie attribute '{attr}': set={set_kwargs[attr]} vs clear={clear_kwargs[attr]}"
        )


# ---------------------------------------------------------------------------
# 6. End-to-end endpoint validation with rotated keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_me_endpoint_with_rotated_session_cookie(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
) -> None:
    """GET /auth/me accepts cookie signed with previous key when dual-key rotation is configured,
    and returns 401 when previous key is subsequently cleared."""
    await truncate_all(db)
    user = await user_factory(github_id=901234, username="rotated_session_user")

    orig_curr = settings.session_secret_key
    orig_prev = settings.session_secret_key_previous

    old_key = "old-previous-auth-key-111"
    new_key = "new-primary-auth-key-222"

    try:
        # Sign cookie with old key
        old_signer = TimestampSigner(old_key)
        old_cookie = old_signer.sign(str(user.id)).decode()

        # Step 1: In dual-key mode, /auth/me succeeds with 200
        settings.session_secret_key = new_key
        settings.session_secret_key_previous = old_key

        resp = await client.get("/auth/me", cookies={"haunter_session": old_cookie})
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(user.id)
        assert data["github_username"] == "rotated_session_user"

        # Step 2: Clear previous key -> /auth/me now rejects with 401
        settings.session_secret_key_previous = None
        resp_expired_rotation = await client.get("/auth/me", cookies={"haunter_session": old_cookie})
        assert resp_expired_rotation.status_code == 401
        assert resp_expired_rotation.json() == {"detail": "Invalid or expired session"}
    finally:
        settings.session_secret_key = orig_curr
        settings.session_secret_key_previous = orig_prev
