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
import secrets

from fastapi import Header, HTTPException, Request, status

from app.config import TenantConfig, settings


def require_tenant(request: Request, x_api_key: str = Header(..., alias="X-API-Key")) -> TenantConfig:
    """FastAPI dependency: looks up the caller's API key and returns the
    TenantConfig (their knowledge bases + their own Gemini key) it's allowed to
    use. Raises 401 for an unknown/missing key.

    Checks the static, startup-time settings.tenant_api_keys first (no I/O,
    handles every tenant onboarded the original way -- see that field's own
    docstring), then falls back to the Neo4j-backed store a tenant created
    through the admin API lands in (app/graph/tenants.py) -- so a *new*
    tenant is usable immediately, without a redeploy, while an existing
    tenant's behavior is unchanged.

    Route handlers should use the returned TenantConfig directly and must not
    also accept a group_id or API-key field from the request body -- that would
    let a client override the identity this function just verified.
    """
    tenant = settings.tenant_api_keys.get(x_api_key)
    if tenant is None:
        from app.graph.graph_repository import GraphRepository
        from app.graph.tenants import find_tenant_by_api_key

        repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
        tenant = find_tenant_by_api_key(x_api_key, repo=repo)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")
    return tenant


def require_admin(x_admin_key: str = Header(..., alias="X-Admin-Key")) -> None:
    """FastAPI dependency backing the admin API (app/api/admin.py) -- a
    single operator-held credential, separate from any tenant's own API
    key, since it can create/delete *any* tenant. secrets.compare_digest
    (not `==`) so a mistyped key can't be brute-forced faster via response-
    timing differences on how many leading characters matched."""
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The admin API isn't configured on this server -- ask your operator to set ADMIN_API_KEY.",
        )
    if not secrets.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin key")


def resolve_knowledge_base(tenant: TenantConfig, knowledge_base: str | None) -> str:
    """Turns a client-supplied knowledge_base id (or None) into the group_id to
    actually query. A tenant can have more than one knowledge base, but this
    still enforces the same boundary require_tenant does for the tenant as a
    whole: the requested id must be one of *this* tenant's own knowledge bases,
    or the request is rejected -- a client can pick among its own datasets, but
    never reach one it wasn't given.
    """
    kb_id = knowledge_base or tenant.default_knowledge_base_id()
    if kb_id not in tenant.knowledge_base_ids():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown knowledge base '{kb_id}' for this tenant.",
        )
    return kb_id
