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


class KnowledgeBase(BaseModel):
    """One selectable dataset within a tenant, e.g. "Northwind" or "Contoso Data
    Warehouse". `id` is the group_id used to scope Neo4j queries and Graphiti
    search (see app/security.py's resolve_knowledge_base); `label` is what a
    client shows in a picker."""

    id: str
    label: str


class TenantConfig(BaseModel):
    """One client's identity, their own Gemini API key, and the knowledge bases
    (datasets) they're allowed to query.

    Each tenant brings their own Gemini key rather than sharing the operator's --
    see app/graph/tenant_graphiti_pool.py for why, and app/security.py for how a
    request gets matched to one of these via its API key.

    A tenant can have more than one knowledge base (multiple group_ids under one
    Gemini key/client) so a client can switch between datasets without needing a
    separate API key per dataset. Every request still must name one of *this*
    tenant's own knowledge bases -- see app/security.py's resolve_knowledge_base --
    so a client can never reach a group_id outside its own list.
    """

    tenant_id: str
    gemini_api_key: str
    # min_length=1: default_knowledge_base_id() assumes there's at least one.
    # Failing to load an empty-list tenant at startup is better than a 500 the
    # first time someone hits an endpoint for it.
    knowledge_bases: list[KnowledgeBase] = Field(min_length=1)

    def default_knowledge_base_id(self) -> str:
        return self.knowledge_bases[0].id

    def knowledge_base_ids(self) -> set[str]:
        return {kb.id for kb in self.knowledge_bases}


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
    # The actual database name inside the DBMS at neo4j_uri -- "neo4j" is the
    # default on Neo4j Desktop/Community Edition (hence the default here), but
    # AuraDB free-tier instances name their database after the instance id
    # instead (e.g. "cc77e01f"), not literally "neo4j". Our own Cypher calls
    # (app/graph/neo4j_client.py) don't need this -- they omit the database
    # name and let the driver resolve the server-side default -- but
    # graphiti_core's own driver defaults to the literal string "neo4j"
    # unless told otherwise (see app/graph/graphiti_adapter.py), so this has
    # to be set explicitly for any Aura instance whose db isn't named that.
    neo4j_database: str = "neo4j"

    # Maps API keys to a TenantConfig (their own Gemini key + knowledge bases).
    # Normally populated from config/tenants.json (see TENANT_CONFIG_PATH above),
    # not from this field directly -- but it can also be set as a JSON object in
    # .env for platforms that prefer environment-variable configuration, e.g.:
    #   TENANT_API_KEYS={"<api-key>": {"tenant_id": "<tenant>", "gemini_api_key": "<their-key>",
    #                                   "knowledge_bases": [{"id": "<group_id>", "label": "<name>"}]}}
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

    # Which LLM provider build_graphiti() constructs. "gemini" is what every
    # tenant uses today (see TenantConfig.gemini_api_key); "azure_openai" and
    # "anthropic" are operator-wide alternatives -- one shared key rather than
    # a key per tenant -- for when Gemini's free-tier rate limit is the
    # bottleneck rather than per-tenant billing isolation, or when a
    # different model's extraction quality is worth the switch. Anthropic
    # (Claude) has no embeddings API of its own, so in "anthropic" mode
    # embeddings/reranking still come from Gemini (see
    # TenantConfig.gemini_api_key) -- only extraction/chat/answer-synthesis
    # move to Claude. See app/graph/graphiti_adapter.py for how all three are
    # wired up.
    llm_provider: str = "gemini"

    # Azure OpenAI connection details, used only when llm_provider is
    # "azure_openai". All three are required in that case; get them from the
    # Azure Portal resource's "Keys and Endpoint" page.
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    # Deployment names, not model names -- Azure OpenAI resources are accessed
    # by whatever name you gave the deployment when you created it, which may
    # not match the underlying model's name.
    azure_openai_llm_deployment: str = "gpt-4o-mini"
    azure_openai_embedding_deployment: str = "text-embedding-3-small"

    # Local spend caps for Azure OpenAI usage -- see app/graph/spend_limiter.py
    # for why this exists (we hold an API key, not portal access to set a real
    # cap on the resource). Two independent budgets: one for data ingestion
    # runs, one for live/testing queries against the API.
    azure_openai_ingestion_budget_usd: float = 30.0
    azure_openai_query_budget_usd: float = 20.0
    # Price-per-1M-tokens used to *estimate* spend against the budgets above.
    # Defaults match GPT-4.1 (~$2 in / ~$8 out per 1M tokens) + text-embedding-3-small
    # ($0.02/1M) as of writing -- if your actual deployments use different
    # models, update these in .env so the estimate is meaningful. Check the
    # exact current price on the deployment's page in Azure AI Foundry.
    azure_openai_input_price_per_1m: float = 2.00
    azure_openai_output_price_per_1m: float = 8.00
    azure_openai_embedding_price_per_1m: float = 0.02

    # Anthropic (Claude) connection details, used only when llm_provider is
    # "anthropic". Operator-wide, like azure_openai_api_key above -- one
    # shared key, not a key per tenant. Get a key from
    # https://console.anthropic.com/. The ingestion/query spend budgets above
    # are shared across whichever provider is active -- they're not
    # duplicated per-provider, since they represent "how much this app is
    # allowed to spend," not "how much on this specific provider."
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    # If your Claude key was issued through Microsoft Foundry (same place as
    # an Azure OpenAI resource) rather than directly from
    # console.anthropic.com, a plain Anthropic client won't authenticate --
    # set this to the Foundry resource name (the first segment of
    # https://<resource>.services.ai.azure.com/anthropic/) to use
    # AsyncAnthropicFoundry instead. Leave blank for a direct Anthropic key.
    anthropic_foundry_resource: str = ""
    # Price-per-1M-tokens for estimating spend against the shared budgets
    # above. Defaults match Claude Haiku 4.5 ($1 in / $5 out per 1M tokens) as
    # of writing -- update if using a different Claude model. Current pricing:
    # https://www.anthropic.com/pricing#api
    anthropic_input_price_per_1m: float = 1.00
    anthropic_output_price_per_1m: float = 5.00

    # Service account credentials for the "google_drive" connector type (see
    # app/ingestion/google_drive_source.py) -- the full JSON key file's
    # contents, as one string. Operator-wide, like the LLM provider keys
    # above: one credential the backend holds, not something a tenant
    # supplies themselves. A service account (rather than per-user OAuth) is
    # used deliberately -- it authenticates without any interactive consent
    # flow, which is what a server-side "Sync now"/scheduled sync needs, and
    # it only ever sees folders someone has explicitly shared with its own
    # email address, nothing else in anyone's Drive. Leave blank to disable
    # the connector type (create_connector rejects it with a clear error
    # instead of failing confusingly at sync time). Get one from Google Cloud
    # Console: enable the Drive API, create a service account, create a JSON
    # key for it, then share the target Drive folder with that service
    # account's email (found in the key JSON's "client_email" field).
    google_drive_service_account_json: str = ""

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
