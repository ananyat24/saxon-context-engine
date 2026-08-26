# Answers a different question than scripts/ingest_samples.py does: not "does
# extraction work," but "does query-time code stay fast once the graph is much
# bigger than our demo datasets." Seeds a synthetic knowledge base directly via
# Cypher -- no LLM calls, no cost, no external RDBMS needed -- at a scale
# meant to resemble a real production graph, then times the exact query paths
# app/graph/graph_repository.py and app/graph/authorization.py use: named-entity
# resolution, role-based visibility, and the two-entity relationship path.
#
# This is deliberately separate from testing real RDBMS ingestion (a different,
# LLM-cost-bound question -- see scripts/ingest_from_postgres.py) so query-time
# scaling can be checked for free, as often as needed, before spending anything
# on a bigger ingest.
#
# Usage:
#   python scripts/load_test_query_scale.py                       # default: 100k entities
#   python scripts/load_test_query_scale.py --entities 500000 --edges 1500000 --users 5000
#   python scripts/load_test_query_scale.py --keep                # skip cleanup, inspect in Neo4j Browser
import argparse
import asyncio
import random
import string
import time
from contextlib import contextmanager

from app.graph import authorization
from app.graph.graph_repository import GraphRepository
from app.graph.neo4j_client import Neo4jClient

GROUP_ID = "loadtest_synthetic"
BATCH_SIZE = 2000


@contextmanager
def timed(label: str):
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    flag = "  <-- SLOW" if elapsed > 0.5 else ""
    print(f"  {label}: {elapsed*1000:.1f} ms{flag}")


def _random_name(prefix: str, i: int) -> str:
    # A trailing random suffix keeps names non-sequential-looking, closer to
    # real entity names, without affecting how many are unique (every name is
    # still unique by index).
    suffix = "".join(random.choices(string.ascii_uppercase, k=3))
    return f"{prefix} {i} {suffix}"


def seed_entities(repo: GraphRepository, n_entities: int) -> None:
    print(f"Seeding {n_entities:,} Entity nodes...")
    for start in range(0, n_entities, BATCH_SIZE):
        batch = [
            {"uuid": f"lt-entity-{i}", "name": _random_name("Synthetic Store", i),
             "summary": f"Synthetic Store {i} is a load-test entity with no real-world meaning."}
            for i in range(start, min(start + BATCH_SIZE, n_entities))
        ]
        repo.execute_cypher(
            """
            UNWIND $rows AS row
            CREATE (n:Entity {group_id: $group_id, uuid: row.uuid, name: row.name, summary: row.summary})
            """,
            {"group_id": GROUP_ID, "rows": batch},
        )
        print(f"  {min(start + BATCH_SIZE, n_entities):,}/{n_entities:,}", end="\r")
    print()


def seed_edges(repo: GraphRepository, n_entities: int, n_edges: int) -> None:
    print(f"Seeding {n_edges:,} RELATES_TO edges...")
    for start in range(0, n_edges, BATCH_SIZE):
        batch = []
        for i in range(start, min(start + BATCH_SIZE, n_edges)):
            a = random.randrange(n_entities)
            b = random.randrange(n_entities)
            batch.append({
                "uuid": f"lt-edge-{i}",
                "a": f"lt-entity-{a}",
                "b": f"lt-entity-{b}",
                "fact": f"Synthetic Store {a} is related to Synthetic Store {b} (load-test edge).",
            })
        repo.execute_cypher(
            """
            UNWIND $rows AS row
            MATCH (a:Entity {group_id: $group_id, uuid: row.a})
            MATCH (b:Entity {group_id: $group_id, uuid: row.b})
            CREATE (a)-[:RELATES_TO {
                uuid: row.uuid, group_id: $group_id, fact: row.fact,
                valid_at: datetime(), invalid_at: null, expired_at: null
            }]->(b)
            """,
            {"group_id": GROUP_ID, "rows": batch},
        )
        print(f"  {min(start + BATCH_SIZE, n_edges):,}/{n_edges:,}", end="\r")
    print()


