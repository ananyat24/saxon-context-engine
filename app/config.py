# Central place all configuration is read from. Uses pydantic-settings, which reads
# values from environment variables (or a .env file, per SettingsConfigDict below)
# and validates their types the same way Pydantic validates request bodies. Copy
# .env.example to .env and fill in real values -- see the project README for setup.
#
# Nothing else in this codebase should call os.environ.get() directly for these
# values; import `settings` from here instead, so there's exactly one source of
# truth for configuration.
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
    # Set as a JSON object in .env, e.g.:
    #   TENANT_API_KEYS={"<api-key>": {"group_id": "<tenant>", "gemini_api_key": "<their-key>"}}
    # Empty by default, meaning no API key will be valid until you configure at least one.
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


# Built once at import time and shared everywhere -- re-reading environment
# variables/the .env file on every access would be pointless, since they don't
# change while the process is running.
settings = Settings()
