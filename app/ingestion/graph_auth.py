# Shared Microsoft Graph client-credentials auth: both the "sharepoint"
# and "outlook_mail" connector types (app/ingestion/sharepoint_source.py,
# app/ingestion/outlook_mail_source.py) authenticate as the same Azure AD
# app registration the same way; factored out here rather than duplicated,
# same spirit as app/ingestion/html_text.py.
import httpx

from app.config import settings
from app.ingestion.connector_base import ConnectorFetchError

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


async def get_graph_access_token(client: httpx.AsyncClient, *, missing_permission_hint: str) -> str:
    """`missing_permission_hint` names the specific Graph application
    permission this caller needs (e.g. "Sites.Read.All" or "Mail.Read"),
    surfaced in the 403 case's error message: the same app registration
    backs multiple connector types, each needing its own permission
    admin-consented, so a generic "access denied" wouldn't say which one."""
    tenant_id = settings.sharepoint_tenant_id
    client_id = settings.sharepoint_client_id
    client_secret = settings.sharepoint_client_secret
    if not (tenant_id and client_id and client_secret):
        raise ConnectorFetchError(
            "This connector isn't configured on this server -- ask your operator to set "
            "SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID, and SHAREPOINT_CLIENT_SECRET."
        )
    try:
        resp = await client.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
    except httpx.HTTPError as e:
        raise ConnectorFetchError(f"Could not reach Microsoft's login service: {e}") from e
    if resp.status_code >= 400:
        raise ConnectorFetchError(
            "Could not authenticate to Microsoft Graph -- check the app registration's tenant id, "
            f"client id, and client secret, and that it's been granted (and admin-consented) "
            f"the {missing_permission_hint} application permission."
        )
    token = resp.json().get("access_token")
    if not token:
        raise ConnectorFetchError("Microsoft's login service didn't return an access token.")
    return token
