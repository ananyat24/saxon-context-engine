# Seeds an org hierarchy (:User nodes + :REPORTS_TO edges) and ownership
# assignments (:ASSIGNED_TO edges from existing business entities to the User
# who owns them) for role-based visibility in the "solandra_supply_chain"
# knowledge base. See scripts/seed_roles.py (the contoso_dw equivalent this
# mirrors) and app/graph/authorization.py for why this is written directly
# via Cypher rather than through Graphiti's LLM extraction.
#
# The org chart below isn't in the source CSVs -- data/uploads' people.csv
# only has name/team/title/location, no manager column -- so the reporting
# lines are a reasonable inference from title and team, not extracted data:
# Daniel Reyes (VP of Operations) at the top; the two plant leads, the
# quality engineer, the procurement lead, the senior account manager, and
# the financial controller reporting to him directly; the plant 2 line
# supervisor reporting to the plant 2 lead; the account manager reporting to
# the senior account manager.
#
# Assignments are seeded from orders.csv's account_owner column, matched
# against whichever order entities Graphiti has actually extracted so far.
# Ownership changes across the day1/day2/day3 snapshots (see that CSV's own
# history), so re-run this after each day's sync to keep assignments current
# -- MERGE makes it safe to run repeatedly, and a later run's ownership
# simply adds any new ASSIGNED_TO edge rather than removing a stale one, the
# same "additive, never destructive" tradeoff scripts/seed_roles.py makes.
#
# Usage:
#   python scripts/seed_roles_solandra.py
import logging

from app.graph import authorization
from app.graph.graph_repository import GraphRepository

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

GROUP_ID = "solandra_supply_chain"

# id, name, role, manager_id (None for the top of the chart)
USERS = [
    ("daniel_reyes", "Daniel Reyes", "VP of Operations", None),
    ("renee_kapoor", "Renee Kapoor", "Plant 2 Operations Lead", "daniel_reyes"),
    ("angela_brooks", "Angela Brooks", "Plant 1 Operations Lead", "daniel_reyes"),
    ("carlos_jimenez", "Carlos Jimenez", "Line Supervisor", "renee_kapoor"),
    ("yusuf_demir", "Yusuf Demir", "Quality Engineer", "daniel_reyes"),
    ("owen_whitfield", "Owen Whitfield", "Procurement Lead", "daniel_reyes"),
    ("priya_nadeem", "Priya Nadeem", "Senior Account Manager", "daniel_reyes"),
    ("diego_alvarez", "Diego Alvarez", "Account Manager", "priya_nadeem"),
    ("grace_okafor", "Grace Okafor", "Financial Controller", "daniel_reyes"),
]

# order code -> user_id, from data/uploads' orders.csv account_owner column.
# Matched against order entities by substring (Graphiti has extracted the
# same order under names like "SO-45821" and "Order SO-45822" depending on
# which episode introduced it first), not by exact name.
ORDER_OWNERS = {
    "SO-45821": "owen_whitfield",
    "SO-45822": "priya_nadeem",
    "SO-45890": "priya_nadeem",
}


def seed_users(repo: GraphRepository) -> None:
    for user_id, name, role, manager_id in USERS:
        repo.execute_cypher(
            """
            MERGE (u:User {group_id: $group_id, id: $id})
            SET u.name = $name, u.role = $role
            """,
            {"group_id": GROUP_ID, "id": user_id, "name": name, "role": role},
        )
        logger.info(f"Upserted user '{name}' ({role})")

    for user_id, _name, _role, manager_id in USERS:
        if manager_id is None:
            continue
        repo.execute_cypher(
            """
            MATCH (u:User {group_id: $group_id, id: $id})
            MATCH (m:User {group_id: $group_id, id: $manager_id})
            MERGE (u)-[:REPORTS_TO]->(m)
            """,
            {"group_id": GROUP_ID, "id": user_id, "manager_id": manager_id},
        )
        logger.info(f"  {user_id} reports to {manager_id}")


def seed_assignments(repo: GraphRepository) -> None:
    for order_code, user_id in ORDER_OWNERS.items():
        rows = repo.execute_cypher(
            """
            MATCH (n:Entity {group_id: $group_id})
            WHERE n.name CONTAINS $order_code
            MATCH (u:User {group_id: $group_id, id: $user_id})
            MERGE (n)-[:ASSIGNED_TO]->(u)
            RETURN n.uuid AS uuid, n.name AS name
            """,
            {"group_id": GROUP_ID, "order_code": order_code, "user_id": user_id},
        )
        if rows:
            for row in rows:
                logger.info(f"Assigned '{row['name']}' to {user_id}")
        else:
            logger.warning(f"No entity matching '{order_code}' found in group '{GROUP_ID}' -- skipped")


def main() -> None:
    repo = GraphRepository()
    authorization.ensure_authorization_indexes(repo)
    seed_users(repo)
    seed_assignments(repo)
    logger.info("\nDone. Restart the API (or just re-query) to see role-based visibility take effect.")


if __name__ == "__main__":
    main()
