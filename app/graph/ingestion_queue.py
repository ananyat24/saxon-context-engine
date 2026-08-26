# Decouples "a sync was triggered" from "extraction actually ran" -- CLAUDE.md's
# v2 goal of adding a change queue between capture and extraction, scoped to
# what this deployment's actual scale needs: an in-process asyncio queue with
# a small bounded worker pool, not a hosted broker (Redis Streams/SQS).
#
# This fixes a real problem the old synchronous "Sync now" route had, not a
# hypothetical one: POST /connectors/{id}/sync used to block the whole HTTP
# request until fetch + extraction finished, which is a real risk for a
# large Drive/SharePoint folder (could be many files, many extraction
# calls). It's also the actual prerequisite for a future real webhook
# receiver (v2's other bullet, not yet built -- needs the source's own
# push-notification registration, real external setup this repo can't do on
# its own): a webhook handler MUST ack fast and can't block on an LLM call,
# so it needs somewhere to hand the job off to, which is what this is.
#
# Single-instance scoped, same caveat app/graph/connector_scheduler.py's
# module docstring already flags for the sync scheduler: if this app ever
# runs more than one replica concurrently, each instance has its own queue --
# fine functionally (content-hash dedup in run_connector_sync still prevents
# double-ingestion), but not a shared work queue across instances. A real
# multi-instance deployment should swap this for Redis Streams/SQS; this
# class's small interface (enqueue/start/stop) is exactly what that swap
# would replace, so nothing above it (the sync route, the scheduler) would
# need to change.
import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

Job = Callable[[], Awaitable[None]]

# Bounds how many syncs can be extracting concurrently across the whole
# process -- protects the per-tenant ingestion spend budget (see
# app/graph/spend_limiter.py) and Neo4j from a burst of queued jobs (e.g.
# several connectors' "Sync now" clicked in quick succession) all running
# their LLM extraction calls at once, rather than relying on however many
# happened to get triggered together.
_DEFAULT_MAX_CONCURRENT = 2


class IngestionQueue:
    def __init__(self, max_concurrent: int = _DEFAULT_MAX_CONCURRENT):
        self._queue: "asyncio.Queue[Job]" = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._dispatcher_task: asyncio.Task | None = None
        self._worker_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        self._dispatcher_task = asyncio.create_task(self._dispatch())

    async def stop(self) -> None:
        """Cancels the dispatcher and every in-flight job, then waits for
        them to actually unwind -- called from app/main.py's lifespan on
        shutdown so a job doesn't outlive the Neo4j driver it depends on."""
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
        for task in list(self._worker_tasks):
            task.cancel()
        await asyncio.gather(*self._worker_tasks, self._dispatcher_task, return_exceptions=True)

    async def enqueue(self, job: Job) -> None:
        await self._queue.put(job)

    async def _dispatch(self) -> None:
        while True:
            job = await self._queue.get()
            task = asyncio.create_task(self._run_job(job))
            self._worker_tasks.add(task)
            task.add_done_callback(self._worker_tasks.discard)

    async def _run_job(self, job: Job) -> None:
        async with self._semaphore:
            try:
                await job()
            except Exception:
                # A queued job's failure has nowhere else to go -- the HTTP
                # request that enqueued it already returned. run_connector_sync
                # itself never raises (it records "error" on the connector's
                # own status instead -- see app/ingestion/connector_sync.py),
                # so reaching here means something outside that contract broke;
                # logging is the only way anyone finds out.
                logger.exception("Queued ingestion job failed unexpectedly")
