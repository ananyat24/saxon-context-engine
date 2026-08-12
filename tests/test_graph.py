from app.graph.neo4j_client import Neo4jClient
from app.graph.graph_repository import GraphRepository


def test_neo4j_client_connection():
    client = Neo4jClient()
    try:
        assert client.verify_connection() is True
    finally:
        client.close()


def test_graph_repository_cypher():
    repo = GraphRepository()
    res = repo.execute_cypher("RETURN 1 AS test_val")
    assert len(res) == 1
    assert res[0]["test_val"] == 1
