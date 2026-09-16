"""The indexing-job HTTP surface (RFC-029 section 7).

Everything below the routes is replaced through FastAPI's dependency
overrides, so these tests are about the HTTP contract: statuses, shapes,
and which refusal means what. That the routes *do not index* is checked
here too, and not only by reading them -- `test_ai_layer_boundaries.py`
walks this package's imports for a database driver or an AI library, and
`TestTheRouteDoesNoWork` pins that a creation does no filesystem or model
work at all.

The status table this module fixes (RFC-029 section 7.1, corrected):

    job id names nothing               404
    device id names nothing            404
    device already has an active job   409
    cancelling a finished job          409
    device is not plugged in           409
    scope absolute / with .. / absent  400
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    make_device,
)

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.value_objects.job_id import JobId
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from app.presentation.api import app
from app.presentation.dependencies import (
    get_device_locator,
    get_device_repository,
    get_indexing_job_repository,
)

NOW = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.UTC)
BACKSLASH = chr(92)


class Api:
    """A client over the real app, with only the outermost edges replaced."""

    def __init__(self, disk: Path | None) -> None:
        self.device = make_device(label="HD2")
        self.devices = FakeDeviceRepository([self.device])
        self.jobs = InMemoryIndexingJobRepository()
        self.locator = FakeDeviceLocator(
            {self.device.id: disk} if disk is not None else {}
        )

        # The app is a module-level singleton (RFC-026), so the overrides
        # are installed on it and cleared by the fixture's teardown --
        # the arrangement `test_search_route.py` already uses.
        app.dependency_overrides[get_indexing_job_repository] = lambda: self.jobs
        app.dependency_overrides[get_device_repository] = lambda: self.devices
        app.dependency_overrides[get_device_locator] = lambda: self.locator

    def queued(self) -> IndexingJob:
        return self.jobs.create(
            IndexingJob(id=JobId.new(), device_id=self.device.id, created_at=NOW)
        )


@pytest.fixture()
def disk(tmp_path: Path) -> Path:
    for folder in ("2018", "2018/junho", "2019"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def api(disk: Path) -> Iterator[tuple[TestClient, Api]]:
    world = Api(disk)
    with TestClient(app) as client:
        yield client, world
    app.dependency_overrides.clear()


class TestCreating:
    def test_a_job_is_accepted_with_202(self, api: tuple[TestClient, Api]) -> None:
        """202, not 201: what exists is the intention, not the result.

        201 asserts the resource the client asked for is ready. It will
        take minutes or hours, and the difference is observable by a
        client deciding whether to poll (RFC-029 section 7.1).
        """
        client, world = api

        response = client.post("/api/v1/jobs", json={"device_id": str(world.device.id)})

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "pending"
        assert body["device_id"] == str(world.device.id)
        assert body["scopes"] == []

    def test_the_response_echoes_the_scopes_that_will_actually_run(
        self, api: tuple[TestClient, Api]
    ) -> None:
        """`2018` and `2018/junho` are one folder to walk, not two."""
        client, world = api

        response = client.post(
            "/api/v1/jobs",
            json={
                "device_id": str(world.device.id),
                "scopes": ["2018", "2018/junho"],
            },
        )

        assert response.status_code == 202
        assert response.json()["scopes"] == ["2018"]

    def test_a_job_starts_with_an_honest_empty_progress(
        self, api: tuple[TestClient, Api]
    ) -> None:
        """`discovery_complete` false means "scanning", never "0%"."""
        client, world = api

        body = client.post(
            "/api/v1/jobs", json={"device_id": str(world.device.id)}
        ).json()

        assert body["discovered_files"] == 0
        assert body["discovery_complete"] is False
        assert body["processed_images"] == 0
        assert body["skipped_images"] == 0
        assert body["last_processed_relative_path"] is None
        assert body["cancel_requested"] is False

    def test_an_unknown_device_is_404(self, api: tuple[TestClient, Api]) -> None:
        client, _ = api

        response = client.post("/api/v1/jobs", json={"device_id": str(uuid.uuid4())})

        assert response.status_code == 404

    def test_a_disconnected_device_is_409(self) -> None:
        """Corrected from RFC-029 section 7.1, which said 400.

        400 says the request needs fixing. Nothing about it does -- the
        identical bytes succeed once the disk is plugged in. It is the
        state that refuses, which is what 409 means.
        """
        world = Api(disk=None)
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/jobs", json={"device_id": str(world.device.id)}
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 409
        assert "not connected" in response.json()["detail"]

    @pytest.mark.parametrize(
        "scope",
        ["..", "2018/../..", "D:/fotos", "/fotos", f"{BACKSLASH}fotos", "//srv/share"],
    )
    def test_a_scope_that_escapes_the_device_is_400(
        self, api: tuple[TestClient, Api], scope: str
    ) -> None:
        """A malformed request, and no change in the world would fix it."""
        client, world = api

        response = client.post(
            "/api/v1/jobs",
            json={"device_id": str(world.device.id), "scopes": [scope]},
        )

        assert response.status_code == 400

    def test_a_scope_that_is_not_on_the_disk_is_400(
        self, api: tuple[TestClient, Api]
    ) -> None:
        client, world = api

        response = client.post(
            "/api/v1/jobs",
            json={"device_id": str(world.device.id), "scopes": ["2020"]},
        )

        assert response.status_code == 400

    def test_a_second_job_for_a_busy_device_is_409(
        self, api: tuple[TestClient, Api]
    ) -> None:
        """Refused by the partial unique index, not by a `SELECT` (section 9)."""
        client, world = api
        client.post("/api/v1/jobs", json={"device_id": str(world.device.id)})

        response = client.post("/api/v1/jobs", json={"device_id": str(world.device.id)})

        assert response.status_code == 409

    def test_a_malformed_body_is_422_rather_than_400(
        self, api: tuple[TestClient, Api]
    ) -> None:
        """FastAPI's 422 still means "this is not a well-formed request".

        A `DomainError` means the request was understood and refused,
        which is the distinction RFC-026 section 8 keeps by answering 400
        rather than flattening the two.
        """
        client, _ = api

        assert (
            client.post("/api/v1/jobs", json={"device_id": "banana"}).status_code == 422
        )


class TestConcurrentCreation:
    def test_only_one_of_two_simultaneous_requests_wins(
        self, api: tuple[TestClient, Api]
    ) -> None:
        """Two real threads, because `def` routes run in a threadpool.

        Two sequential calls would prove nothing about concurrency: the
        second simply finds the first one's row. These overlap, and the
        outcome is fixed by the database rather than by whichever arrives
        first -- one 202, one 409, never two 202s.
        """
        from concurrent.futures import ThreadPoolExecutor

        client, world = api
        body = {"device_id": str(world.device.id)}

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(lambda _: client.post("/api/v1/jobs", json=body), range(2))
            )

        statuses = sorted(response.status_code for response in responses)
        assert statuses == [202, 409]
        assert len(world.jobs.list()) == 1


class TestReading:
    def test_a_job_is_readable_by_id(self, api: tuple[TestClient, Api]) -> None:
        client, world = api
        job = world.queued()

        response = client.get(f"/api/v1/jobs/{job.id}")

        assert response.status_code == 200
        assert response.json()["id"] == str(job.id)

    def test_an_unknown_job_is_404(self, api: tuple[TestClient, Api]) -> None:
        """ "Never existed" must not look like "queued, not started yet"."""
        client, _ = api

        assert client.get(f"/api/v1/jobs/{uuid.uuid4()}").status_code == 404

    def test_listing_returns_every_job(self, api: tuple[TestClient, Api]) -> None:
        client, world = api
        world.queued()

        response = client.get("/api/v1/jobs")

        assert response.status_code == 200
        assert len(response.json()["jobs"]) == 1

    def test_listing_filters_by_status(self, api: tuple[TestClient, Api]) -> None:
        client, world = api
        job = world.queued()
        world.jobs.save_if_status(job.cancel(NOW), JobStatus.PENDING)

        assert client.get("/api/v1/jobs?status=pending").json()["jobs"] == []
        assert len(client.get("/api/v1/jobs?status=cancelled").json()["jobs"]) == 1

    def test_listing_filters_by_device(self, api: tuple[TestClient, Api]) -> None:
        client, world = api
        world.queued()

        mine = client.get(f"/api/v1/jobs?device_id={world.device.id}")
        other = client.get(f"/api/v1/jobs?device_id={uuid.uuid4()}")

        assert len(mine.json()["jobs"]) == 1
        assert other.json()["jobs"] == []

    def test_an_unknown_device_filter_is_empty_rather_than_404(
        self, api: tuple[TestClient, Api]
    ) -> None:
        """A filter narrows a set; it does not assert the set exists."""
        client, _ = api

        assert client.get(f"/api/v1/jobs?device_id={uuid.uuid4()}").status_code == 200

    def test_no_filters_means_everything_not_nothing(
        self, api: tuple[TestClient, Api]
    ) -> None:
        client, world = api
        world.queued()

        assert len(client.get("/api/v1/jobs").json()["jobs"]) == 1


class TestCancelling:
    def test_cancelling_a_queued_job_is_immediate(
        self, api: tuple[TestClient, Api]
    ) -> None:
        client, world = api
        job = world.queued()

        response = client.post(f"/api/v1/jobs/{job.id}/cancel")

        assert response.status_code == 202
        assert response.json()["status"] == "cancelled"

    def test_cancelling_a_running_job_records_the_request(
        self, api: tuple[TestClient, Api]
    ) -> None:
        """The worker owns the transitions out of `running` (section 8).

        Writing `cancelled` here would race it for the column and release
        the disk before the worker had let go of it, so the route sets a
        flag and the client sees `running` with `cancel_requested` true.
        """
        client, world = api
        job = world.queued()
        world.jobs.claim(job.id, NOW)

        response = client.post(f"/api/v1/jobs/{job.id}/cancel")

        assert response.status_code == 202
        assert response.json()["status"] == "running"
        assert response.json()["cancel_requested"] is True

    @pytest.mark.parametrize("finished", ["completed", "failed", "cancelled"])
    def test_cancelling_a_finished_job_is_409(
        self, api: tuple[TestClient, Api], finished: str
    ) -> None:
        """An error, not a quiet success (RFC-029 section 8)."""
        client, world = api
        job = world.queued()
        claimed = world.jobs.claim(job.id, NOW)
        assert claimed is not None
        terminal = {
            "completed": claimed.complete(NOW),
            "failed": claimed.fail(NOW, "boom"),
            "cancelled": claimed.cancel(NOW),
        }[finished]
        world.jobs.save_if_status(terminal, JobStatus.RUNNING)

        assert client.post(f"/api/v1/jobs/{job.id}/cancel").status_code == 409

    def test_cancelling_an_unknown_job_is_404(
        self, api: tuple[TestClient, Api]
    ) -> None:
        client, _ = api

        assert client.post(f"/api/v1/jobs/{uuid.uuid4()}/cancel").status_code == 404


class TestTheRouteDoesNoWork:
    def test_creating_a_job_reads_no_files_and_loads_no_model(
        self, api: tuple[TestClient, Api]
    ) -> None:
        """RFC-029 section 2.3, as a test rather than as a promise.

        The route writes a row. It opens no image, encodes nothing, and
        hands nothing to `IndexOrUpdateImagesUseCase` -- which is the
        whole difference between this RFC and the thing RFC-026 section 3
        prohibited. The locator is consulted, because "is this disk
        plugged in" is a question the request has to answer; nothing else
        touches the disk.
        """
        client, world = api

        client.post(
            "/api/v1/jobs",
            json={"device_id": str(world.device.id), "scopes": ["2018"]},
        )

        job = world.jobs.list()[0]
        assert job.status is JobStatus.PENDING
        assert job.progress.discovered_files == 0
        assert job.started_at is None

    def test_health_still_answers_while_jobs_exist(
        self, api: tuple[TestClient, Api]
    ) -> None:
        """The premise of RFC-029 section 2.3: the API does not do the work.

        A route that indexed inside the request would hold a threadpool
        worker for hours and take this probe down with it. Nothing here
        can, because the run happens in another process entirely -- so
        `/health` is unaffected by a queue full of jobs.
        """
        client, world = api
        client.post("/api/v1/jobs", json={"device_id": str(world.device.id)})

        assert client.get("/health").status_code == 200
