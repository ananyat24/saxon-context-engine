import asyncio
import time
from datetime import datetime, timedelta, timezone
from graphiti_core.nodes import EpisodeType
from app.graph.graphiti_adapter import build_graphiti

GROUP_ID = "acme_demo"
now = datetime.now(timezone.utc)


async def main():
    graphiti = build_graphiti()

    try:
        await graphiti.build_indices_and_constraints()

        print("--- Ingesting from CRM ---")
        await graphiti.add_episode(
            name="crm-contoso-account",
            episode_body=(
                "Contoso Ltd is an enterprise customer. "
                "The account is managed by Sarah Chen (sarah.chen@ourcompany.com). "
                "Deal stage: Closed Won."
            ),
            source=EpisodeType.text,
            source_description="CRM export",
            reference_time=now - timedelta(days=10),
            group_id=GROUP_ID,
        )

        time.sleep(15)  # stay under free-tier Gemini rate limit
        print("--- Ingesting from ERP ---")
        await graphiti.add_episode(
            name="erp-contoso-order",
            episode_body=(
                "Order #4521 for Contoso Ltd shipped on 2026-08-01: "
                "50 units of Widget-X, total value $12,500."
            ),
            source=EpisodeType.text,
            source_description="ERP export",
            reference_time=now - timedelta(days=5),
            group_id=GROUP_ID,
        )

        time.sleep(15)
        print("--- Query across both systems ---")
        results = await graphiti.search("What do we know about Contoso Ltd?", group_ids=[GROUP_ID])
        for r in results:
            print(f"  [{r.source_node_uuid[:8]}] {r.fact}")

        time.sleep(15)
        print("\n--- CRM update: account rep changes ---")
        await graphiti.add_episode(
            name="crm-contoso-rep-change",
            episode_body="Contoso Ltd's account is now managed by Marcus Lee, not Sarah Chen.",
            source=EpisodeType.text,
            source_description="CRM export",
            reference_time=now,
            group_id=GROUP_ID,
        )

        time.sleep(15)
        print("--- Query again: check account rep status ---")
        results = await graphiti.search("Who manages the Contoso Ltd account?", group_ids=[GROUP_ID])
        for r in results:
            valid = "VALID" if r.expired_at is None and r.invalid_at is None else "INVALIDATED"
            print(f"  [{valid}] {r.fact}")

    finally:
        await graphiti.close()


if __name__ == "__main__":
    asyncio.run(main())
