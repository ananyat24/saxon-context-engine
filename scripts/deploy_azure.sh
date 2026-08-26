#!/usr/bin/env bash
# Deploys the Saxon AI Context Engine API to Azure Container Apps.
#
# Run this yourself, from a machine where `az login` has already been done
# against the right Azure account -- it's not run by any automation in this
# repo. Requires the Azure CLI (az): https://learn.microsoft.com/cli/azure/install-azure-cli
#
# Prerequisites this script does NOT handle:
#   - A Neo4j instance reachable over the public internet (Neo4j AuraDB Free is
#     the recommended option -- see docs/internal/infrastructure-plan.md).
#     Neo4j Desktop running on your laptop is NOT reachable from Azure.
#   - At least one tenant added via `python scripts/manage_tenants.py add`,
#     so there's an API key for the deployed app to authenticate.
#
# Usage:
#   SUBSCRIPTION="your subscription name" \
#   RESOURCE_GROUP="your resource group" \
#   LOCATION="your Azure region" \
#   NEO4J_URI="neo4j+s://xxxx.databases.neo4j.io" \
#   NEO4J_PASSWORD="..." \
#   TENANT_API_KEYS='{"...": {"tenant_id": "...", "gemini_api_key": "...", "knowledge_bases": [{"id": "...", "label": "..."}]}}' \
#   ./scripts/deploy_azure.sh
#
# All the values below are read from your environment rather than hardcoded,
# so this script has nothing deployment-specific baked in. If you're doing
# this often, it's easier to keep an env file with your actual values
# somewhere outside the repo (or in a gitignored local file) and `source` it
# before running this.
set -euo pipefail

: "${SUBSCRIPTION:?Set SUBSCRIPTION to your Azure subscription name or id}"
: "${RESOURCE_GROUP:?Set RESOURCE_GROUP}"
: "${LOCATION:?Set LOCATION, e.g. eastus}"
ACR_NAME="${ACR_NAME:-saxoncontextacr}"    # must be globally unique; override if taken
ENV_NAME="${ENV_NAME:-saxon-context-env}"
APP_NAME="${APP_NAME:-saxon-context-engine}"
IMAGE_NAME="${IMAGE_NAME:-saxon-context-engine}"

# --- Values you supply via environment variables (see Usage above) ---
: "${NEO4J_URI:?Set NEO4J_URI, e.g. neo4j+s://xxxx.databases.neo4j.io}"
: "${NEO4J_USER:=neo4j}"
# graphiti_core's own driver defaults to a database literally named "neo4j"
# unless told otherwise -- true for Neo4j Desktop/Community Edition, but an
# AuraDB instance's actual database is often named after its instance id
# instead (see app/config.py's neo4j_database).
: "${NEO4J_DATABASE:=neo4j}"
: "${NEO4J_PASSWORD:?Set NEO4J_PASSWORD}"
# Defaults to "anthropic" -- this deployment's normal configuration -- so a
# routine redeploy doesn't need to keep repeating it; override to switch
# providers for a one-off deploy.
: "${LLM_PROVIDER:=anthropic}"
: "${TENANT_API_KEYS:?Set TENANT_API_KEYS, e.g. output of: cat config/tenants.json}"
# Only needed if LLM_PROVIDER=azure_openai:
AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}"
AZURE_OPENAI_API_KEY="${AZURE_OPENAI_API_KEY:-}"
AZURE_OPENAI_API_VERSION="${AZURE_OPENAI_API_VERSION:-2024-10-21}"
AZURE_OPENAI_LLM_DEPLOYMENT="${AZURE_OPENAI_LLM_DEPLOYMENT:-gpt-4o-mini}"
AZURE_OPENAI_EMBEDDING_DEPLOYMENT="${AZURE_OPENAI_EMBEDDING_DEPLOYMENT:-text-embedding-3-small}"
# Only needed if LLM_PROVIDER=anthropic:
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-haiku-4-5}"
# Only needed if the Claude key was issued through Microsoft Foundry rather
# than directly from console.anthropic.com (see app/config.py):
ANTHROPIC_FOUNDRY_RESOURCE="${ANTHROPIC_FOUNDRY_RESOURCE:-}"
# Only needed to enable the "google_drive" source connector type -- the full
# contents of a Google Cloud service account's JSON key (see app/config.py
# and .env.example for how to get one). Leave unset to deploy without it.
GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON="${GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON:-}"

echo "=== 1. Selecting subscription ==="
az account set --subscription "$SUBSCRIPTION"

echo "=== 2. Confirming resource group exists ==="
az group show --name "$RESOURCE_GROUP" --output none

echo "=== 3. Registering required resource providers (skipped for any already registered) ==="
# Only calls `register` for a provider that actually needs it -- the register
# action itself requires subscription-level Contributor/Owner, which a
# resource-group-scoped Contributor role (common when someone else owns the
# subscription) doesn't have. Checking first (a lower-privilege read) avoids
# an AuthorizationFailed error on a call that would've been a no-op anyway.
for provider in Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry; do
  state=$(az provider show --namespace "$provider" --query registrationState --output tsv)
  if [ "$state" = "Registered" ]; then
    echo "$provider already registered, skipping."
  else
    az provider register --namespace "$provider" --wait
  fi
done

echo "=== 4. Creating a container registry to hold the built image ==="
# Skipped if it already exists.
az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null || \
  az acr create --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" --sku Basic --admin-enabled true

