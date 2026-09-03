# Seeds an org hierarchy (:User nodes + :REPORTS_TO edges) and ownership
# assignments (:ASSIGNED_TO edges from existing business entities to the User
# who owns them) for role-based visibility. See app/graph/authorization.py
# for how those get enforced at query time, and why this is written directly
# via Cypher rather than through Graphiti's LLM extraction: who-reports-to-
# whom and who-owns-what is exact organizational data, the kind a real
# deployment would get from an HR/CRM sync or an admin action, not something
# to have an LLM infer from text.
#
# This script is intentionally specific to the "contoso_dw" knowledge base's
# actual, already-ingested customers/sales (matched by name, reliable here
# because this demo dataset is small and has no name collisions; a larger
# real dataset should instead assign ownership at ingestion time, when the
# source record's own key still uniquely identifies which entity Graphiti's
# add_episode() just created, rather than re-matching by name afterward).
#
# Usage:
#   python scripts/seed_roles.py
import logging

from app.graph import authorization
from app.graph.graph_repository import GraphRepository

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

GROUP_ID = "contoso_dw"

# id, name, role, manager_id (None for the top of the chart)
USERS = [
    ("jordan_blake", "Jordan Blake", "Chief Revenue Officer", None),
    ("morgan_reyes", "Morgan Reyes", "Americas Sales Manager", "jordan_blake"),
    ("casey_nguyen", "Casey Nguyen", "EMEA/APAC Sales Manager", "jordan_blake"),
    ("diego_ramirez", "Diego Ramirez", "Americas Sales Rep", "morgan_reyes"),
    ("priya_shah", "Priya Shah", "EMEA Sales Rep", "casey_nguyen"),
    ("liam_oconnor", "Liam O'Connor", "APAC Sales Rep", "casey_nguyen"),
]

# Which already-ingested entities (by their Graphiti-assigned name) each rep
# owns. Matched against data/samples/contoso_dw's own CustomerKey -> Country
# mapping for the specific sales sample scripts/ingest_samples.py ingested.
# See that file's CONTOSO_SALES_SAMPLE for which records these are. Two
# expected records (customer "Virgil Blevins" and "Sale 103300") aren't
# assigned here because they were marked ingested but never actually produced
# a node, a separate Graphiti extraction gap, not a bug in this script.
ASSIGNMENTS = {
    "diego_ramirez": ["Karen Dorman", "Adassa Cavazos", "Sale 109300", "Sale 114900"],
    "priya_shah": ["Angelika Kuster", "Elisabetta Marcelo", "Sale 117000", "Sale 112401"],
    "liam_oconnor": ["George Nicholas", "Sale 87200"],
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
    for user_id, entity_names in ASSIGNMENTS.items():
        for entity_name in entity_names:
            rows = repo.execute_cypher(
                """
                MATCH (n:Entity {group_id: $group_id, name: $name})
                MATCH (u:User {group_id: $group_id, id: $user_id})
                MERGE (n)-[:ASSIGNED_TO]->(u)
                RETURN n.uuid AS uuid
                """,
                {"group_id": GROUP_ID, "name": entity_name, "user_id": user_id},
            )
            if rows:
                logger.info(f"Assigned '{entity_name}' to {user_id}")
            else:
                logger.warning(f"No entity named '{entity_name}' found in group '{GROUP_ID}' -- skipped")


def main() -> None:
    repo = GraphRepository()
    authorization.ensure_authorization_indexes(repo)
    seed_users(repo)
    seed_assignments(repo)
    logger.info("\nDone. Restart the API (or just re-query) to see role-based visibility take effect.")


if __name__ == "__main__":
    main()
