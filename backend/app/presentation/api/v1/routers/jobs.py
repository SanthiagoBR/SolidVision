"""HTTP entry point for indexing jobs (RFC-029 section 7).

**These routes do not index anything, and that is the entire design.**
They write a row and read rows back. No file is opened here, no model is
loaded, and `IndexOrUpdateImagesUseCase` is never reached from this
module -- a separate host process polls the table and does the work.

That distinction is what separates this RFC from the thing RFC-026
section 3 prohibited. The prohibition was never "indexing must not be
exposed over HTTP"; it was that indexing must not happen *inside a
request*, because a route that ran five hours of inference would hold a
threadpool worker for five hours and take `/health` down with it. The
asynchronous form was always compatible, and this is it.

What that rules out, each of which is a plausible thing to reach for:
no `BackgroundTasks`, no `SessionLocal`, no `ClipEmbeddingModel`, no
`torch`, no deciding whether a cancellation is legal, and no checking for
an existing active job before creating one.
`tests/test_ai_layer_boundaries.py` walks this package's imports to keep
the first four honest; the last two are decisions that live in the Domain
and in the database respectively.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.application.use_cases.cancel_indexing_job import CancelIndexingJobUseCase
from app.application.use_cases.create_indexing_job import CreateIndexingJobUseCase
from app.application.use_cases.get_indexing_job import GetIndexingJobUseCase
from app.application.use_cases.list_indexing_jobs import ListIndexingJobsUseCase
from app.domain.entities.indexing_job import JobStatus
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.job_id import JobId
from app.infrastructure.logging.logger import get_logger
from app.presentation.dependencies import (
    get_cancel_indexing_job_use_case,
    get_create_indexing_job_use_case,
    get_indexing_job_use_case,
    get_list_indexing_jobs_use_case,
)
from app.presentation.schemas.job_schema import (
    CreateJobRequestSchema,
    JobListSchema,
    JobSchema,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = get_logger(__name__)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobSchema,
    summary="Queue an indexing run for a device, or for folders on it",
)
def create_job(
    request: CreateJobRequestSchema,
    use_case: CreateIndexingJobUseCase = Depends(get_create_indexing_job_use_case),
) -> JobSchema:
    """Queue a job and answer immediately with what was queued.

    **202, not 201.** 201 asserts that the resource the client asked for
    exists and is ready. What exists is the *intention* to index; the
    result does not exist yet and will take minutes or hours. 202
    describes that, and the difference is observable by the client that
    has to decide whether to poll (RFC-029 section 7.1).

    Synchronous `def`, not `async def`, following RFC-026 section 9:
    everything below this line is a blocking DBAPI call, and an `async
    def` would run it on the event loop and stall every concurrent
    request -- `/health` included -- for its duration. The work itself is
    somebody else's process, so what blocks here is one `INSERT`.

    The response echoes the **normalised** scopes, which may not be the
    ones that were sent: `2018` and `2018/junho` are one folder to walk,
    and a client that asked for both should be able to see that it got
    one. `[]` means the whole device.

    No `try/except`. A device that does not exist, a disk in a drawer, a
    folder that is not there and a disk that is already busy are all
    domain errors, and `error_handlers.py` turns each into 404, 409, 400
    and 409 without this function knowing any of those numbers.
    """
    logger.info(
        "job requested: device=%s, scopes=%d", request.device_id, len(request.scopes)
    )
    job = use_case.execute(DeviceId(request.device_id), request.scopes)
    logger.info("job %s queued", job.id)
    return JobSchema.from_domain(job)


@router.get(
    "",
    status_code=status.HTTP_200_OK,
    response_model=JobListSchema,
    summary="List indexing jobs, newest first",
)
def list_jobs(
    device_id: UUID | None = Query(
        default=None, description="Only jobs for this device; omit for all devices"
    ),
    job_status: JobStatus | None = Query(
        default=None,
        alias="status",
        description="Only jobs in this state; omit for all states",
    ),
    use_case: ListIndexingJobsUseCase = Depends(get_list_indexing_jobs_use_case),
) -> JobListSchema:
    """Return the history a UI shows beside a disk.

    Both filters are optional, and an omitted one means *all*, never
    *none* -- the trap the search route's `device_id` had to avoid, where
    an absent parameter turned into an empty `IN ()` and every unfiltered
    request returned nothing.

    An unknown device id is not rejected. The filter narrows a set rather
    than asserting that its members exist, so a list for a disk the system
    has never seen correctly comes back empty (the same reasoning RFC-027
    section 9 applies to search filters).

    `job_status` is named `status` on the wire. The parameter cannot be
    called `status` in this signature because that is FastAPI's status
    module, imported above and used in every decorator here.
    """
    jobs = use_case.execute(
        device_id=DeviceId(device_id) if device_id is not None else None,
        status=job_status,
    )
    return JobListSchema.from_domain(jobs)


@router.get(
    "/{job_id}",
    status_code=status.HTTP_200_OK,
    response_model=JobSchema,
    summary="Read one indexing job's progress",
)
def get_job(
    job_id: UUID,
    use_case: GetIndexingJobUseCase = Depends(get_indexing_job_use_case),
) -> JobSchema:
    """Return the job, or 404 when the id names nothing.

    Polling, not WebSocket or SSE (RFC-029 section 7.2). The client is a
    local UI asking for a number that moves every few seconds; a
    persistent channel would add connection management, reconnection and
    an asynchronous path through the API in order to save requests that
    cost almost nothing on localhost. It is future work if a measurement
    ever shows the cost.

    A missing job is 404 rather than an empty 200, because "queued, not
    started" and "that id was never a job" are the two things a polling
    client most needs to tell apart.
    """
    return JobSchema.from_domain(use_case.execute(JobId(job_id)))


@router.post(
    "/{job_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobSchema,
    summary="Ask an indexing job to stop",
)
def cancel_job(
    job_id: UUID,
    use_case: CancelIndexingJobUseCase = Depends(get_cancel_indexing_job_use_case),
) -> JobSchema:
    """Cancel a queued job outright, or ask a running one to wind up.

    **202, because cancelling a running job is also a request rather than
    an outcome.** The worker owns every transition out of `running`: it
    reads the flag between batches, finishes the batch in flight -- up to
    `settings.batch_size` images of already-paid-for inference that
    aborting would throw away for nothing -- writes its checkpoint, and
    leaves. So the response can say the stop was recorded, and a client
    watching `status` sees `running` with `cancel_requested` true until it
    actually stops.

    A queued job has nobody to ask and is cancelled immediately, which the
    same response shape reports.

    Cancelling a job that has already finished is a 409, not a quiet
    success: the caller is acting on a wrong belief about the system and a
    202 would confirm it (RFC-029 section 8). Whether a cancellation is
    legal is `IndexingJob`'s decision, not this route's.
    """
    logger.info("cancellation requested for job %s", job_id)
    return JobSchema.from_domain(use_case.execute(JobId(job_id)))
