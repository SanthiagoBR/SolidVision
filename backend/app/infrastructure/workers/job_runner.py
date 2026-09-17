r"""The process that runs indexing jobs, and the reaper that cleans up after it.

    python -m app.infrastructure.workers.job_runner

**A host process, not a container, and that is not provisional.** RFC-029
section 6 proposed a second service in `docker-compose.yml`. It cannot
work here and would be wrong if it could: there is no backend image;
`WindowsVolumeIdentityProvider` refuses to construct off `win32`; and even
with a bind mount a container sees a mounted path rather than the
`\\?\Volume{GUID}\` that RFC-027's whole identity scheme rests on. The
product ships as a native Windows process with an installer, with only
PostgreSQL in a container -- so a Linux executor would also lose hot-plug
detection, since bind mounts are fixed when the container starts.

The consequence for this file: **nothing in it knows what platform it is
on.** Claiming, the reaper, the checkpoint and the heartbeat are plain
database work. The one Windows-shaped thing is behind `DeviceLocator`, so
a future Linux deployment is a new volume adapter rather than a rewrite.

Three entry points, one code path:

* `JobRunner.run_forever()` -- the polling loop, which is the executor;
* `JobRunner.run_claimed()` -- everything that happens to one claimed job;
* `JobRunner.run_job()` -- claim *this* job and run it, which is what the
  CLI uses so that `indexing_worker --root PATH` needs no second process
  running (RFC-029 section 12).
"""

from __future__ import annotations

import argparse
import datetime
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path, PurePath

from sqlalchemy.orm import Session

from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    IndexingSummary,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.domain.entities.device import Device
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.device_locator import DeviceLocator
from app.domain.value_objects.job_id import JobId
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.logging.logger import get_logger
from app.infrastructure.workers.indexing_worker import discovered_candidates
from app.infrastructure.workers.job_progress_observer import JobProgressObserver

