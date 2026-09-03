# The plain-HTTP calls the "fabric_iq_ontology" and "work_iq" connector
# types need against Microsoft's own identity platform (Entra ID):
# building the consent URL, exchanging an authorization code for tokens,
# and refreshing an access token. Same shape and reasoning as
# google_oauth.py (plain httpx over a heavier client library for a handful
# of well-documented POSTs), but a real redirect-based Authorization Code
# flow rather than Google Identity Services' popup+postMessage trick.
# Microsoft's identity platform doesn't have an equivalent to Google's
# "postmessage" redirect_uri, so this needs a real, pre-registered redirect
# URI (see app/config.py's microsoft_oauth_* settings for where that's
# registered) that a static page in frontend/ reads its own code/state
# query params from client-side, exactly the way the popup would.
#
# This is the delegated user auth Fabric IQ Ontology's and Work IQ's own
# MCP endpoints require when queried directly (not through Foundry IQ);
# see app/config.py's own comment on why that's a real product decision,
# not just an implementation detail.
import json

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.ingestion.connector_base import ConnectorFetchError

_AUTHORIZE_PATH = "oauth2/v2.0/authorize"
_TOKEN_PATH = "oauth2/v2.0/token"

# How long a "start connecting" attempt has to actually complete Microsoft's
# consent screen before the encoded state blob (see encode_state/decode_state
# below) is rejected as expired: generous enough for a real person to
# actually sign in, short enough that a captured/logged authorize URL isn't
# useful for long.
_STATE_TTL_SECONDS = 600


class MicrosoftOAuthNotConfigured(ConnectorFetchError):
    """microsoft_oauth_tenant_id/client_id/secret aren't all set; see app/config.py."""


def _require_client_credentials() -> tuple[str, str, str]:
    if not (
        settings.microsoft_oauth_tenant_id and settings.microsoft_oauth_client_id and settings.microsoft_oauth_client_secret
    ):
        raise MicrosoftOAuthNotConfigured(
            "Microsoft sign-in isn't configured on this server -- ask your operator to set "
            "MICROSOFT_OAUTH_TENANT_ID, MICROSOFT_OAUTH_CLIENT_ID, and MICROSOFT_OAUTH_CLIENT_SECRET."
        )
    return settings.microsoft_oauth_tenant_id, settings.microsoft_oauth_client_id, settings.microsoft_oauth_client_secret


def redirect_uri() -> str:
    """The one, fixed redirect URI this app registration must have
    pre-registered (Azure Portal -> App registrations -> Authentication):
    a static page (frontend/microsoft-oauth-callback.html), not a backend
    route, since all it does is read its own URL's code/state query params
    and hand them back to the window that opened it, the same role
    Google's "postmessage" trick plays for the Drive connector."""
    if not settings.public_base_url:
        raise MicrosoftOAuthNotConfigured(
            "Microsoft sign-in needs PUBLIC_BASE_URL set on this server (the redirect URI is built from it)."
        )
    return f"{settings.public_base_url}/static/microsoft-oauth-callback.html"


def encode_state(payload: dict) -> str:
    """Packs the "what was this connect attempt for" data (provider, name,
    group_id, tenant_id, and provider-specific extras like workspace_id)
    into the OAuth `state` param itself, Fernet-encrypted with the same key
    every other stored credential in this codebase uses, rather than a
    separate server-side pending-connect table, since this is the exact
    shape Fernet's authenticated encryption already exists for: opaque to
    the browser and to Microsoft, tamper-evident, and (via ttl on decode)
    self-expiring with no cleanup job needed. Reuses token_encryption_key
    rather than a second secret: one operator setting to manage, and this
    already has to be configured for the refresh token this same flow
    produces to be stored at all."""
    key = _require_state_key()
    return Fernet(key.encode("utf-8")).encrypt(json.dumps(payload).encode("utf-8")).decode("utf-8")


def decode_state(state: str) -> dict:
    """Raises ConnectorFetchError (not InvalidToken) on anything wrong:
    tampered, expired, or encrypted under a since-rotated key, so the
    exchange route can surface one clear "try connecting again" message
    regardless of which of those it was."""
    key = _require_state_key()
    try:
        raw = Fernet(key.encode("utf-8")).decrypt(state.encode("utf-8"), ttl=_STATE_TTL_SECONDS)
    except InvalidToken as e:
        raise ConnectorFetchError("That connection attempt expired or is invalid -- please try connecting again.") from e
    return json.loads(raw.decode("utf-8"))


def _require_state_key() -> str:
    if not settings.token_encryption_key:
        raise MicrosoftOAuthNotConfigured(
            "Microsoft sign-in needs TOKEN_ENCRYPTION_KEY set on this server (see app/config.py)."
        )
    return settings.token_encryption_key


def build_authorize_url(scope: str, state: str) -> str:
    """Built server-side (not by the frontend directly) so client_secret's
    sibling settings (tenant_id) never have to be exposed to the browser
    beyond client_id, which GET /connectors/oauth/providers already treats
    as public; see that route's own docstring."""
    tenant_id, client_id, _ = _require_client_credentials()
    params = httpx.QueryParams({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "response_mode": "query",
        "scope": f"{scope} offline_access",
        "state": state,
    })
    return f"https://login.microsoftonline.com/{tenant_id}/{_AUTHORIZE_PATH}?{params}"


async def exchange_code(code: str, scope: str) -> dict:
    """Trades a one-time authorization code (from the static callback
    page's redirect) for an access_token + refresh_token pair. Raises
    ConnectorFetchError on anything that should stop the connect flow: an
    expired/already-used code, a client-secret mismatch, etc."""
    tenant_id, client_id, client_secret = _require_client_credentials()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                f"https://login.microsoftonline.com/{tenant_id}/{_TOKEN_PATH}",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri(),
                    "grant_type": "authorization_code",
                    "scope": f"{scope} offline_access",
                },
            )
        except httpx.HTTPError as e:
            raise ConnectorFetchError(f"Could not reach Microsoft to complete sign-in: {e}") from e
    if resp.status_code >= 400:
        raise ConnectorFetchError(
            "Microsoft rejected that sign-in attempt -- it may have expired. Please try connecting again."
        )
    body = resp.json()
    if "refresh_token" not in body:
        raise ConnectorFetchError(
            "Microsoft didn't return a refresh token for this connection -- this app registration may need "
            "the offline_access permission granted, or the tenant policy may not allow it for this scope."
        )
    return body


async def refresh_access_token(refresh_token: str, scope: str) -> str:
    """Mints a fresh, short-lived access token from a stored refresh token.
    Called at query time (see app/retrieval/fabric_iq_ontology_retriever.py
    and work_iq_retriever.py), not cached, since a delegated access token is
    typically only valid about 1 hour and a query may run far less often
    than that."""
    tenant_id, client_id, client_secret = _require_client_credentials()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                f"https://login.microsoftonline.com/{tenant_id}/{_TOKEN_PATH}",
                data={
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "scope": f"{scope} offline_access",
                },
            )
        except httpx.HTTPError as e:
            raise ConnectorFetchError(f"Could not reach Microsoft to refresh access: {e}") from e
    if resp.status_code == 400:
        raise ConnectorFetchError(
            "This connection was revoked or has expired -- reconnect it from the connectors list."
        )
    if resp.status_code >= 400:
        raise ConnectorFetchError(f"Microsoft returned an error refreshing access (HTTP {resp.status_code}).")
    return resp.json()["access_token"]
