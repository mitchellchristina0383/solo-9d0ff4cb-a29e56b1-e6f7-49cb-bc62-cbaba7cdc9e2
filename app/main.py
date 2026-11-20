"""FastAPI service: merge two consecutive OCI diff layers into one.

Uploads are streamed to private spool files first; the merged tar is only
produced (and therefore downloadable) when both input layers have been fully
validated.  Any invalid input yields HTTP 400 and no artifact.

The service also hosts the cpuset re-pinning API for the online/batch
synthesis groups (see :mod:`app.cpuset` and the ``/cpuset/*`` routes).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .cpuset import (
    BATCH,
    ONLINE,
    AdjustmentInProgressError,
    CgroupWriteFault,
    CpusetConfig,
    CpusetController,
    CpusetError,
    InvalidSpecError,
    NoPlanError,
    Operation,
    OperationNotFoundError,
)
from .merger import InvalidLayer, LayerMerger

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB per layer
READ_CHUNK = 1024 * 1024

app = FastAPI(
    title="OCI Layer Merger",
    version="1.1.0",
    description=(
        "Combine two consecutive, uncompressed OCI diff layers (ordered "
        "lower then upper) into a single reusable diff layer. Regular files, "
        "directories and whiteouts (including opaque directory markers) are "
        "supported. Upload both tars as multipart/form-data and download the "
        "merged uncompressed tar on success.\n\n"
        "The `/cpuset/*` routes manage safe CPU re-pinning of the online and "
        "batch synthesis groups: one group is changed per step, both keep "
        "their minimum CPU count, the groups never share a CPU, and idle "
        "parent CPUs may be borrowed temporarily. Adjustments are journaled "
        "by operation id and can resume from the actual on-disk allocation."
    ),
)


class HealthResponse(BaseModel):
    status: str


# --------------------------------------------------------------------------- #
# cpuset re-pinning: wiring, request/response models and routes
# --------------------------------------------------------------------------- #

def _env_int_list(name: str, default: List[int]) -> List[int]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    from .cpuset import parse_cpus

    return sorted(parse_cpus(raw))


_controller_lock = threading.Lock()
_cached_controller: Optional[CpusetController] = None


def get_controller() -> CpusetController:
    """Build the process-wide cpuset controller from environment variables.

    Initialization is guarded by a lock: concurrent first requests must not
    build two controllers over the same state directory (their atomic writes
    would otherwise race while bootstrapping the cgroup layout).
    """
    global _cached_controller
    if _cached_controller is not None:
        return _cached_controller
    with _controller_lock:
        if _cached_controller is None:
            _cached_controller = _build_controller()
        return _cached_controller


def _build_controller() -> CpusetController:
    state_dir = os.environ.get("CPSET_STATE_DIR", "/var/lib/cpuset-controller")
    parent_cpus = frozenset(
        _env_int_list("CPSET_PARENT_CPUS", list(range(8)))
    )
    min_online = int(os.environ.get("CPSET_ONLINE_MIN_CPUS", "2"))
    min_batch = int(os.environ.get("CPSET_BATCH_MIN_CPUS", "1"))
    # Initial allocation only bootstraps a brand-new state directory; an
    # existing simulated cgroup tree is always read back as-is.
    initial_online = frozenset(
        _env_int_list("CPSET_ONLINE_INITIAL_CPUS", [0, 1, 2])
    )
    initial_batch = frozenset(
        _env_int_list("CPSET_BATCH_INITIAL_CPUS", [3])
    )
    fail_on_write_raw = os.environ.get("CPSET_FAIL_ON_WRITE", "").strip()
    fail_on_write = int(fail_on_write_raw) if fail_on_write_raw else None
    config = CpusetConfig(
        parent_cpus=parent_cpus,
        min_cpus={ONLINE: min_online, BATCH: min_batch},
        initial_cpus={ONLINE: initial_online, BATCH: initial_batch},
    )
    return CpusetController(
        state_dir=state_dir, config=config, fail_on_write=fail_on_write
    )


class RebindRequest(BaseModel):
    # Strict integer parsing: a string-encoded cpu id such as "0" must be a
    # 422 validation error rather than silently coerced.
    model_config = {"strict": True}

    online_cpus: List[int] = Field(
        ...,
        description="Target CPU ids for the online synthesis group",
        examples=[[0, 1]],
    )
    batch_cpus: List[int] = Field(
        ...,
        description="Target CPU ids for the batch synthesis group",
        examples=[[2, 3]],
    )


class StepModel(BaseModel):
    step: int
    group: str
    cpus: List[int]


class OperationResponse(BaseModel):
    operation_id: str
    status: str
    target: Dict[str, List[int]]
    plan: List[StepModel]
    records: List[Dict[str, object]]
    created_at: str
    updated_at: str


class GroupAllocation(BaseModel):
    cpus: List[int]
    min_cpus: int


class AllocationResponse(BaseModel):
    parent_cpus: List[int]
    groups: Dict[str, GroupAllocation]
    in_progress_operation: Optional[str] = None


def _serialize_operation(operation: Operation) -> OperationResponse:
    return OperationResponse(
        operation_id=operation.operation_id,
        status=operation.status,
        target={
            ONLINE: sorted(operation.target[0]),
            BATCH: sorted(operation.target[1]),
        },
        plan=[
            StepModel(step=i, group=group, cpus=sorted(cpus))
            for i, (group, cpus) in enumerate(operation.plan)
        ],
        records=operation.records,
        created_at=operation.created_at,
        updated_at=operation.updated_at,
    )


def _raise_cpuset_error(exc: CpusetError) -> None:
    if isinstance(exc, InvalidSpecError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, NoPlanError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, AdjustmentInProgressError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, OperationNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, CgroupWriteFault):
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get(
    "/cpuset/allocation",
    response_model=AllocationResponse,
    tags=["cpuset"],
    summary="Inspect the actual cpuset allocation of both groups",
)
def cpuset_allocation(
    controller: CpusetController = Depends(get_controller),
) -> AllocationResponse:
    """Return the allocation read back from the simulated cgroup files."""
    return AllocationResponse(**controller.allocation())


@app.post(
    "/cpuset/rebind",
    response_model=OperationResponse,
    tags=["cpuset"],
    summary="Re-pin both groups to their target CPU sets",
    responses={
        400: {"description": "Invalid target specification"},
        409: {"description": "No legal plan exists, or another operation is active"},
        500: {"description": "A cpuset write was interrupted; resume with the operation id"},
    },
)
def cpuset_rebind(
    payload: RebindRequest,
    controller: CpusetController = Depends(get_controller),
) -> OperationResponse:
    """Plan and execute a safe, single-group-at-a-time re-pin.

    If no legal step sequence reaches the targets, the existing allocation is
    preserved and HTTP 409 is returned.  While an earlier operation is
    unfinished, every adjustment is rejected with HTTP 409.
    """
    try:
        operation = controller.rebind(payload.online_cpus, payload.batch_cpus)
    except CpusetError as exc:
        _raise_cpuset_error(exc)
    return _serialize_operation(operation)


@app.post(
    "/cpuset/operations/{operation_id}/resume",
    response_model=OperationResponse,
    tags=["cpuset"],
    summary="Resume an interrupted operation from the actual allocation",
    responses={
        404: {"description": "Unknown operation id"},
        409: {"description": "The remembered targets are no longer reachable"},
    },
)
def cpuset_resume(
    operation_id: str,
    controller: CpusetController = Depends(get_controller),
) -> OperationResponse:
    """Re-read the actual cgroup files and continue an unfinished operation."""
    try:
        operation = controller.resume(operation_id)
    except CpusetError as exc:
        _raise_cpuset_error(exc)
    return _serialize_operation(operation)


@app.get(
    "/cpuset/operations/{operation_id}",
    response_model=OperationResponse,
    tags=["cpuset"],
    summary="Fetch one operation and its committed step record",
    responses={404: {"description": "Unknown operation id"}},
)
def cpuset_operation(
    operation_id: str,
    controller: CpusetController = Depends(get_controller),
) -> OperationResponse:
    try:
        operation = controller.get_operation(operation_id)
    except CpusetError as exc:
        _raise_cpuset_error(exc)
    return _serialize_operation(operation)


@app.get(
    "/cpuset/operations",
    response_model=List[OperationResponse],
    tags=["cpuset"],
    summary="List all re-pin operations, newest last",
)
def cpuset_operations(
    controller: CpusetController = Depends(get_controller),
) -> List[OperationResponse]:
    return [_serialize_operation(op) for op in controller.list_operations()]


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