echo "=== 5. Building the image in Azure (no local Docker needed) ==="
# `az acr build` uploads the repo and builds it in the cloud, using the
# Dockerfile at the repo root -- no local Docker install required.
# --no-logs: still blocks until the build finishes and still fails the
# script on a real build failure (via the command's exit code) -- it only
# skips *displaying* the streamed logs locally, which is needed on some
# Windows consoles where a non-UTF8 codepage makes the CLI's own log-printing
# crash on certain build output (pip's progress spinner, etc.), independent
# of whether the build itself succeeds.
az acr build --registry "$ACR_NAME" --image "$IMAGE_NAME:latest" --no-logs .

echo "=== 6. Creating the Container Apps environment (skipped if it exists) ==="
az containerapp env show --name "$ENV_NAME" --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null || \
  az containerapp env create --name "$ENV_NAME" --resource-group "$RESOURCE_GROUP" --location "$LOCATION"

ACR_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer --output tsv)

echo "=== 7. Creating (or updating) the Container App ==="
# Secrets (--secrets) are Container Apps' own secret store: encrypted at
# rest, referenced by name rather than embedded in plain env vars, and not
# visible in `az containerapp show` output the way a plain env var value is.
# This is the "read from environment variable, not Key Vault, for now" setup
# -- one step more secure than a bare env var, with Key Vault itself still an
# option to layer in later without changing how the app reads its config.
if az containerapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null; then
  echo "App exists, updating image, secrets, and env vars..."
  # `containerapp update` has no --secrets flag (that's create-only) -- secrets
  # on an existing app are set separately, then referenced the same way.
  az containerapp secret set \
    --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --secrets \
      neo4j-password="$NEO4J_PASSWORD" \
      tenant-api-keys="$TENANT_API_KEYS" \
      azure-openai-api-key="$AZURE_OPENAI_API_KEY" \
      anthropic-api-key="$ANTHROPIC_API_KEY" \
      google-drive-service-account-json="$GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON"
  # Container Apps only creates a new revision (and thus restarts the
  # container, re-resolving secretref values into fresh env vars) when it
  # detects a template change -- an env var *declaration* changing, or the
  # image tag changing. A secret's *value* changing while its declaration
  # (secretref:tenant-api-keys, etc.) stays the same does NOT count, so an
  # update that only rotates a secret's content would otherwise silently
  # keep serving the old value from the already-running container. An
  # explicit --revision-suffix forces a new revision on every update,
  # regardless of what else did or didn't change.
  az containerapp update \
    --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --image "$ACR_SERVER/$IMAGE_NAME:latest" \
    --revision-suffix "deploy$(date +%Y%m%d%H%M%S)" \
    --set-env-vars \
      NEO4J_URI="$NEO4J_URI" \
      NEO4J_USER="$NEO4J_USER" \
      NEO4J_DATABASE="$NEO4J_DATABASE" \
      NEO4J_PASSWORD=secretref:neo4j-password \
      TENANT_API_KEYS=secretref:tenant-api-keys \
      LLM_PROVIDER="$LLM_PROVIDER" \
      AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
      AZURE_OPENAI_API_KEY=secretref:azure-openai-api-key \
      AZURE_OPENAI_API_VERSION="$AZURE_OPENAI_API_VERSION" \
      AZURE_OPENAI_LLM_DEPLOYMENT="$AZURE_OPENAI_LLM_DEPLOYMENT" \
      AZURE_OPENAI_EMBEDDING_DEPLOYMENT="$AZURE_OPENAI_EMBEDDING_DEPLOYMENT" \
      ANTHROPIC_API_KEY=secretref:anthropic-api-key \
      ANTHROPIC_MODEL="$ANTHROPIC_MODEL" \
      ANTHROPIC_FOUNDRY_RESOURCE="$ANTHROPIC_FOUNDRY_RESOURCE" \
      GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON=secretref:google-drive-service-account-json
else
  az containerapp create \
    --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --environment "$ENV_NAME" \
    --image "$ACR_SERVER/$IMAGE_NAME:latest" \
    --registry-server "$ACR_SERVER" \
    --target-port 8000 \
    --ingress external \
    --min-replicas 0 --max-replicas 3 \
    --secrets \
      neo4j-password="$NEO4J_PASSWORD" \
      tenant-api-keys="$TENANT_API_KEYS" \
      azure-openai-api-key="$AZURE_OPENAI_API_KEY" \
      anthropic-api-key="$ANTHROPIC_API_KEY" \
      google-drive-service-account-json="$GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON" \
    --env-vars \
      NEO4J_URI="$NEO4J_URI" \
      NEO4J_USER="$NEO4J_USER" \
      NEO4J_DATABASE="$NEO4J_DATABASE" \
      NEO4J_PASSWORD=secretref:neo4j-password \
      TENANT_API_KEYS=secretref:tenant-api-keys \
      LLM_PROVIDER="$LLM_PROVIDER" \
      AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
      AZURE_OPENAI_API_KEY=secretref:azure-openai-api-key \
      AZURE_OPENAI_API_VERSION="$AZURE_OPENAI_API_VERSION" \
      AZURE_OPENAI_LLM_DEPLOYMENT="$AZURE_OPENAI_LLM_DEPLOYMENT" \
      AZURE_OPENAI_EMBEDDING_DEPLOYMENT="$AZURE_OPENAI_EMBEDDING_DEPLOYMENT" \
      ANTHROPIC_API_KEY=secretref:anthropic-api-key \
      ANTHROPIC_MODEL="$ANTHROPIC_MODEL" \
      ANTHROPIC_FOUNDRY_RESOURCE="$ANTHROPIC_FOUNDRY_RESOURCE" \
      GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON=secretref:google-drive-service-account-json
fi

echo "=== Done ==="
FQDN=$(az containerapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query properties.configuration.ingress.fqdn --output tsv)
echo "App URL: https://$FQDN"
echo "Health check: curl https://$FQDN/api/v1/health"
