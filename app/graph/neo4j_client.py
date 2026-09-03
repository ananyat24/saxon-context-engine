# Thin wrapper around the official Neo4j Python driver. Neo4j is the graph database
# this project stores everything in; a "driver" is the connection-pool object the
# neo4j package gives you to run queries against it. Nothing here is specific to
# Graphiti or to this project's ontology; this is just plumbing.
import logging
from neo4j import GraphDatabase, Driver
from app.config import settings

logger = logging.getLogger(__name__)


class Neo4jClient:
    """Owns a Neo4j driver instance and its connection lifecycle.

    Create one, use `.driver` to open sessions and run queries, and call
    `.close()` when you're done with it (or use it in a try/finally, as the
    scripts under scripts/ do)."""

    def __init__(self) -> None:
        self.driver: Driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )

    def verify_connection(self) -> bool:
        """Ping the database. Raises if it's unreachable rather than returning False,
        so callers that want a boolean should go through check_neo4j_connection() below."""
        self.driver.verify_connectivity()
        return True

    def close(self) -> None:
        self.driver.close()


def check_neo4j_connection() -> bool:
    """Best-effort health check: True if Neo4j is reachable, False otherwise
    (never raises). Used by the /health API endpoint."""
    try:
        client = Neo4jClient()
        connected = client.verify_connection()
        client.close()
        return connected
    except Exception as e:
        logger.error(f"Neo4j connection health check failed: {e}")
        return False
