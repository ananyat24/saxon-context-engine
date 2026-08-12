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
