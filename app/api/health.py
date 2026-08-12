# GET /api/v1/health -- lets a load balancer, uptime monitor, or a developer
# quickly check whether the app can actually reach its Neo4j database.
from fastapi import APIRouter
from app.graph.neo4j_client import check_neo4j_connection

router = APIRouter()


@router.get("")
def get_health():
    neo4j_ok = check_neo4j_connection()
    return {
        "status": "healthy" if neo4j_ok else "degraded",
        "neo4j_connected": neo4j_ok,
    }
