# MCP server (v3.5) -- exposes the same tenant-scoped context-query path
# HTTP clients hit at POST /api/v1/context/query (see app/context/query_service.py)
# as MCP tools, so any MCP-capable agent (Claude Desktop/Code, Copilot, a
# custom agent) can query a client's consolidated graph directly, not only
# through Saxon's own chat UI.
#
# Same auth model as the HTTP API, deliberately not a separate one: a caller
# authenticates with the same X-API-Key header (see app/security.py), read
# here from the MCP request's raw headers via Context.headers rather than a
# FastAPI Header() dependency -- MCP tool functions aren't part of FastAPI's
# dependency-injection system, so require_tenant's lookup logic is
# reimplemented against a plain header mapping instead of reused directly.
#
# Mounted into the same FastAPI app/process as the HTTP API (app/main.py) --
# one container, one deploy, no separate auth model or infrastructure to
# stand up for this to work.
import asyncio
from typing import Optional

from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.config import TenantConfig, settings
from app.context.query_service import execute_causal_query, execute_context_query
from app.graph import document_sets
from app.graph.graph_repository import GraphRepository
from app.graph.neo4j_client import Neo4jClient
from app.graph.tenant_graphiti_pool import TenantGraphitiPool

mcp_server = MCPServer(
    name="saxon-context-engine",
    instructions=(
        "Query a client's consolidated, temporal knowledge graph -- entities and "
        "facts reconciled across their connected sources (CRM, documents, email, "
        "web, Drive, SharePoint), each fact still linked back to which source it "
        "came from and when it became true. Call list_available_sources first if "
        "you don't already know which knowledge base or document set to query."
    ),
)

# Set once, at process startup, by app/main.py's lifespan -- see that module's
# comment on why this can't just be request.app.state the way the HTTP route
# reaches these: the MCP server is mounted as its own Starlette sub-app
# (mcp_server.streamable_http_app()), which doesn't share the parent FastAPI
# app's `state` object.
_neo4j_client: Optional[Neo4jClient] = None
_graphiti_pool: Optional[TenantGraphitiPool] = None


def configure(neo4j_client: Neo4jClient, graphiti_pool: TenantGraphitiPool) -> None:
    global _neo4j_client, _graphiti_pool
    _neo4j_client = neo4j_client
    _graphiti_pool = graphiti_pool


async def _authenticate(ctx: Context) -> TenantConfig:
    """Same lookup as app/security.py's require_tenant (static config first,
    then the Neo4j-backed store a tenant created through the admin API
    lands in -- see app/graph/tenants.py), against MCP's own header mapping
    instead of a FastAPI Header() dependency. Raises ToolError (not
    HTTPException -- there's no HTTP response to shape here), which the MCP
    SDK treats as an anticipated failure and surfaces to the calling agent
    verbatim -- a plain exception would be masked as a generic "Error
    executing tool" with no detail, per mcp.server.mcpserver.tools.base.Tool.run."""
    headers = ctx.headers or {}
    api_key = headers.get("x-api-key")
    if not api_key:
        raise ToolError("Missing X-API-Key header. Configure your MCP client with this tenant's API key.")
    tenant = settings.tenant_api_keys.get(api_key)
    if tenant is None:
        assert _neo4j_client is not None, "MCP server not configured -- see configure()"
        from app.graph.tenants import find_tenant_by_api_key

        repo = GraphRepository(neo4j_client=_neo4j_client)
        tenant = await asyncio.to_thread(find_tenant_by_api_key, api_key, repo=repo)
    if tenant is None:
        raise ToolError("Invalid X-API-Key.")
    return tenant


@mcp_server.tool()
async def query_context_graph(
    query: str,
    ctx: Context,
    knowledge_base: Optional[str] = None,
    document_set: Optional[str] = None,
    as_user: Optional[str] = None,
) -> dict:
    """Ask a question in plain language and get back a synthesized answer
    plus the exact sourced facts it's built from (which connector each fact
    came from, when it became true, whether it's since been superseded).

    knowledge_base: id of one specific connected source to search (see
    list_available_sources). Omit to use the tenant's default source.
    document_set: id of a named group of sources to search across instead of
    just one (see list_available_sources). Mutually exclusive with as_user.
    as_user: restrict the answer to what this person can see in their org's
    hierarchy, instead of the whole knowledge base. Mutually exclusive with
    document_set.
    """
    tenant = await _authenticate(ctx)
    assert _neo4j_client is not None and _graphiti_pool is not None, "MCP server not configured -- see configure()"
    try:
        packet = await execute_context_query(
            tenant=tenant,
            query=query,
            neo4j_client=_neo4j_client,
            graphiti_pool=_graphiti_pool,
            knowledge_base=knowledge_base,
            document_set=document_set,
            as_user=as_user,
        )
    except HTTPException as e:
        raise ToolError(str(e.detail))
    return packet


@mcp_server.tool()
async def query_causal_chain(
    query: str, ctx: Context, knowledge_base: Optional[str] = None, as_user: Optional[str] = None
) -> dict:
    """What happened -> why -> impact -> recommendation, reasoned across a
    chain of related facts (e.g. an at-risk Order to its Product to a
    Component to the Supplier to an open QualityEvent) -- distinct from
    query_context_graph above, which only ever restates facts already in
    the graph and never infers or recommends anything.

    Returns a dict whose "recommendation" field (what_happened/why/impact/
    recommendation) is a generated suggestion, kept separate from the
    grounded "summary" field of facts it was built from -- the two are
    never blended, so a caller always knows which is which. Saxon does not
    act on the recommendation; it's also logged as an auditable :Decision
    graph node ("decision_id") for later review.

    knowledge_base: id of one specific connected source to search. Omit to
    use the tenant's default source. as_user restricts the chain to what
    that person can see in their org's hierarchy, same as
    query_context_graph's as_user. document_set scoping isn't supported for
    this mode yet -- a causal chain needs one clear knowledge base to write
    its Decision node into.
    """
    tenant = await _authenticate(ctx)
    assert _neo4j_client is not None and _graphiti_pool is not None, "MCP server not configured -- see configure()"
    try:
        packet = await execute_causal_query(
            tenant=tenant,
            query=query,
            neo4j_client=_neo4j_client,
            graphiti_pool=_graphiti_pool,
            knowledge_base=knowledge_base,
            as_user=as_user,
        )
    except HTTPException as e:
        raise ToolError(str(e.detail))
    return packet


@mcp_server.tool()
async def list_available_sources(ctx: Context) -> dict:
    """List this tenant's own connected knowledge bases (individual sources)
    and document sets (named groups of sources), for choosing what to pass
    as query_context_graph's knowledge_base or document_set argument."""
    tenant = await _authenticate(ctx)
    assert _neo4j_client is not None, "MCP server not configured -- see configure()"
    repo = GraphRepository(neo4j_client=_neo4j_client)
    return {
        "knowledge_bases": [{"id": kb.id, "label": kb.label} for kb in tenant.knowledge_bases],
        "document_sets": [
            {"id": d["id"], "name": d["name"], "connector_ids": d["connector_ids"]}
            for d in document_sets.list_document_sets(tenant.tenant_id, repo=repo)
        ],
    }
