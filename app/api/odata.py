# A read-only OData v4 feed over the same tenant-scoped entity/fact data
# app/api/graph.py already exposes to Saxon's own UI. The "Context Layer"
# reference architecture explicitly names Power BI as a first-class
# consumer alongside Copilots, agents, and apps, and Power BI Desktop's
# built-in "OData Feed" data source (Get Data > OData Feed) can point
# straight at this with no custom connector to build or certify. Same
# auth, same tenant boundary, same underlying Cypher this project already
# runs. This file only adds the OData request/response shape on top.
#
# Deliberately not a full OData implementation ($filter, $orderby, $expand,
# and so on). Just enough ($top, plus the service document and $metadata a
# client needs to discover the feed) for Power BI's OData connector to
# load Entities/Facts as tables. If richer server-side filtering becomes a
# real need, extend this rather than reaching for a separate OData
# library. The data source underneath is exactly the same Cypher
# app/api/graph.py already runs.
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.config import TenantConfig
from app.graph.graph_repository import GraphRepository
from app.security import require_tenant, resolve_knowledge_base

router = APIRouter()

_MAX_TOP = 5000
_DEFAULT_TOP = 200


def _clamp_top(top: int | None) -> int:
    if top is None:
        return _DEFAULT_TOP
    return max(1, min(top, _MAX_TOP))


def _service_root(request: Request) -> str:
    # Prefer the configured public URL (see app/config.py's public_base_url)
    # when it's set. Behind Azure Container Apps, request.base_url reflects
    # the internal HTTP forwarding address, not the client-facing HTTPS one,
    # and an OData client resolves relative @odata.context/next-link URLs
    # against whatever this returns.
    from app.config import settings

    base = settings.public_base_url or str(request.base_url).rstrip("/")
    return f"{base}/api/v1/odata"


@router.get("")
def service_document(request: Request, tenant: TenantConfig = Depends(require_tenant)):
    """The OData service document. This is the first thing an OData client
    (Power BI's OData Feed connector included) requests to discover what
    feeds (EntitySets) are available."""
    root = _service_root(request)
    return {
        "@odata.context": f"{root}/$metadata",
        "value": [
            {"name": "Entities", "kind": "EntitySet", "url": "Entities"},
            {"name": "Facts", "kind": "EntitySet", "url": "Facts"},
        ],
    }


# Minimal CSDL describing the two feeds below. Every property is
# deliberately typed Edm.String, including timestamps. Graphiti's
# valid_at/invalid_at can be null or absent, and a nullable
# Edm.DateTimeOffset that's sometimes missing entirely trips up some
# OData clients' schema inference more than a plain string does. A client
# that wants to parse them as dates can still do so on its own side.
_METADATA_XML = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema Namespace="Saxon" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="Entity">
        <Key><PropertyRef Name="id"/></Key>
        <Property Name="id" Type="Edm.String" Nullable="false"/>
        <Property Name="name" Type="Edm.String"/>
        <Property Name="entity_type" Type="Edm.String"/>
        <Property Name="summary" Type="Edm.String"/>
        <Property Name="group_id" Type="Edm.String"/>
      </EntityType>
      <EntityType Name="Fact">
        <Key><PropertyRef Name="id"/></Key>
        <Property Name="id" Type="Edm.String" Nullable="false"/>
        <Property Name="source" Type="Edm.String"/>
        <Property Name="relationship_type" Type="Edm.String"/>
        <Property Name="target" Type="Edm.String"/>
        <Property Name="fact" Type="Edm.String"/>
        <Property Name="valid_at" Type="Edm.String"/>
        <Property Name="invalid_at" Type="Edm.String"/>
        <Property Name="is_valid" Type="Edm.Boolean"/>
        <Property Name="group_id" Type="Edm.String"/>
      </EntityType>
      <EntityContainer Name="SaxonContainer">
        <EntitySet Name="Entities" EntityType="Saxon.Entity"/>
        <EntitySet Name="Facts" EntityType="Saxon.Fact"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""


@router.get("/$metadata")
def metadata_document():
    return Response(content=_METADATA_XML, media_type="application/xml")


@router.get("/Entities")
def list_entities_odata(
    request: Request,
    top: int | None = None,
    knowledge_base: str | None = None,
    tenant: TenantConfig = Depends(require_tenant),
):
    group_id = resolve_knowledge_base(tenant, knowledge_base)
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    limit = _clamp_top(top)
    rows = repo.execute_cypher(
        """
        MATCH (n:Entity {group_id: $group_id})
        RETURN n.uuid AS id, n.name AS name, n.summary AS summary,
               n.group_id AS group_id,
               [l IN labels(n) WHERE l <> 'Entity'][0] AS entity_type
        ORDER BY n.created_at DESC
        LIMIT $limit
        """,
        {"group_id": group_id, "limit": limit},
    )
    root = _service_root(request)
    return {"@odata.context": f"{root}/$metadata#Entities", "value": rows}


@router.get("/Facts")
def list_facts_odata(
    request: Request,
    top: int | None = None,
    knowledge_base: str | None = None,
    tenant: TenantConfig = Depends(require_tenant),
):
    group_id = resolve_knowledge_base(tenant, knowledge_base)
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    limit = _clamp_top(top)
    rows = repo.execute_cypher(
        """
        MATCH (a:Entity {group_id: $group_id})-[r:RELATES_TO]->(b:Entity {group_id: $group_id})
        RETURN toString(id(r)) AS id, a.name AS source, r.name AS relationship_type, b.name AS target,
               r.fact AS fact, r.valid_at AS valid_at, r.invalid_at AS invalid_at,
               r.expired_at AS expired_at, r.group_id AS group_id
        ORDER BY r.created_at DESC
        LIMIT $limit
        """,
        {"group_id": group_id, "limit": limit},
    )
    for row in rows:
        expired_at = GraphRepository._to_native(row.pop("expired_at"))
        invalid_at = GraphRepository._to_native(row["invalid_at"])
        valid_at = GraphRepository._to_native(row["valid_at"])
        row["is_valid"] = GraphRepository.fact_is_valid(expired_at, invalid_at)
        row["valid_at"] = valid_at.isoformat() if hasattr(valid_at, "isoformat") else valid_at
        row["invalid_at"] = invalid_at.isoformat() if hasattr(invalid_at, "isoformat") else invalid_at
    root = _service_root(request)
    return {"@odata.context": f"{root}/$metadata#Facts", "value": rows}
