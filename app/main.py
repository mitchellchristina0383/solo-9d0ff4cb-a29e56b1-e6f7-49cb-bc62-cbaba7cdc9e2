"""FastAPI service: merge two consecutive OCI diff layers into one.

Uploads are streamed to private spool files first; the merged tar is only
produced (and therefore downloadable) when both input layers have been fully
validated.  Any invalid input yields HTTP 400 and no artifact.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .merger import InvalidLayer, LayerMerger

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB per layer
READ_CHUNK = 1024 * 1024

app = FastAPI(
    title="OCI Layer Merger",
    version="1.0.0",
    description=(
        "Combine two consecutive, uncompressed OCI diff layers (ordered "
        "lower then upper) into a single reusable diff layer. Regular files, "
        "directories and whiteouts (including opaque directory markers) are "
        "supported. Upload both tars as multipart/form-data and download the "
        "merged uncompressed tar on success."
    ),
)


class HealthResponse(BaseModel):
    status: str


def _cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _spool_upload(upload: UploadFile, workdir: str) -> str:
    """Stream an upload into a private spool file, enforcing the size cap."""
    if not upload.filename:
        raise HTTPException(status_code=400, detail="missing upload filename")
    safe_name = os.path.basename(upload.filename).replace("/", "_") or "layer.tar"
    spool = os.path.join(workdir, safe_name)
    total = 0
    try:
        with open(spool, "wb") as dst:
            while True:
                chunk = upload.file.read(READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"layer exceeds {MAX_UPLOAD_BYTES} byte limit",
                    )
                dst.write(chunk)
    finally:
        upload.file.close()
    if total == 0:
        raise HTTPException(status_code=400, detail=f"{safe_name} is empty")
    return spool


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post(
    "/merge",
    tags=["layers"],
    responses={
        200: {
            "content": {"application/x-tar": {}},
            "description": "Merged uncompressed layer tar",
        },
        400: {"description": "Invalid input layer"},
        413: {"description": "Upload too large"},
    },
)
def merge_layers(
    lower: UploadFile = File(..., description="lower layer tar, applied first"),
    upper: UploadFile = File(..., description="upper layer tar, applied second"),
) -> FileResponse:
    """Merge two ordered diff layers and return the combined layer tar."""
    workdir = tempfile.mkdtemp(prefix="layermerger-req-")
    try:
        lower_path = _spool_upload(lower, workdir)
        upper_path = _spool_upload(upper, workdir)
        output_path = os.path.join(workdir, "merged-layer.tar")

        try:
            with LayerMerger() as merger:
                merger.add_layer(lower_path, origin=1)
                merger.add_layer(upper_path, origin=2)
                with open(output_path, "wb") as out:
                    merger.write_tar(out)
        except InvalidLayer as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, EOFError) as exc:
            raise HTTPException(status_code=400, detail=f"unreadable tar: {exc}") from exc

        return FileResponse(
            output_path,
            media_type="application/x-tar",
            filename="merged-layer.tar",
            background=BackgroundTask(_cleanup, workdir),
        )
    except HTTPException:
        _cleanup(workdir)
        raise
    except Exception:
        _cleanup(workdir)
        raise
