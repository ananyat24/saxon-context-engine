# Tests app/graph/ingestion_queue.py: pure asyncio logic, no database, no
# network. Covers: jobs actually run, concurrency is bounded by
# max_concurrent, and one job's exception doesn't take down the dispatcher
# or block jobs queued after it.
import asyncio

from app.graph.ingestion_queue import IngestionQueue


def test_enqueued_job_runs():
    async def scenario():
        queue = IngestionQueue(max_concurrent=2)
        queue.start()
        ran = asyncio.Event()

        async def job():
            ran.set()

        await queue.enqueue(job)
        await asyncio.wait_for(ran.wait(), timeout=2)
        await queue.stop()

    asyncio.run(scenario())


def test_concurrency_is_bounded_by_max_concurrent():
    async def scenario():
        queue = IngestionQueue(max_concurrent=2)
        queue.start()
        concurrent = 0
        max_seen = 0
        lock = asyncio.Lock()
        done = asyncio.Event()
        finished = []

        async def job(i):
            nonlocal concurrent, max_seen
            async with lock:
                concurrent += 1
                max_seen = max(max_seen, concurrent)
            await asyncio.sleep(0.05)
            async with lock:
                concurrent -= 1
            finished.append(i)
            if len(finished) == 5:
                done.set()

        for i in range(5):
            await queue.enqueue(lambda i=i: job(i))

        await asyncio.wait_for(done.wait(), timeout=3)
        await queue.stop()
        return max_seen

    max_seen = asyncio.run(scenario())
    assert max_seen <= 2


def test_a_failing_job_does_not_block_jobs_queued_after_it():
    async def scenario():
        queue = IngestionQueue(max_concurrent=1)
        queue.start()
        second_ran = asyncio.Event()

        async def failing_job():
            raise RuntimeError("boom")

        async def second_job():
            second_ran.set()

        await queue.enqueue(failing_job)
        await queue.enqueue(second_job)
        await asyncio.wait_for(second_ran.wait(), timeout=2)
        await queue.stop()

    asyncio.run(scenario())
