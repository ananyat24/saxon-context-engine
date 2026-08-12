# These two tests need a real, reachable Neo4j database (whatever .env points at) --
# they aren't mocked. If Neo4j isn't running, both will fail with a connection error;
# that's expected, not a bug in the test itself. See README.md for how to start Neo4j.
from app.graph.neo4j_client import Neo4jClient
from app.graph.graph_repository import GraphRepository


def test_neo4j_client_connection():
    client = Neo4jClient()
    try:
        assert client.verify_connection() is True
    finally:
        client.close()


def test_graph_repository_cypher():
    # "RETURN 1 AS test_val" is a trivial Cypher query (Neo4j's query language) that
    # doesn't touch any stored data -- it just confirms a query round-trip works.
    repo = GraphRepository()
    res = repo.execute_cypher("RETURN 1 AS test_val")
    assert len(res) == 1
    assert res[0]["test_val"] == 1
