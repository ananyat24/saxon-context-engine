class ContextPlanner:
    """Plans retrieval sub-queries across graph, semantic, and live data sources."""

    def plan_query(self, user_query: str) -> dict:
        return {"sub_queries": [user_query]}
