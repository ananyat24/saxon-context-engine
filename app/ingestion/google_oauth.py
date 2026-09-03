# The three plain-HTTP calls the "google_drive_oauth" connector type needs
# against Google's own OAuth endpoints: exchanging an authorization code
# for tokens, refreshing an access token, and revoking a grant on
# disconnect. Deliberately not the google-auth-oauthlib package: these are
# three small, well-documented POSTs, and this codebase already reaches for
# plain httpx over a heavier client library wherever the API surface is this
# small (see google_drive_source.py's own module docstring).
#
# This is the user OAuth half of Drive access; see google_drive_source.py
# for the service-account half, which is a different auth model entirely
# (no per-user consent, no refresh token to manage).
import httpx

from app.config import settings
from app.ingestion.connector_base import ConnectorFetchError

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Google Identity Services' browser-side `initCodeClient` (ux_mode: "popup")
# exchanges its authorization code via postMessage, not a real HTTP
# redirect. Google's token endpoint expects the literal string "postmessage"
# as redirect_uri for that flow, not this deployment's own URL. See
# https://developers.google.com/identity/oauth2/web/guides/use-code-model.
_POPUP_REDIRECT_URI = "postmessage"


class GoogleOAuthNotConfigured(ConnectorFetchError):
    """google_oauth_client_id/secret aren't set; see app/config.py."""


def _require_client_credentials() -> tuple[str, str]:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise GoogleOAuthNotConfigured(
            "Google Drive one-click connect isn't configured on this server -- ask your "
            "operator to set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET."
        )
    return settings.google_oauth_client_id, settings.google_oauth_client_secret


async def exchange_code(code: str) -> dict:
    """Trades a one-time authorization code (from the browser's Google
    Identity Services popup) for an access_token + refresh_token pair.
    Raises ConnectorFetchError on anything that should stop the connect
    flow: an expired/already-used code, a client-secret mismatch, etc."""
    client_id, client_secret = _require_client_credentials()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": _POPUP_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
        except httpx.HTTPError as e:
            raise ConnectorFetchError(f"Could not reach Google to complete sign-in: {e}") from e
    if resp.status_code >= 400:
        raise ConnectorFetchError(
            "Google rejected that sign-in attempt -- it may have expired. Please try connecting again."
        )
    body = resp.json()
    if "refresh_token" not in body:
        # Happens if the user has already granted this app the same scope
        # before and Google skipped re-issuing a refresh token. Asking
        # for consent again (prompt=consent) forces a fresh one. The
        # frontend's initCodeClient already requests consent every time
        # (see frontend/app.js), so this is a defensive check, not the
        # expected path.
        raise ConnectorFetchError(
            "Google didn't return a refresh token for this connection -- try disconnecting any "
            "existing Saxon access in your Google Account's Security settings, then connect again."
        )
    return body


async def refresh_access_token(refresh_token: str) -> str:
    """Mints a fresh, short-lived access token from a stored refresh token.
    Called at the start of every sync (see google_drive_source.py's
    GoogleDriveOAuthConnector), not cached across syncs, since Drive access
    tokens are only valid about 1 hour and a background sync may run far
    less often than that."""
    client_id, client_secret = _require_client_credentials()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as e:
            raise ConnectorFetchError(f"Could not reach Google to refresh Drive access: {e}") from e
    if resp.status_code == 400:
        # The standard shape for "this refresh token was revoked": by the
        # user in their Google Account settings, or by us on disconnect.
        raise ConnectorFetchError(
            "This Drive connection was revoked or has expired -- reconnect it from the connectors list."
        )
    if resp.status_code >= 400:
        raise ConnectorFetchError(f"Google Drive returned an error refreshing access (HTTP {resp.status_code}).")
    return resp.json()["access_token"]


async def revoke_token(token: str) -> None:
    """Best-effort: called when a google_drive_oauth connector is deleted,
    so disconnecting in Saxon also actually revokes the grant at Google
    (matching what "disconnect this app" means in Google's own Security
    settings), not just deleting our own pointer to it. Never raises: the
    connector deletion itself should still succeed even if Google's revoke
    endpoint is unreachable or the token's already invalid."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(_REVOKE_URL, params={"token": token})
        except httpx.HTTPError:
            pass
