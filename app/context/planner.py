# Placeholder for query planning: breaking one user question into several targeted
# sub-queries before retrieval runs (e.g. splitting "what's Contoso's order history
# and who's their account rep" into two separate lookups). Currently a no-op that
# just wraps the original query in a single-item list, so the rest of the context
# pipeline has a stable interface to build against before real planning logic exists.
class ContextPlanner:
    """Plans retrieval sub-queries across graph, semantic, and live data sources."""

    def plan_query(self, user_query: str) -> dict:
        return {"sub_queries": [user_query]}
