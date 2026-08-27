# Receives Microsoft Graph change notifications -- the real-time-push
# counterpart to app/graph/connector_scheduler.py's interval polling. See
# app/ingestion/graph_subscriptions.py for how a subscription gets created
# in the first place (currently only for the "outlook_mail" connector type).
#
# Deliberately NOT behind require_tenant: Graph calls this directly with no
# tenant API key (it has none), so the trust boundary here is the
# per-subscription clientState secret instead -- see _matches_client_state
# below. This route has to be publicly reachable for Graph to call it at
# all, which is also why MCP_ALLOWED_HOSTS-style host allow-listing doesn't
# apply here; nothing sensitive is ever returned from this route (only 200/
# 202/204 with no body, or the validation handshake's own token echoed
# back), so there's nothing for a stranger hitting it to actually read.
import logging

from fastapi import APIRouter, Request, Response

from app.graph import connectors, tenants
from app.graph.graph_repository import GraphRepository
from app.ingestion.connector_sync import run_connector_sync

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/graph")
async def graph_webhook(request: Request):
    """Handles both of Graph's two request shapes to this one endpoint:

    1. The subscription-creation handshake: a `validationToken` query
       param, which must be echoed back verbatim as the plain-text response
       body within 10 seconds, or Graph refuses to create the subscription.
    2. An actual change notification: a JSON body of `{"value": [...]}`,
       one entry per changed resource. Acks fast (this just enqueues a sync
       per matched connector, same as the manual "Sync now" route) rather
       than doing the fetch+extraction inline -- Graph also expects a
       response within a few seconds here, and retries (then eventually
       cancels the subscription) if it doesn't get one.
    """
    validation_token = request.query_params.get("validationToken")
    if validation_token is not None:
        return Response(content=validation_token, media_type="text/plain")

    try:
        body = await request.json()
    except Exception:
        return Response(status_code=202)  # malformed body -- ack anyway, nothing to retry usefully
    notifications = body.get("value") or []

    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    for notification in notifications:
        await _handle_one(notification, request, repo)

    return Response(status_code=202)


async def _handle_one(notification: dict, request: Request, repo: GraphRepository) -> None:
    subscription_id = notification.get("subscriptionId")
    if not subscription_id:
        return
    connector = connectors.get_connector_by_subscription_id(subscription_id, repo=repo)
    if connector is None:
        return  # a subscription for a connector that's since been deleted -- nothing to do
    if notification.get("clientState") != connector.get("push_client_state"):
        # Doesn't match the secret this subscription was created with --
        # either a stale/replayed notification or not really from Graph.
        # Log and drop rather than act on it.
        logger.warning(f"Graph webhook clientState mismatch for connector '{connector['id']}' -- ignoring.")
        return

    tenant = tenants.find_tenant_by_tenant_id(connector["tenant_id"], repo=repo)
    if tenant is None:
        return  # the tenant was removed since this subscription was created

    from app.api.connectors import _CONNECTOR_FACTORIES

    factory = _CONNECTOR_FACTORIES.get(connector["type"])
    if factory is None:
        return

    connectors.mark_sync_queued(tenant.tenant_id, connector["id"], repo=repo)

    async def _job() -> None:
        await run_connector_sync(
            tenant, connector, factory, repo=GraphRepository(neo4j_client=request.app.state.neo4j_client)
        )

    await request.app.state.ingestion_queue.enqueue(_job)
