# Run with: python scripts/init_neo4j.py
#
# One-time setup step: asks Graphiti to create the indices and uniqueness
# constraints it needs in Neo4j (so, e.g., it can look up nodes by id quickly and
# reject duplicates). Run this once against a fresh Neo4j database before ingesting
# any data; it's safe to run again later too, it just re-applies the same schema.
#
# `asyncio.run(main())` is how you start an async function from a plain script:
# Graphiti's calls are all `async def`, meaning they can pause while waiting on
# network I/O (talking to Neo4j or to the Gemini API) instead of blocking the whole
# program, but that means they have to be awaited from inside an event loop.
# asyncio.run() sets one up.
import asyncio
import logging
from app.graph.graphiti_adapter import build_graphiti

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    logger.info("Initializing Neo4j constraints and indices via Graphiti...")
    graphiti = build_graphiti()
    try:
        await graphiti.build_indices_and_constraints()
        logger.info("Neo4j indices and constraints created successfully.")
    finally:
        await graphiti.close()


if __name__ == "__main__":
    asyncio.run(main())