def seed_org_and_assignments(repo: GraphRepository, n_entities: int, n_users: int) -> tuple[str, str]:
    """Builds a branching org chart (branching factor ~6) and assigns every
    entity to a random user, so visibility-set sizes vary realistically instead
    of every user seeing either everything or nothing. Returns (root_user_id,
    a_leaf_user_id) for the visibility timing below."""
    print(f"Seeding {n_users:,} User nodes with a branching org chart...")
    branching = 6
    users = [{"id": "lt-user-0", "manager_id": None}]
    for i in range(1, n_users):
        manager_idx = (i - 1) // branching
        users.append({"id": f"lt-user-{i}", "manager_id": f"lt-user-{manager_idx}"})

    for start in range(0, len(users), BATCH_SIZE):
        batch = users[start:start + BATCH_SIZE]
        repo.execute_cypher(
            "UNWIND $rows AS row CREATE (u:User {group_id: $group_id, id: row.id, name: row.id})",
            {"group_id": GROUP_ID, "rows": batch},
        )
    for start in range(0, len(users), BATCH_SIZE):
        batch = [u for u in users[start:start + BATCH_SIZE] if u["manager_id"]]
        if batch:
            repo.execute_cypher(
                """
                UNWIND $rows AS row
                MATCH (u:User {group_id: $group_id, id: row.id})
                MATCH (m:User {group_id: $group_id, id: row.manager_id})
                CREATE (u)-[:REPORTS_TO]->(m)
                """,
                {"group_id": GROUP_ID, "rows": batch},
            )

    print(f"Assigning {n_entities:,} entities to random users...")
    for start in range(0, n_entities, BATCH_SIZE):
        batch = [
            {"entity": f"lt-entity-{i}", "user": f"lt-user-{random.randrange(n_users)}"}
            for i in range(start, min(start + BATCH_SIZE, n_entities))
        ]
        repo.execute_cypher(
            """
            UNWIND $rows AS row
            MATCH (n:Entity {group_id: $group_id, uuid: row.entity})
            MATCH (u:User {group_id: $group_id, id: row.user})
            CREATE (n)-[:ASSIGNED_TO]->(u)
            """,
            {"group_id": GROUP_ID, "rows": batch},
        )

    leaf_id = f"lt-user-{n_users - 1}"
    return "lt-user-0", leaf_id


def cleanup(repo: GraphRepository) -> None:
    print("Cleaning up synthetic data...")
    while True:
        rows = repo.execute_cypher(
            "MATCH (n {group_id: $group_id}) WITH n LIMIT 10000 DETACH DELETE n RETURN count(n) AS c",
            {"group_id": GROUP_ID},
        )
        if not rows or rows[0]["c"] == 0:
            break


def warm_up(repo: GraphRepository, n_entities: int, root_user: str, leaf_user: str) -> None:
    """Neo4j compiles and caches a query plan the first time it sees a given
    Cypher *shape* (the literal query string, independent of parameter values)
    -- that compilation costs ~200-300ms locally, but only once per shape per
    server lifetime; every later call with different parameters reuses the
    cached plan and costs ~10ms. A real server pays this exactly once, at
    startup or on first use, then stays warm for its whole uptime -- so
    without a warm-up pass here, the timings below would mostly measure a
    one-time compilation cost, not the steady-state, data-scale-dependent cost
    they're meant to reveal.
    """
    print("Warming up query plan cache (compiles each query shape once)...")
    asyncio.run(repo._resolve_named_entities("What do we know about Warmup Placeholder?", [GROUP_ID], None))
    repo._relationship_path_facts("lt-entity-0", "lt-entity-1", None)
    authorization.get_visible_entity_uuids(GROUP_ID, root_user, repo=repo)
    authorization.get_visible_entity_uuids(GROUP_ID, leaf_user, repo=repo)


