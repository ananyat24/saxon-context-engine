# Central place all configuration is read from. Uses pydantic-settings, which reads
# values from environment variables (or a .env file, per SettingsConfigDict below)
# and validates their types the same way Pydantic validates request bodies. Copy
# .env.example to .env and fill in real values -- see the project README for setup.
#
# Nothing else in this codebase should call os.environ.get() directly for these
# values; import `settings` from here instead, so there's exactly one source of
# truth for configuration.
import json
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TenantConfig(BaseModel):
    """One client's identity and their own Gemini API key.

    Each tenant brings their own Gemini key rather than sharing the operator's --
    see app/graph/tenant_graphiti_pool.py for why, and app/security.py for how a
    request gets matched to one of these via its API key.
    """

    group_id: str
    gemini_api_key: str


# Where tenants are managed day-to-day: `python scripts/manage_tenants.py add/list/
# remove` reads and writes this file, so onboarding a client's API key never
# requires editing code or hand-writing JSON. Gitignored -- config/tenants.example.json
# is the committed template. Falls back to the TENANT_API_KEYS environment variable
# (see below) if this file doesn't exist, for deployments that prefer configuring
# secrets through their hosting platform's environment variables instead of a file
# (e.g. Azure Container Apps, see docs/internal/infrastructure-plan.md).
TENANT_CONFIG_PATH = Path("config/tenants.json")


def _load_tenants_from_file(path: Path) -> dict[str, TenantConfig] | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {api_key: TenantConfig(**cfg) for api_key, cfg in raw.items()}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Neo4j connection details. The defaults match Neo4j Desktop's default local setup.
    # Shared across all tenants -- Neo4j Community Edition (what this project runs
    # on) doesn't support a separate database per tenant, so tenant isolation
    # happens via group_id (see app/security.py) rather than separate credentials here.
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    # Maps API keys to a TenantConfig (their group_id + their own Gemini key).
    # Normally populated from config/tenants.json (see TENANT_CONFIG_PATH above),
    # not from this field directly -- but it can also be set as a JSON object in
    # .env for platforms that prefer environment-variable configuration, e.g.:
    #   TENANT_API_KEYS={"<api-key>": {"group_id": "<tenant>", "gemini_api_key": "<their-key>"}}
    # Empty by default, meaning no API key will be valid until at least one tenant
    # is added (via the script, or this variable).
    tenant_api_keys: dict[str, TenantConfig] = Field(default_factory=dict)

    # Fallback Gemini key used only by local scripts/tests (scripts/*.py) that run
    # outside the multi-tenant API and don't have a TenantConfig to draw from. The
    # API itself never falls back to this -- every /context/query request uses the
    # calling tenant's own key, never this one. Get a key from https://aistudio.google.com/.
    google_api_key: str = ""
    llm_model: str = "gemini-flash-lite-latest"
    small_llm_model: str = "gemini-flash-lite-latest"
    embedding_model: str = "gemini-embedding-001"

    app_env: str = "development"
    log_level: str = "INFO"

    def model_post_init(self, __context) -> None:
        # config/tenants.json, if present, is the source of truth over the
        # TENANT_API_KEYS environment variable -- it's the one the management
        # script edits, and a file a person can inspect/edit beats a JSON blob
        # crammed into a .env line.
        from_file = _load_tenants_from_file(TENANT_CONFIG_PATH)
        if from_file is not None:
            self.tenant_api_keys = from_file


# Built once at import time and shared everywhere -- re-reading environment
# variables/the .env file on every access would be pointless, since they don't
# change while the process is running. This does mean the app must be restarted
# to pick up a tenant added while it's already running.
settings = Settings()
