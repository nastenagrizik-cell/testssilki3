from __future__ import annotations

import asyncio
import hmac
import json
import os
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from .jobs import jobs


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024

app = FastAPI(title="Survey QA", version="0.4.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    expected = os.getenv("APP_ACCESS_PASSWORD", "")
    if not expected:
        return
    expected_username = os.getenv("APP_ACCESS_USERNAME", "surveyqa")
    supplied_username = credentials.username if credentials else ""
    supplied_password = credentials.password if credentials else ""
    username_ok = hmac.compare_digest(supplied_username.encode("utf-8"), expected_username.encode("utf-8"))
    password_ok = hmac.compare_digest(supplied_password.encode("utf-8"), expected.encode("utf-8"))
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется пароль",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", dependencies=[Depends(require_auth)])
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/jobs", dependencies=[Depends(require_auth)])
async def create_job(
    document: UploadFile = File(...),
    survey_url: str = Form(...),
    run_logic: bool = Form(True),
    allow_platform_demographics: bool = Form(True),
) -> JSONResponse:
    filename = document.filename or "questionnaire.docx"
    if not filename.lower().endswith(".docx"):
        raise HTTPException(400, "Поддерживаются только файлы .docx")
    payload = await document.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Файл больше {MAX_UPLOAD_BYTES // 1024 // 1024} МБ")
    if not payload.startswith(b"PK"):
        raise HTTPException(400, "Файл не похож на корректный DOCX")
    job = await jobs.create()
    asyncio.create_task(
        jobs.run(
            job,
            payload,
            filename,
            survey_url,
            run_logic,
            allow_platform_demographics,
        )
    )
    return JSONResponse({"job_id": job.id, "status": job.status})


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def get_job(job_id: str) -> JSONResponse:
    job = await jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Проверка не найдена")
    return JSONResponse(asdict(job))


@app.get("/api/jobs/{job_id}/report.json", dependencies=[Depends(require_auth)])
async def download_report(job_id: str) -> Response:
    job = await jobs.get(job_id)
    if not job or not job.result:
        raise HTTPException(404, "Отчёт ещё не готов")
    body = json.dumps(job.result, ensure_ascii=False, indent=2)
    return Response(
        body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="survey-audit-{job_id}.json"'},
    )
