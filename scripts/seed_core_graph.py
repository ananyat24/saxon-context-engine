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
            episode_body="Ananya set up the AIssist Context Engine with Graphiti and Gemini.",
            source=EpisodeType.text,
            source_description="seed quickstart",
            reference_time=datetime.now(timezone.utc),
        )

        results = await graphiti.search("What did Ananya set up?")
        for r in results:
            print(f"Fact: {r.fact}")
    finally:
        await graphiti.close()


if __name__ == "__main__":
    asyncio.run(main())
