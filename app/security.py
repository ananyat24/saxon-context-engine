# Ties every context-query request to a tenant identity via an API key, and
# derives the Graphiti group_id (this project's mechanism for keeping one
# tenant's data logically separate from another's -- see scripts/test_graph.py
# for group_id in action) from that verified identity, server-side.
#
# Why this, specifically: Neo4j Community Edition (what this project runs on)
# doesn't support separate databases per tenant the way Enterprise Edition does,
# so tenant separation here has to happen at the application layer via group_id.
# If a caller could simply put any group_id they wanted in a request, that
# "separation" would be advisory only -- one careless or malicious client could
# read another tenant's data just by guessing or enumerating group ids. Requiring
# an API key and looking up the group_id server-side means a client can only ever
# operate within the tenant its key was issued for, regardless of what it asks for.
#
# This is a practical baseline, not the strongest possible guarantee. The
# strongest guarantee is physical separation -- a separate Neo4j database (Neo4j
# Enterprise Edition) or a fully separate deployment per tenant, which trades
# infrastructure cost/complexity for eliminating any risk of an application-layer
# bug leaking data across tenants. Revisit that tradeoff if/when a client's
# compliance requirements demand physical rather than logical isolation.
from fastapi import Header, HTTPException, status

from app.config import settings


def require_tenant(x_api_key: str = Header(..., alias="X-API-Key")) -> str:
    """FastAPI dependency: looks up the caller's API key and returns the
    group_id it's allowed to operate on. Raises 401 for an unknown/missing key.

    Route handlers should use the returned group_id directly and must not also
    accept a group_id/group_ids field from the request body -- that would let a
    client override the identity this function just verified.
    """
    group_id = settings.tenant_api_keys.get(x_api_key)
    if group_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")
    return group_id
