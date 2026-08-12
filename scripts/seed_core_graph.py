# Run with: python scripts/seed_core_graph.py
#
# The smallest possible end-to-end example: ingest one sentence, then ask a question
# about it. Good smoke test to confirm your Neo4j + Gemini setup actually works,
# since it exercises the full path -- LLM extraction, graph write, and search.
import asyncio
from datetime import datetime, timezone
from graphiti_core.nodes import EpisodeType
from app.graph.graphiti_adapter import build_graphiti


async def main():
    graphiti = build_graphiti()

    try:
        await graphiti.build_indices_and_constraints()

        print("--- Seeding quickstart core episode ---")
        await graphiti.add_episode(
            name="seed episode",
            episode_body="Ananya set up the Saxon AI Context Engine with Graphiti and Gemini.",
            source=EpisodeType.text,
            source_description="seed quickstart",
            # reference_time is when the episode's content happened/was true --
            # here, "right now", since we're describing something happening live.
            reference_time=datetime.now(timezone.utc),
        )

        results = await graphiti.search("What did Ananya set up?")
        for r in results:
            print(f"Fact: {r.fact}")
    finally:
        await graphiti.close()


if __name__ == "__main__":
    asyncio.run(main())
