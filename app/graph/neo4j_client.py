import logging
from typing import Any, Dict, List, Optional
from neo4j import GraphDatabase, Driver, AsyncGraphDatabase, AsyncDriver
from app.config import settings

logger = logging.getLogger(__name__)


class Neo4jClient:
    def __init__(self):
        self.driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(
                settings.neo4j_user,
                settings.neo4j_password
            )
        )

    def verify_connection(self) -> bool:
        self.driver.verify_connectivity()
        return True

    def close(self) -> None:
        self.driver.close()


def get_neo4j_driver() -> Driver:
    """Return a synchronous Neo4j driver using global settings."""
    return GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )


def check_neo4j_connection() -> bool:
    """Verify Neo4j database connectivity."""
    try:
        client = Neo4jClient()
        connected = client.verify_connection()
        client.close()
        return connected
    except Exception as e:
        logger.error(f"Neo4j connection health check failed: {e}")
        return False
