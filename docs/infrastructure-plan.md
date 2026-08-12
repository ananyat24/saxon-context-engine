# Infrastructure & tech stack plan

Where things stand: everything runs locally today (Neo4j Desktop plus a
Python process on one machine), which costs nothing but also can't be
reached by anyone else. This document scopes what's actually needed to
change that, in phases, choosing the cheapest option at each step rather
than the most capable one.

Prices below were current as of August 2026 (sources at the bottom) and
change over time; re-check before committing to a plan.

## What's actually needed, and why

To be usable by more than one person, four things are missing:

1. A database reachable from outside one laptop.
2. Somewhere to run the API continuously.
3. A way to run ingestion jobs on a schedule, unattended.
4. Somewhere to keep real secrets that isn't a local `.env` file.

Nothing else on the usual "production checklist" (API gateways, secret
vaults, dedicated schedulers) is needed yet. Adding them now would be paying
for capacity this project doesn't use.

## Phase 1: a shareable pilot or demo

**Database: Neo4j AuraDB Free.** A hosted, managed Neo4j instance at $0/mo,
no credit card required. No code changes needed beyond swapping `NEO4J_URI`
in `.env` for the Aura connection string (`neo4j+s://...`); the Python
driver already used in this project handles that URI scheme without
modification. Two real limits worth knowing: Aura's own docs aren't fully
consistent on the exact free-tier cap (either 50k or 200k nodes depending on
the page), and an idle instance auto-pauses, adding a few seconds of delay
to the first query after a gap. Both are fine for a demo, worth watching
once real usage starts.

**API hosting: Azure Container Apps, Consumption plan.** The free monthly
grant (180,000 vCPU-seconds, 360,000 GiB-seconds, 2,000,000 requests) covers
a small FastAPI app used for occasional demos with room to spare. Realistic
cost: $0/month at this stage.

**Secrets: platform environment variables**, set directly in Container
Apps' own configuration. No extra tool needed. Azure Key Vault isn't worth
adding yet, it only pays off once multiple environments or services need to
share the same secrets.

**Scheduled ingestion: GitHub Actions scheduled workflows.** The code
already lives on GitHub, and a private repo gets 2,000 free minutes/month,
enough for a daily or even hourly sync job that takes a few minutes to run.
No separate scheduler (Azure Functions, Airflow, Data Factory) is needed at
this volume.

**LLM: keep Google Gemini**, already integrated. Its free tier (roughly 15
requests/minute on Flash-Lite as of writing) covers this phase; paid usage
beyond that runs about $0.10-0.30 per million input tokens depending on
model version. There's no reason to add Azure OpenAI as a second provider
unless the organization already has committed Azure OpenAI spend to draw
down. Graphiti does support it if that becomes true later.

**Estimated cost: $0/month**, assuming usage stays inside the free tiers
above, which is realistic for a pilot.

## Phase 2: first real client, production pilot

- **Database**: move to AuraDB Professional ($65/month minimum for 1GB)
  once the free tier's size limit or auto-pause cold starts become a real
  problem, not before. Still fully managed, no server to maintain.
- **API hosting**: same Container Apps Consumption plan; cost scales with
  actual traffic (roughly $0.000024 per vCPU-second beyond the free grant),
  likely still low at pilot-client volume.
- **Secrets**: this is the point to add Azure Key Vault, once there's more
  than one environment (dev/staging/prod) or more than one service reading
  the same secret. Cost is close to negligible ($0.03 per 10,000
  operations).
- **Tenant isolation**: the API-key-to-tenant mapping already built
  (`app/security.py`) is enough for a handful of clients. Revisit only if
  managing keys by hand across many clients becomes the actual bottleneck.
- **Ingestion**: stay on GitHub Actions unless job volume outgrows the free
  minutes. If it does, Azure Functions on its Consumption plan (billed per
  execution, typically pennies a month at this scale) is the next cheapest
  step, not a full orchestration platform.

## Phase 3: multiple concurrent clients, compliance requirements

Only take on true per-tenant physical isolation (a separate Neo4j database
per client, which needs AuraDB Business Critical or a self-managed
Enterprise deployment) if a specific client's compliance requirement
demands it. Until then, the group_id-plus-API-key model already built is
the appropriate amount of isolation for the cost; see the main README's
[API access](../README.md#api-access) section for the reasoning.

This is also roughly the point where self-hosting Neo4j on a VM could
become cheaper than AuraDB's per-GB pricing. It's not a trade worth making
before then: self-hosting means someone now owns patching, backups, and
uptime, which AuraDB currently handles.

## Deliberately not adding yet

| Tool | Why not yet |
|---|---|
| Azure Key Vault | No second environment or service needs it yet |
| Azure OpenAI | Redundant with Gemini unless spend is already committed elsewhere |
| Azure Functions / Data Factory / Airflow | GitHub Actions' free minutes cover realistic ingestion volume for $0 |
| A dedicated VM for Neo4j | AuraDB removes an entire category of ops work for a competitive price |
| Azure API Management / Front Door | The API-key auth already built covers current needs; add this for rate limiting or a public developer portal, not before |

## Summary

| Need | Phase 1 | Later |
|---|---|---|
| Graph database | Neo4j AuraDB Free ($0) | AuraDB Professional (~$65/mo) once outgrown |
| API hosting | Azure Container Apps, Consumption ($0 at low traffic) | Same, cost scales with traffic |
| Secrets | Platform environment variables ($0) | Azure Key Vault once multi-environment (~$0.03/10k ops) |
| Scheduled ingestion | GitHub Actions ($0, within free minutes) | Azure Functions Consumption if outgrown |
| LLM | Google Gemini, free tier | Gemini paid tier if outgrown |
| Tenant isolation | API key to group_id (already built, $0) | Same, revisit only under real compliance pressure |

## Sources

- [Neo4j AuraDB pricing](https://neo4j.com/pricing/)
- [Azure Container Apps pricing](https://azure.microsoft.com/en-us/pricing/details/container-apps/)
- [Azure Key Vault pricing](https://azure.microsoft.com/en-us/pricing/details/key-vault/)
- [GitHub Actions billing](https://docs.github.com/billing/managing-billing-for-github-actions/about-billing-for-github-actions)
- [Google Gemini API pricing](https://ai.google.dev/pricing)
- [Graphiti LLM provider configuration](https://help.getzep.com/graphiti/configuration/llm-configuration)
- [Azure B-series VM pricing](https://instances.vantage.sh/azure/vm/b1s)
