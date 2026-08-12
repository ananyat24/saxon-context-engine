# GET /api/v1/health -- lets a load balancer, uptime monitor, or a developer
# quickly check whether the app can actually reach its database. The response
# says "database_connected" rather than naming Neo4j specifically -- that's an
# implementation detail, not something a caller of this API needs to know.
from fastapi import APIRouter
from app.graph.neo4j_client import check_neo4j_connection

router = APIRouter()


@router.get("")
def get_health():
    db_ok = check_neo4j_connection()
    return {
        "status": "healthy" if db_ok else "degraded",
        "database_connected": db_ok,
    }
