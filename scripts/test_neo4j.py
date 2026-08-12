from app.graph.neo4j_client import Neo4jClient

client = Neo4jClient()
try:
    if client.verify_connection():
        print("Neo4j connection successful")
    else:
        print("Neo4j connection failed")
finally:
    client.close()
