"""Read-only GitHub App credentials for Auditor Mode."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx

from app.config import settings

GITHUB_API_BASE = "https://api.github.com"
TOKEN_TTL_SECONDS = 50 * 60
# A real installation token plus its permission map is well under 4KB. The cap
# exists so a hostile or broken endpoint cannot stream an unbounded body into
# the Lambda's heap.
MAX_CREDENTIAL_RESPONSE_BYTES = 64 * 1024
_CREDENTIAL_CHUNK_BYTES = 8 * 1024
# Allowlist, not denylist. The auditor only needs to read commit contents, PR
# metadata, and repository metadata; anything outside that set is refused even
# if its value looks harmless, so a newly granted GitHub permission cannot
# silently widen this credential.
_ALLOWED_PERMISSIONS = frozenset(
    {"contents", "pull_requests", "metadata", "single_file"}
)
_REQUIRED_READ_PERMISSIONS = frozenset({"contents", "pull_requests", "metadata"})
_ALLOWED_PERMISSION_VALUES = frozenset({"read", "write", "none"})
_TOKEN_CACHE: dict[int, tuple[str, float]] = {}


class AuditorCredentialError(RuntimeError):
    pass


class AuditorCredentialResponseTooLargeError(AuditorCredentialError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _build_read_only_jwt(app_id: str, private_key: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    now = int(time.time())
    header = _b64url(
        json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode()
    )
    payload = _b64url(
        json.dumps(
            {"iat": now - 60, "exp": now + 600, "iss": app_id},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    key = serialization.load_pem_private_key(private_key.encode(), password=None)
    if not isinstance(key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
        raise ValueError(f"Unsupported private key type: {type(key).__name__}")
    if isinstance(key, rsa.RSAPrivateKey):
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    else:
        signature = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    return f"{header}.{payload}.{_b64url(signature)}"


def _validate_read_only_permissions(data: dict[str, Any]) -> None:
    """Accept an explicitly audited permission set (read or write capable).

    Three properties are enforced:
      1. every permission key is on the allowlist
      2. every permission value is `read`, `write`, or `none`
      3. contents, pull_requests, and metadata are all present and at least `read` (or `write`)
    """
    permissions = data.get("permissions")
    if not isinstance(permissions, dict) or not permissions:
        raise AuditorCredentialError("auditor installation token omitted permissions")
    unknown = sorted(set(permissions) - _ALLOWED_PERMISSIONS)
    if unknown:
        raise AuditorCredentialError(
            f"auditor GitHub App exposes unapproved permissions: {','.join(unknown)}"
        )
    invalid_values = sorted(
        permission
        for permission, value in permissions.items()
        if not isinstance(value, str) or value not in _ALLOWED_PERMISSION_VALUES
    )
    if invalid_values:
        raise AuditorCredentialError(
            f"auditor GitHub App has invalid permission values: {','.join(invalid_values)}"
        )
    missing = sorted(
        permission
        for permission in _REQUIRED_READ_PERMISSIONS
        if permissions.get(permission) not in {"read", "write"}
    )
    if missing:
        raise AuditorCredentialError(
            "auditor GitHub App is missing required read permissions: "
            f"{','.join(missing)}"
        )


async def _read_bounded_credential_response(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
) -> bytes:
    """POST and read the response body with a hard byte ceiling."""
    try:
        async with client.stream("POST", url, headers=headers) as response:
            if response.status_code in (401, 403):
                raise AuditorCredentialError("auditor GitHub App authentication failed")
            if response.is_error:
                raise AuditorCredentialError("auditor credential request failed")
            declared = response.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > MAX_CREDENTIAL_RESPONSE_BYTES:
                        raise AuditorCredentialResponseTooLargeError(
                            "auditor credential response exceeded the size limit"
                        )
                except ValueError:
                    pass
            buffer = bytearray()
            async for chunk in response.aiter_bytes(_CREDENTIAL_CHUNK_BYTES):
                if len(buffer) + len(chunk) > MAX_CREDENTIAL_RESPONSE_BYTES:
                    raise AuditorCredentialResponseTooLargeError(
                        "auditor credential response exceeded the size limit"
                    )
                buffer.extend(chunk)
            return bytes(buffer)
    except AuditorCredentialError:
        raise
    except httpx.RequestError as exc:
        raise AuditorCredentialError(
            f"auditor credential request failed: {type(exc).__name__}"
        ) from exc


async def get_auditor_installation_token(repo: Any) -> str:
    install_id = getattr(repo, "auditor_github_install_id", None)
    app_id = settings.github_auditor_app_id
    private_key = settings.github_auditor_app_private_key
    if (
        isinstance(install_id, bool)
        or not isinstance(install_id, int)
        or install_id <= 0
    ):
        raise AuditorCredentialError("repository has no auditor GitHub installation")
    # A whitespace-only value is an unset secret, not a usable one: it passes a
    # bare truthiness check and then fails deep inside the JWT signer with a
    # cryptography error that no caller is typed to handle.
    if not isinstance(app_id, str) or not app_id.strip():
        raise AuditorCredentialError("auditor GitHub App ID is not configured")
    if not isinstance(private_key, str) or not private_key.strip():
        raise AuditorCredentialError("auditor GitHub App key is not configured")

    cached = _TOKEN_CACHE.get(install_id)
    if cached is not None:
        token, expires_at = cached
        if time.monotonic() < expires_at:
            return token

    try:
        jwt_token = _build_read_only_jwt(app_id.strip(), private_key)
    except AuditorCredentialError:
        raise
    except Exception as exc:  # noqa: BLE001 — every credential fault is typed
        # An unreadable or malformed App key is a credential misconfiguration.
        # Surfacing it as a cryptography ValueError would break the module's
        # contract that every credential failure is an AuditorCredentialError.
        raise AuditorCredentialError(
            f"auditor GitHub App key is unusable: {type(exc).__name__}"
        ) from exc
    url = f"{GITHUB_API_BASE}/app/installations/{install_id}/access_tokens"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {jwt_token}",
        "User-Agent": "Haunter-Auditor/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        body = await _read_bounded_credential_response(client, url, headers)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AuditorCredentialError(
            "auditor credential response was malformed"
        ) from exc
    if not isinstance(data, dict):
        raise AuditorCredentialError("auditor credential response was malformed")
    token = data.get("token")
    # A whitespace-only token is an unset credential, not a usable one: it
    # passes a bare truthiness check, would be cached as valid for the whole
    # TTL, and would only surface much later as an opaque 401 from GitHub.
    if not isinstance(token, str) or not token.strip():
        raise AuditorCredentialError("auditor credential response omitted token")
    _validate_read_only_permissions(data)
    _TOKEN_CACHE[install_id] = (token, time.monotonic() + TOKEN_TTL_SECONDS)
    return token