logger = get_logger(__name__)


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class JobRunner:
    """Claims queued jobs, runs them, and resolves the ones nobody is running.

    Dependency-injected all the way down, including the clock. The clock
    is what makes the reaper testable: RFC-029's validation table asks for
    a worker that died mid-job, and the honest way to test that is to move
    time forward rather than to kill a process -- which on Windows would
    mean `subprocess` and a signal that does not exist there.
    """

    def __init__(
        self,
        jobs: IndexingJobRepository,
        devices: DeviceRepository,
        locator: DeviceLocator,
        indexer: IndexOrUpdateImagesUseCase,
        supported_extensions: Iterable[str],
        extract_capture_date: bool = True,
        clock: Callable[[], datetime.datetime] = utc_now,
        poll_interval: float = 2.0,
        heartbeat_interval: float = 10.0,
        stale_timeout: float = 120.0,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        warm_up: Callable[[], None] | None = None,
        excluded_directories: tuple[Path, ...] = (),
    ) -> None:
        self._jobs = jobs
        self._devices = devices
        self._locator = locator
        self._indexer = indexer
        self._supported_extensions = tuple(supported_extensions)
        self._extract_capture_date = extract_capture_date
        self._clock = clock
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval
        self._stale_timeout = stale_timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._excluded_directories = excluded_directories
        """Directories no scan may yield files from -- the thumbnail cache.

        See `FilesystemImageProvider`: without this, a job over a system
        disk would index the application's own thumbnails as photographs
        (RFC-030 section 7.1).
        """
        self._warm_up = warm_up
        """Loads the model before the loop starts; see `run_forever()`.

        Held by the runner rather than left to the composition root to
        remember, because the ordering is a correctness rule and not a
        convenience: a worker that claimed a job first would hold it,
        silent, for the several seconds the CLIP checkpoint takes to
        load, and on a short stale timeout could be reaped before it had
        done anything at all (RFC-029 section 9.1).
        """

    # ------------------------------------------------------------------
    # The loop

    def run_forever(self) -> None:
        """Reap, claim, run, repeat -- until the operator stops the process.

        Reaping happens between polls rather than on a timer of its own,
        which is the arrangement RFC-029 section 9.1 describes: it is the
        same process, doing the cheap query while it has nothing else to
        do. One executor therefore cleans up after a previous incarnation
        of itself, which is the common case -- the machine was rebooted.
        """
        self.warm_up_now()
        logger.info("Job executor started")
        try:
            while True:
                self.reap()
                if not self.claim_and_run():
                    self._sleep(self._poll_interval)
        except KeyboardInterrupt:
            # Reaching here means the interrupt landed between jobs, so
            # there is nothing to hand back. An interrupt *during* a job
            # is caught in `run_claimed()`, which returns it to the queue.
            logger.info("Job executor stopped by the operator")

    def warm_up_now(self) -> None:
        """Load the model now, without entering the polling loop.

        `run_forever()` calls this first. `--once` calls it too, because a
        measurement of start-up latency that skipped the load would be
        measuring a different process from the one that runs in anger.
        """
        if self._warm_up is not None:
            logger.info("Loading the embedding model before polling for work")
            self._warm_up()

    def run_job(self, job_id: JobId) -> IndexingJob | None:
        """Claim one named job and run it here, or report that somebody has it.

        What `indexing_worker --root PATH` calls, so that the CLI executes
        its job in its own process instead of waiting for an executor
        somebody may never have started (RFC-029 section 12). It is the
        same claim and the same run as the polling loop -- one code path,
        and no second process is ever required.

        Returns `None` when the job was not claimable, which is not a
        failure: a separate executor got there first, and the caller
        follows that job by polling instead.
        """
        claimed = self._jobs.claim(job_id, self._clock())
        if claimed is None:
            return None
        return self.run_claimed(claimed)

    def claim_and_run(self) -> bool:
        """Take the oldest queued job and run it; report whether there was one."""
        claimed = self._jobs.claim_next(self._clock())
        if claimed is None:
            return False
        self.run_claimed(claimed)
        return True

    # ------------------------------------------------------------------
    # The reaper

    def reap(self) -> list[IndexingJob]:
        """Resolve every running job whose worker stopped proving it is alive.

        The query is here and the *verdict* is not: `IndexingJob.expire()`
        decides between requeueing, cancelling and failing, because that
        is a rule about what a job is. This applies the verdict through a
        conditional write, so a job that emitted a heartbeat between the
        query and the write is left alone -- the worker holding it is
        alive after all, and stealing it would put two workers on one disk.

        Returns what it actually changed, so a caller (and a test) can see
        it rather than infer it from a log line.
        """
        cutoff = self._clock() - datetime.timedelta(seconds=self._stale_timeout)
        resolved: list[IndexingJob] = []

        for abandoned in self._jobs.list_stale(cutoff):
            verdict = abandoned.expire(self._clock(), self._max_attempts)
            written = self._jobs.save_if_status(verdict, JobStatus.RUNNING)
            if written is None:
                continue
            resolved.append(written)
            logger.warning(
                "Job %s had no heartbeat since %s; it is now %s (attempt %d of %d)",
                abandoned.id,
                abandoned.last_heartbeat_at,
                written.status.value,
                written.attempts,
                self._max_attempts,
            )
        return resolved

    # ------------------------------------------------------------------
    # One job

    def run_claimed(self, job: IndexingJob) -> IndexingJob | None:
        """Run a job this runner has already claimed, and write its outcome.

        Every exit from here writes a terminal state or hands the job
        back, so a claimed job never stays `running` because of something
        this process did. The one case that does leave it `running` is the
        one nothing can write: the process dying, which is what the reaper
        is for.
        """
        logger.info("Running job %s on device %s", job.id, job.device_id)
        observer = JobProgressObserver(
            self._jobs, job.id, self._clock, self._heartbeat_interval
        )

        try:
            summary = self._index(job, observer)
        except KeyboardInterrupt:
            # An operator stopping the process is not a crash, so no
            # attempt is consumed -- otherwise three ordinary restarts
            # would permanently fail a job that never went wrong.
            logger.info("Job %s interrupted; returning it to the queue", job.id)
            return self._finish(job, lambda current: current.release(self._clock()))
        except Exception as error:
            # The message is taken here rather than inside the lambda:
            # Python unbinds an `except ... as` name when the block ends,
            # so a closure over it is a latent `NameError` waiting for the
            # day the call moves out of the block.
            reason = str(error)
            logger.exception("Job %s failed", job.id)
            return self._finish(
                job, lambda current: current.fail(self._clock(), reason)
            )

        if observer.lost:
            # The reaper gave this job to somebody else while we were
            # working. Writing anything now would overwrite whatever that
            # worker has done since.
            logger.warning("Job %s was taken over; leaving it alone", job.id)
            return None

        for failure in summary.failures:
            logger.error("Failed to index %s", failure.path, exc_info=failure.error)
        for failure in summary.thumbnail_failures:
            logger.warning(
                "Indexed %s without a thumbnail", failure.path, exc_info=failure.error
            )
        logger.info("%s", summary.format_report())

        if summary.stopped:
            return self._finish(job, lambda current: current.cancel(self._clock()))

        return self._finish(job, self._completion_of(job))

    def _completion_of(self, job: IndexingJob) -> Callable[[IndexingJob], IndexingJob]:
        """Decide between `completed` and `failed` by looking at the disk again.

        **A scan whose volume disappeared halfway does not raise.**
        `discover()` returns quietly when its root is gone, and an
        `rglob` over a volume that vanished can simply stop producing
        files -- so without this check the job would be marked
        `completed` with half its scope indexed, which is the worst
        possible outcome because it looks like success (RFC-029 section
        15).

        The mount point is compared, not merely re-enumerated: a disk that
        was unplugged and plugged back in under a different letter is not
        the disk this run was reading from, and the files it did not get
        to are not the files it would have got to.
        """
        device = self._devices.get(job.device_id)
        mount = self._locator.mount_point(device) if device is not None else None

        def decide(current: IndexingJob) -> IndexingJob:
            if device is None or mount is None:
                return current.fail(
                    self._clock(),
                    "The device was disconnected before the scan finished; "
                    "the work already done is kept and the next job will "
                    "skip it.",
                )
            return current.complete(self._clock())

        return decide

    def _index(
        self, job: IndexingJob, observer: JobProgressObserver
    ) -> IndexingSummary:
        """Build the pipeline for one job and run it.

        The connection is re-validated *here*, after the claim, because a
        disk that was plugged in when the job was created can be in a
        drawer by the time the job comes out of the queue (RFC-029 section
        7.1). So can a folder: a scope that no longer exists is refused
        rather than scanned into nothing, because "indexed zero files"
        and "that folder is gone" are different answers.
        """
        device = self._devices.get(job.device_id)
        if device is None:
            raise RuntimeError(f"Device {job.device_id} is no longer known.")

        mount = self._locator.mount_point(device)
        if mount is None:
            raise RuntimeError(
                f"Device {device.label!r} is not connected. Plug it in and "
                "create the job again."
            )

        for scope in job.scopes:
            if self._locator.resolve_scope(device, scope) is None:
                raise RuntimeError(
                    f"{str(scope)!r} is no longer a folder on device "
                    f"{device.label!r}."
                )

        provider = FilesystemImageProvider(
            mount,
            self._supported_extensions,
            extract_capture_date=self._extract_capture_date,
            scopes=job.scopes,
            resume_after=_checkpoint_of(job),
            observer=observer,
            excluded_directories=self._excluded_directories,
        )
        return self._indexer.execute(
            self._candidates(provider, device, mount), observer
        )

    def _candidates(
        self,
        provider: FilesystemImageProvider,
        device: Device,
        mount: Path,
    ) -> Iterator[IndexCandidate]:
        """Stream discovered files as candidates, exactly as the CLI does.

        `discovered_candidates()` is imported rather than reimplemented:
        it is where `ImageId` is minted, and a second copy would be a
        second chance to compute a different id for the same file
        (RFC-029 section 12 -- one indexing code path, not two).
        """
        undiscoverable: list[IndexingFailure] = []
        yield from discovered_candidates(provider, device, mount, undiscoverable)
        for failure in undiscoverable:
            logger.error("Could not read %s", failure.path, exc_info=failure.error)

    def _finish(
        self,
        job: IndexingJob,
        transition: Callable[[IndexingJob], IndexingJob],
    ) -> IndexingJob | None:
        """Apply a terminal transition to the job **as it stands now**.

        Re-read rather than transitioned from the copy claimed minutes
        ago, because the row has moved since: the observer has been
        writing counters and a checkpoint into it, and the route may have
        set `cancel_requested`. Writing the stale copy would erase the
        progress of the run that just finished.

        Conditional on the row still being `running`, so a job the reaper
        took back in the last instant is not overwritten.
        """
        current = self._jobs.get(job.id)
        if current is None:
            logger.warning("Job %s disappeared before it could be finished", job.id)
            return None
        if current.status is not JobStatus.RUNNING:
            logger.warning(
                "Job %s is %s and no longer this worker's to finish",
                job.id,
                current.status.value,
            )
            return None

        written = self._jobs.save_if_status(transition(current), JobStatus.RUNNING)
        if written is None:
            logger.warning("Job %s changed hands while finishing", job.id)
            return None

        logger.info("Job %s is %s", written.id, written.status.value)
        return written