def run_timings(repo: GraphRepository, n_entities: int, root_user: str, leaf_user: str) -> None:
    warm_up(repo, n_entities, root_user, leaf_user)
    print("\n--- Query-time results (steady-state, after warm-up) ---")

    target_uuid = f"lt-entity-{n_entities // 2}"
    target_name = repo.execute_cypher(
        "MATCH (n:Entity {group_id: $group_id, uuid: $uuid}) RETURN n.name AS name",
        {"group_id": GROUP_ID, "uuid": target_uuid},
    )[0]["name"]

    with timed("Exact-name entity resolution (indexed equality match)"):
        asyncio.run(repo._resolve_named_entities(f"What do we know about {target_name}?", [GROUP_ID], None))

    with timed("Entity resolution for a name with no match (worst case: falls through to CONTAINS scan)"):
        asyncio.run(repo._resolve_named_entities("What do we know about Totally Nonexistent Widget?", [GROUP_ID], None))

    a, b = random.randrange(n_entities), random.randrange(n_entities)
    with timed(f"Relationship path between two random entities (bounded 4 hops)"):
        repo._relationship_path_facts(f"lt-entity-{a}", f"lt-entity-{b}", None)

    with timed("RBAC visible-entity-uuids for the ORG ROOT (sees everyone)"):
        root_visible = authorization.get_visible_entity_uuids(GROUP_ID, root_user, repo=repo)
    print(f"    -> {len(root_visible):,} entities visible")

    with timed("RBAC visible-entity-uuids for a LEAF user (sees only themself)"):
        leaf_visible = authorization.get_visible_entity_uuids(GROUP_ID, leaf_user, repo=repo)
    print(f"    -> {len(leaf_visible):,} entities visible")

    with timed("Full single-entity resolved-query path (resolution + own-edges + RBAC filter)"):
        resolved, _ = asyncio.run(
            repo._resolve_named_entities(f"What do we know about Synthetic Store {a}?", [GROUP_ID], root_visible)
        )
        if resolved:
            repo._entity_own_facts(resolved[0][0]["uuid"], root_visible)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entities", type=int, default=100_000)
    parser.add_argument("--edges", type=int, default=300_000)
    parser.add_argument("--users", type=int, default=2_000)
    parser.add_argument("--keep", action="store_true", help="Skip cleanup so you can inspect the data afterward.")
    parser.add_argument("--skip-seed", action="store_true", help="Reuse data already seeded by a previous run.")
    args = parser.parse_args()

    # A shared client (one driver/connection pool for the whole run) so these
    # timings reflect real per-request Cypher cost, not connection setup --
    # this matches how the app itself now shares one Neo4jClient across a
    # request via app.state (see app/main.py) rather than opening a fresh
    # driver on every single Cypher call, which is what GraphRepository()
    # with no client falls back to.
    neo4j_client = Neo4jClient()
    repo = GraphRepository(neo4j_client=neo4j_client)
    authorization.ensure_authorization_indexes(repo)

    if not args.skip_seed:
        start = time.perf_counter()
        seed_entities(repo, args.entities)
        seed_edges(repo, args.entities, args.edges)
        root_user, leaf_user = seed_org_and_assignments(repo, args.entities, args.users)
        print(f"Seeding took {time.perf_counter() - start:.1f}s total.\n")
    else:
        root_user, leaf_user = "lt-user-0", f"lt-user-{args.users - 1}"

    run_timings(repo, args.entities, root_user, leaf_user)

    if args.keep:
        print(f"\n--keep set: synthetic data left in group_id='{GROUP_ID}'. "
              f"Re-run with --skip-seed to time again, or clean up manually later.")
    else:
        cleanup(repo)
        print("Done -- synthetic data removed.")

    neo4j_client.close()


if __name__ == "__main__":
    main()
