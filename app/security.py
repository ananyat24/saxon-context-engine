# Ties every context-query request to a tenant identity via an API key, and
# resolves that identity to the tenant's own group_id and their own Gemini API
# key, server-side. Nothing about which tenant a request belongs to, or which
# Gemini key gets billed, is ever taken from the request itself.
#
# Why the group_id part: Neo4j Community Edition (what this project runs on)
# doesn't support separate databases per tenant the way Enterprise Edition does,
# so tenant separation here has to happen at the application layer via group_id.
# If a caller could simply put any group_id they wanted in a request, that
# "separation" would be advisory only -- one careless or malicious client could
# read another tenant's data just by guessing or enumerating group ids. Requiring
# an API key and looking up the group_id server-side means a client can only ever
# operate within the tenant its key was issued for, regardless of what it asks for.
#
# Why the Gemini key part: each tenant supplies their own Gemini API key (see
# app/config.py's TenantConfig) instead of every client sharing one operator-owned
# key. That means each tenant's own Gemini account is billed for their own usage,
# and revoking one tenant's access doesn't require rotating a key everyone shares.
#
# This is a practical baseline, not the strongest possible guarantee. The
# strongest guarantee for the group_id side is physical separation -- a separate
# Neo4j database (Neo4j Enterprise Edition) or a fully separate deployment per
# tenant. Revisit that tradeoff if/when a client's compliance requirements demand
# physical rather than logical isolation.
from fastapi import Header, HTTPException, status

from app.config import TenantConfig, settings


def require_tenant(x_api_key: str = Header(..., alias="X-API-Key")) -> TenantConfig:
    """FastAPI dependency: looks up the caller's API key and returns the
    TenantConfig (group_id + their own Gemini key) it's allowed to use.
    Raises 401 for an unknown/missing key.

    Route handlers should use the returned TenantConfig directly and must not
    also accept a group_id or API-key field from the request body -- that would
    let a client override the identity this function just verified.
    """
    tenant = settings.tenant_api_keys.get(x_api_key)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")
    return tenant