def _checkpoint_of(job: IndexingJob) -> PurePath | None:
    """Rebuild the stored checkpoint as a path of the running platform's kind.

    **Never compared as a string.** The checkpoint is a position in an
    order established by `sorted()` over paths, which on Windows compares
    part by part and case-insensitively; text comparison gives a different
    answer and PostgreSQL's collation a third, so a resume driven by any
    of the other two skips and repeats arbitrary files.
    """
    stored = job.last_processed_relative_path
    return None if stored is None else Path(str(stored))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.infrastructure.workers.job_runner",
        description=(
            "Run queued indexing jobs, and resolve jobs whose worker died. "
            "A host process, not a container (RFC-029 section 6)."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Reap, take at most one queued job, run it, and exit. Intended "
            "for measurement scripts and for a scheduled run; the default is "
            "to keep polling."
        ),
    )
    return parser


def build_runner(job_session: Session, image_session: Session) -> JobRunner:
    """Compose a runner over two already-open sessions.

    **Two sessions, and they must not be one** (RFC-029 section 4.11). The
    heartbeat and the checkpoint would otherwise share a transaction with
    the image writes, so the `rollback()` in RFC-024 section 6's bulk-write
    fallback would undo progress that had already been reported -- or the
    heartbeat's commit would commit half a batch.

    Separated as a function so the executor and the CLI compose the same
    runner rather than two that drift.

    **This is the composition that renders thumbnails** (RFC-030 section
    7.2), and the thumbnail cache is excluded from every scan it runs.
    Both are here rather than defaulted inside `JobRunner` because the
    directory is configuration, and this is where configuration becomes
    arguments.
    """
    from app.application.use_cases.thumbnail_writer import ThumbnailWriter
    from app.infrastructure.config.settings import settings
    from app.infrastructure.filesystem.mounted_device_locator import (
        MountedDeviceLocator,
    )
    from app.infrastructure.filesystem.sha256_content_hasher import Sha256ContentHasher
    from app.infrastructure.filesystem.thumbnail_generator import (
        PillowThumbnailGenerator,
    )
    from app.infrastructure.filesystem.thumbnail_store import (
        FilesystemThumbnailStore,
    )
    from app.infrastructure.filesystem.volume_identity_provider import (
        WindowsVolumeIdentityProvider,
    )
    from app.infrastructure.persistence.postgres_device_repository import (
        PostgresDeviceRepository,
    )
    from app.infrastructure.persistence.postgres_image_repository import (
        PostgresImageRepository,
    )
    from app.infrastructure.persistence.postgres_indexing_job_repository import (
        PostgresIndexingJobRepository,
    )
    from app.presentation.dependencies import get_embedding_model

    return JobRunner(
        jobs=PostgresIndexingJobRepository(job_session),
        devices=PostgresDeviceRepository(job_session),
        locator=MountedDeviceLocator(WindowsVolumeIdentityProvider()),
        indexer=IndexOrUpdateImagesUseCase(
            repository=PostgresImageRepository(image_session),
            embedding_model=get_embedding_model(),
            content_hasher=Sha256ContentHasher(),
            batch_size=settings.batch_size,
            metadata_prefetch_size=settings.metadata_prefetch_size,
            thumbnail_writer=ThumbnailWriter(
                generator=PillowThumbnailGenerator(),
                store=FilesystemThumbnailStore(settings.thumbnail_directory),
                max_edge=settings.thumbnail_max_edge,
            ),
        ),
        supported_extensions=settings.supported_extensions,
        extract_capture_date=settings.extract_capture_date,
        warm_up=get_embedding_model().warm_up,
        poll_interval=settings.job_poll_interval,
        heartbeat_interval=settings.job_heartbeat_interval,
        stale_timeout=settings.job_stale_timeout,
        max_attempts=settings.job_max_attempts,
        excluded_directories=(settings.thumbnail_directory,),
    )


def main() -> None:
    """Compose the executor and run it.

    The composition root -- the one place allowed to know every concrete
    class at once. The imports are function-local for the reason
    `indexing_worker.main()` gives: importing `JobRunner` must not drag
    the CLIP adapter into an Infrastructure import graph, and most of the
    test suite imports this module.

    **The model is loaded before the first poll**, not after a job is
    claimed -- and `JobRunner` owns that ordering rather than this
    function, so it holds however the executor is started.
    """
    from app.infrastructure.persistence.session import SessionLocal

    args = _build_arg_parser().parse_args()

    job_session = SessionLocal()
    image_session = SessionLocal()
    try:
        runner = build_runner(job_session, image_session)
        if args.once:
            runner.warm_up_now()
            runner.reap()
            runner.claim_and_run()
        else:
            runner.run_forever()
    finally:
        image_session.close()
        job_session.close()


if __name__ == "__main__":
    main()
