# Enforces a local, app-side cost ceiling on paid-LLM-provider usage (Azure
# OpenAI and/or Anthropic -- see app/graph/graphiti_adapter.py for how each
# provider wraps its client to call into this).
#
# This exists because for Azure OpenAI specifically, we only hold an API key
# for the resource, not the Azure portal access needed to set a hard spending
# cap on the resource itself (that has to come from whoever provisioned it --
# a deployment-level TPM rate limit is the real hard limit; this is a
# backstop on our side in the meantime). For Anthropic, this is just a
# straightforward local safety net on top of whatever limit the Anthropic
# Console account itself has configured.
#
# Tracks two independent budgets so a runaway ingestion run can't eat into
# the query-testing budget or vice versa:
#   - "ingestion": scripts/ingest_samples.py, scripts/seed_core_graph.py
#   - "query": the live /api/v1/context/query path (app/api/context.py)
#
# Cost is *estimated* from token usage x a per-token price the caller
# supplies (see app/config.py's azure_openai_*_price_per_1m and
# anthropic_*_price_per_1m) -- it only matches the provider's actual invoice
# if those prices are kept in sync with whatever model is really in use.
# That's fine for what this is for: catching a runaway loop or bug well
# before it becomes a real bill, not exact billing reconciliation.
#
# State is a plain JSON file (same pattern as app/ingestion/ingest_log.py),
# so the running total survives process restarts and can be inspected/reset
# by hand.
import json
import logging
from pathlib import Path
from threading import Lock

from app.config import settings

logger = logging.getLogger(__name__)

STATE_PATH = Path("data/processed/azure_openai_spend.json")


class SpendLimitExceeded(RuntimeError):
    """Raised instead of making a paid-provider LLM call once a bucket's budget is used up."""


class SpendLimiter:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self._lock = Lock()
        self._spent: dict[str, float] = {}
        if path.exists():
            self._spent = json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _budget_for(bucket: str) -> float:
        return (
            settings.azure_openai_ingestion_budget_usd
            if bucket == "ingestion"
            else settings.azure_openai_query_budget_usd
        )

    def spent(self, bucket: str) -> float:
        return self._spent.get(bucket, 0.0)

    def ensure_room(self, bucket: str) -> None:
        """Called before a paid-provider LLM call is made (whichever provider
        is active -- see app/graph/graphiti_adapter.py). Raises if this
        bucket is already at or over budget, so the over-budget call itself
        never goes out."""
        spent, budget = self.spent(bucket), self._budget_for(bucket)
        if spent >= budget:
            raise SpendLimitExceeded(
                f"Local '{bucket}' spend budget of ${budget:.2f} reached "
                f"(${spent:.2f} spent so far, estimated). Raise "
                f"azure_openai_{bucket}_budget_usd in .env to continue."
            )

    def record(self, bucket: str, cost_usd: float) -> float:
        with self._lock:
            total = self._spent.get(bucket, 0.0) + cost_usd
            self._spent[bucket] = total
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._spent, indent=2) + "\n", encoding="utf-8")
        budget = self._budget_for(bucket)
        if total >= budget:
            logger.warning(f"Local '{bucket}' spend budget of ${budget:.2f} reached (${total:.2f} spent, estimated).")
        return total


def estimate_cost_usd(
    prompt_tokens: int, completion_tokens: int, price_per_1m_input: float, price_per_1m_output: float = 0.0
) -> float:
    """Provider-agnostic: the caller supplies whichever provider/price applies
    (see app/graph/graphiti_adapter.py's per-provider wrappers) -- this
    function has no opinion on which provider or model is in use. For an
    embedding call, pass price_per_1m_output=0.0 (embeddings have no
    completion tokens)."""
    return (
        prompt_tokens / 1_000_000 * price_per_1m_input
        + completion_tokens / 1_000_000 * price_per_1m_output
    )


# One process-wide limiter (like app/config.py's `settings`) so every call
# site shares the same running totals rather than each keeping its own.
_limiter = SpendLimiter()


def get_limiter() -> SpendLimiter:
    return _limiter
