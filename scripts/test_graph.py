# Run with: python scripts/test_graph.py
#
# Demonstrates the feature that makes Graphiti more than "just" a graph database:
# tracking facts over time. This simulates two systems (a CRM and an ERP) both
# writing about the same customer, then a later CRM update that changes who manages
# the account. The final query shows Graphiti automatically marking the old
# "Sarah Chen manages this account" fact as INVALIDATED once the new one
# ("Marcus Lee manages this account") is ingested. It doesn't just overwrite the
# old fact, it keeps both and marks which one is currently true.
#
# time.sleep(15) calls exist only because Gemini's free tier caps requests per
# minute (check current limits at ai.google.dev/pricing, they change over time).
# Remove them if you're on a paid tier with a higher rate limit.
import asyncio
import time
from datetime import datetime, timedelta, timezone
from graphiti_core.nodes import EpisodeType
from app.graph.graphiti_adapter import build_graphiti

# Graphiti's "group_id" scopes data to a logical bucket (e.g. one customer's data
# in a multi-tenant deployment) so a search can be limited to just that bucket.
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
            # A fact with an expired_at or invalid_at timestamp has been superseded
            # by a newer fact. Graphiti keeps it in the graph as history rather
            # than deleting it, it just stops treating it as currently true.
            valid = "VALID" if r.expired_at is None and r.invalid_at is None else "INVALIDATED"
            print(f"  [{valid}] {r.fact}")

    finally:
        await graphiti.close()


if __name__ == "__main__":
    asyncio.run(main())
