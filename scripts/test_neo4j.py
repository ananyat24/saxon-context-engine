# Run with: python scripts/test_neo4j.py
#
# The simplest possible check that this project can reach the Neo4j database
# configured in .env: no Graphiti, no LLM calls, just "can we open a connection".
# Good first thing to run after setting up .env, before trying anything more
# involved (see the "Graphiti + Gemini" scripts below it in this folder).
from app.graph.neo4j_client import Neo4jClient

client = Neo4jClient()
try:
    if client.verify_connection():
        print("Neo4j connection successful")
    else:
        print("Neo4j connection failed")
finally:
    # Always release the connection, even if verify_connection() raised.
    client.close()
