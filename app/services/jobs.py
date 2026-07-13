from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
import logging
from threading import Event, Thread
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.models import DurableJob
from app.db.session import SessionLocal


logger = logging.getLogger(__name__)
JobHandler = Callable[[Session, DurableJob], None]


class PermanentJobError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def enqueue_job(
    db: Session,
    job_type: str,
    idempotency_key: str,
    payload: dict[str, Any],
    *,
    max_attempts: int | None = None,
) -> DurableJob:
    values = {
        "job_type": job_type,
        "idempotency_key": idempotency_key,
        "payload_json": json.dumps(payload),
        "status": "pending",
        "attempts": 0,
        "max_attempts": max_attempts or settings.job_max_attempts,
        "available_at": utc_now(),
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(postgresql_insert(DurableJob).values(**values).on_conflict_do_nothing(index_elements=["idempotency_key"]))
    elif dialect == "sqlite":
        db.execute(sqlite_insert(DurableJob).values(**values).on_conflict_do_nothing(index_elements=["idempotency_key"]))
    else:
        existing = db.scalar(select(DurableJob).where(DurableJob.idempotency_key == idempotency_key))
        if existing is None:
            db.add(DurableJob(**values))
            db.flush()
    return db.scalar(select(DurableJob).where(DurableJob.idempotency_key == idempotency_key))


class DurableJobWorker:
    def __init__(
        self,
        session_factory: sessionmaker = SessionLocal,
        handler: JobHandler | None = None,
        *,
        poll_interval: float | None = None,
        retry_base_seconds: float = 2,
    ):
        self.session_factory = session_factory
        self.handler = handler or self._default_handler
        self.poll_interval = settings.job_poll_interval_seconds if poll_interval is None else poll_interval
        self.retry_base_seconds = retry_base_seconds
        self.stop_event = Event()
        self.thread: Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = Thread(target=self.run_forever, name="durable-job-worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def run_forever(self) -> None:
        logger.info("durable job worker started")
        while not self.stop_event.is_set():
            try:
                processed = self.run_once()
            except Exception:
                logger.exception("durable job worker loop failed")
                processed = False
            if not processed:
                self.stop_event.wait(self.poll_interval)
        logger.info("durable job worker stopped")

    def run_once(self) -> bool:
        job_id = self._claim_next()
        if job_id is None:
            return False

        try:
            with self.session_factory() as db:
                job = db.get(DurableJob, job_id)
                if job is None:
                    return True
                self.handler(db, job)
                job.status = "succeeded"
                job.locked_at = None
                job.last_error = None
                job.updated_at = utc_now()
                db.commit()
                logger.info(
                    "job succeeded",
                    extra={"job_id": job.id, "job_type": job.job_type, "attempt": job.attempts},
                )
        except Exception as exc:
            self._record_failure(job_id, exc)
        return True

    def _claim_next(self) -> int | None:
        now = utc_now()
        stale_before = now - timedelta(seconds=settings.job_lock_timeout_seconds)
        with self.session_factory() as db:
            db.execute(
                update(DurableJob)
                .where(DurableJob.status == "processing", DurableJob.locked_at < stale_before)
                .values(status="pending", locked_at=None, available_at=now, updated_at=now)
            )
            job = db.scalar(
                select(DurableJob)
                .where(DurableJob.status == "pending", DurableJob.available_at <= now)
                .order_by(DurableJob.id)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                db.commit()
                return None
            job.status = "processing"
            job.locked_at = now
            job.attempts += 1
            job.updated_at = now
            db.commit()
            logger.info(
                "job claimed",
                extra={"job_id": job.id, "job_type": job.job_type, "attempt": job.attempts},
            )
            return job.id

    def _record_failure(self, job_id: int, exc: Exception) -> None:
        with self.session_factory() as db:
            job = db.get(DurableJob, job_id)
            if job is None:
                return
            permanent = isinstance(exc, PermanentJobError)
            exhausted = job.attempts >= job.max_attempts
            job.status = "dead" if permanent or exhausted else "pending"
            delay = min(300, self.retry_base_seconds * (2 ** max(job.attempts - 1, 0)))
            job.available_at = utc_now() if job.status == "dead" else utc_now() + timedelta(seconds=delay)
            job.locked_at = None
            job.last_error = f"{type(exc).__name__}: {exc}"[:4000]
            job.updated_at = utc_now()
            db.commit()
            logger.error(
                "job failed",
                extra={"job_id": job.id, "job_type": job.job_type, "attempt": job.attempts},
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    @staticmethod
    def _default_handler(db: Session, job: DurableJob) -> None:
        from app.services.job_handlers import handle_job

        handle_job(db, job)
