# Microsoft Graph change notifications (webhooks): the real-time-push
# counterpart to interval polling (app/graph/connector_scheduler.py) for the
# "outlook_mail" connector type. A Graph subscription asks Graph to POST a
# notification to this deployment's own /api/v1/webhooks/graph endpoint
# (app/api/webhooks.py) whenever the watched resource changes, instead of
# this app having to keep asking "anything new?" on a timer.
#
# Scoped to outlook_mail only for now, not sharepoint too. SharePoint's
# resource path needs an extra Graph call to resolve a drive_id first
# (SharePointConnector._resolve_drive_id), which this module doesn't do yet.
# The subscription API itself is identical either way, so extending this to
# sharepoint later is a small, contained addition, not a redesign.
#
# A subscription expires (Graph's own cap for a mail resource is about 4230
# minutes, a little under 3 days) and has to be renewed before then;
# app/graph/connector_scheduler.py's periodic tick does that. If renewal
# ever fails (permission revoked, etc.), the connector doesn't break: it
# just falls back to being polled on the normal interval, same as before
# push was ever set up.
import secrets
from datetime import datetime, timedelta, timezone

import httpx

from app.ingestion.connector_base import ConnectorFetchError
from app.ingestion.graph_auth import get_graph_access_token

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Comfortably under Graph's ~3-day max for a message resource, leaving a
# wide renewal window so a missed scheduler tick or two doesn't let a
# subscription lapse.
SUBSCRIPTION_LIFETIME = timedelta(days=2, hours=12)
# A subscription due to expire within this long is renewed on the next
# scheduler tick; see app/graph/connector_scheduler.py.
RENEW_WHEN_WITHIN = timedelta(hours=18)


def new_client_state() -> str:
    """A per-subscription secret Graph echoes back on every notification.
    app/api/webhooks.py checks it matches what was stored for that
    subscription before trusting (and acting on) the notification, so a
    third party POSTing to the public, unauthenticated (Graph itself
    sends no API key) webhook endpoint can't trigger a sync for a
    connector it doesn't actually own."""
    return secrets.token_urlsafe(24)


async def create_mail_subscription(
    mailbox: str, notification_url: str, client_state: str
) -> tuple[str, datetime]:
    """Subscribes to new-message notifications for `mailbox`'s inbox.
    Returns (subscription_id, expires_at). Raises ConnectorFetchError on
    any failure; callers should treat this as best-effort (the connector
    still works via polling) rather than fail connector creation over it."""
    expires_at = datetime.now(timezone.utc) + SUBSCRIPTION_LIFETIME
    async with httpx.AsyncClient(timeout=15.0) as client:
        token = await get_graph_access_token(client, missing_permission_hint="Mail.Read")
        try:
            resp = await client.post(
                f"{_GRAPH_BASE}/subscriptions",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "changeType": "created",
                    "notificationUrl": notification_url,
                    "resource": f"/users/{mailbox}/mailFolders('Inbox')/messages",
                    "expirationDateTime": expires_at.isoformat(),
                    "clientState": client_state,
                },
            )
        except httpx.HTTPError as e:
            raise ConnectorFetchError(f"Could not reach Microsoft Graph: {e}") from e
        if resp.status_code >= 400:
            raise ConnectorFetchError(
                f"Microsoft Graph refused the push subscription (HTTP {resp.status_code}): {resp.text[:300]}"
            )
        subscription_id = resp.json().get("id")
        if not subscription_id:
            raise ConnectorFetchError("Microsoft Graph didn't return a subscription id.")
        return subscription_id, expires_at


async def renew_subscription(subscription_id: str) -> datetime:
    """Extends an existing subscription's expiry. Raises ConnectorFetchError
    if Graph rejects it (e.g. the subscription was already deleted/expired,
    or the permission was revoked); callers should clear the connector's
    stored subscription in that case so it falls back to polling instead of
    retrying a renewal that will never succeed."""
    expires_at = datetime.now(timezone.utc) + SUBSCRIPTION_LIFETIME
    async with httpx.AsyncClient(timeout=15.0) as client:
        token = await get_graph_access_token(client, missing_permission_hint="Mail.Read")
        try:
            resp = await client.patch(
                f"{_GRAPH_BASE}/subscriptions/{subscription_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"expirationDateTime": expires_at.isoformat()},
            )
        except httpx.HTTPError as e:
            raise ConnectorFetchError(f"Could not reach Microsoft Graph: {e}") from e
        if resp.status_code >= 400:
            raise ConnectorFetchError(f"Microsoft Graph refused the renewal (HTTP {resp.status_code}).")
        return expires_at


async def delete_subscription(subscription_id: str) -> None:
    """Best-effort cleanup (e.g. when a connector is deleted). A failure
    here just means Graph keeps sending notifications for a subscription
    nothing is listening for anymore until it naturally expires, not
    something worth surfacing as an error to the caller."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token = await get_graph_access_token(client, missing_permission_hint="Mail.Read")
            await client.delete(
                f"{_GRAPH_BASE}/subscriptions/{subscription_id}", headers={"Authorization": f"Bearer {token}"}
            )
    except Exception:
        pass
