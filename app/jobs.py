from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .pipeline import run_audit


@dataclass
class Job:
    id: str
    status: str = "queued"
    stage: str = "Ожидание запуска"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    result: dict[str, Any] | None = None
    error: str | None = None


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_AUDITS", "1")))

    async def create(self) -> Job:
        job = Job(id=uuid.uuid4().hex)
        async with self._lock:
            self._jobs[job.id] = job
        return job

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def run(
        self,
        job: Job,
        document_bytes: bytes,
        filename: str,
        url: str,
        run_logic: bool,
        allow_platform_demographics: bool,
    ) -> None:
        async with self._semaphore:
            job.status = "running"
            job.stage = "Разбор Word и проверка ссылки"
            suffix = Path(filename).suffix.lower()
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                    handle.write(document_bytes)
                    temp_path = Path(handle.name)
                scenario_limit = max(1, int(os.getenv("MAX_LOGIC_SCENARIOS", "80")))
                report = await run_audit(
                    temp_path,
                    url,
                    run_logic=run_logic,
                    allow_standard_demographics=allow_platform_demographics,
                    scenario_limit=scenario_limit,
                )
                job.result = report.model_dump(mode="json")
                job.status = "completed"
                job.stage = "Проверка завершена"
            except Exception as exc:
                job.status = "failed"
                job.stage = "Ошибка"
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                if temp_path:
                    temp_path.unlink(missing_ok=True)


jobs = JobStore()
