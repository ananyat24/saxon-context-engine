# Central place all configuration is read from. Uses pydantic-settings, which reads
# values from environment variables (or a .env file, per SettingsConfigDict below)
# and validates their types the same way Pydantic validates request bodies. Copy
# .env.example to .env and fill in real values -- see the project README for setup.
#
# Nothing else in this codebase should call os.environ.get() directly for these
# values; import `settings` from here instead, so there's exactly one source of
# truth for configuration.
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Neo4j connection details. The defaults match Neo4j Desktop's default local setup.
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    # Maps API keys to the tenant (Graphiti group_id) they're allowed to query --
    # see app/security.py for why this exists and how it's enforced. Set as a JSON
    # object in .env, e.g. TENANT_API_KEYS={"<key>": "<group_id>"}. Empty by
    # default, meaning no API key will be valid until you configure at least one.
    tenant_api_keys: dict[str, str] = Field(default_factory=dict)

    # Google Gemini is used for entity extraction, embeddings, and reranking (see
    # app/graph/graphiti_adapter.py). Get a key from https://aistudio.google.com/.
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
