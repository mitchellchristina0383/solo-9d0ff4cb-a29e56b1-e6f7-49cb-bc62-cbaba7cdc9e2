"""HTTP tests for the /cpuset re-pinning API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.cpuset import (
    ONLINE,
    CgroupWriteFault,
    CpusetConfig,
    CpusetController,
)
from app.main import app, get_controller


@pytest.fixture()
def client(tmp_path):
    controller = CpusetController(str(tmp_path), CpusetConfig())
    app.dependency_overrides[get_controller] = lambda: controller
    with TestClient(app) as test_client:
        yield test_client, controller
    app.dependency_overrides.clear()


def test_allocation_endpoint_reports_initial_state(client):
    http, _ = client
    resp = http.get("/cpuset/allocation")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["parent_cpus"] == [0, 1, 2, 3, 4, 5, 6, 7]
    assert payload["groups"][ONLINE]["cpus"] == [0, 1, 2]
    assert payload["groups"]["batch"]["min_cpus"] == 1
    assert payload["in_progress_operation"] is None


def test_rebind_returns_operation_with_steps_and_final_allocation(client):
    http, controller = client
    resp = http.post(
        "/cpuset/rebind", json={"online_cpus": [3, 4], "batch_cpus": [0, 1, 2]}
    )
    assert resp.status_code == 200
    operation = resp.json()
    assert operation["status"] == "done"
    assert operation["operation_id"] == "000001"
    assert operation["target"] == {"online": [3, 4], "batch": [0, 1, 2]}
    assert len(operation["plan"]) >= 2
    # Every step names one group and a non-empty cpu list.
    for step in operation["plan"]:
        assert step["group"] in ("online", "batch")
        assert step["cpus"]

    allocation = http.get("/cpuset/allocation").json()
    assert allocation["groups"]["online"]["cpus"] == [3, 4]
    assert allocation["groups"]["batch"]["cpus"] == [0, 1, 2]
    assert allocation["in_progress_operation"] is None


def test_rebind_rejects_invalid_payloads(client):
    http, _ = client
    semantic_errors = [
        {"online_cpus": [9], "batch_cpus": [3]},          # outside parent
        {"online_cpus": [0], "batch_cpus": [3]},          # below minimum
        {"online_cpus": [4, 5], "batch_cpus": [5]},       # overlap
        {"online_cpus": [4, 4], "batch_cpus": [3]},       # duplicates
        {"online_cpus": [], "batch_cpus": [3]},           # empty
    ]
    for payload in semantic_errors:
        resp = http.post("/cpuset/rebind", json=payload)
        assert resp.status_code == 400, payload
        assert "detail" in resp.json()

    # Type errors are caught at the schema layer instead.
    resp = http.post(
        "/cpuset/rebind", json={"online_cpus": ["0", "1"], "batch_cpus": [3]}
    )
    assert resp.status_code == 422


def test_rebind_rejects_malformed_body_with_422(client):
    http, _ = client
    resp = http.post("/cpuset/rebind", json={"online_cpus": [0, 1]})
    assert resp.status_code == 422


def test_unreachable_target_returns_409_and_keeps_allocation(client, tmp_path):
    cfg = CpusetConfig(
        parent_cpus=frozenset({0, 1, 2, 3}),
        min_cpus={"online": 2, "batch": 2},
        initial_cpus={"online": frozenset({0, 1}), "batch": frozenset({2, 3})},
    )
    packed = CpusetController(str(tmp_path / "packed"), cfg)
    app.dependency_overrides[get_controller] = lambda: packed
    http, _ = client
    resp = http.post(
        "/cpuset/rebind", json={"online_cpus": [2, 3], "batch_cpus": [0, 1]}
    )
    assert resp.status_code == 409
    allocation = http.get("/cpuset/allocation").json()
    assert allocation["groups"]["online"]["cpus"] == [0, 1]
    assert allocation["groups"]["batch"]["cpus"] == [2, 3]
    assert http.get("/cpuset/operations").json() == []


def test_second_adjustment_rejected_while_operation_unfinished(client, tmp_path):
    faulting = CpusetController(
        str(tmp_path / "fault"), CpusetConfig(), fail_on_write=1
    )
    app.dependency_overrides[get_controller] = lambda: faulting
    http, _ = client

    with pytest.raises(CgroupWriteFault):
        faulting.rebind([3, 4], [0, 1, 2])

    blocked = http.post(
        "/cpuset/rebind", json={"online_cpus": [6, 7], "batch_cpus": [2]}
    )
    assert blocked.status_code == 409
    assert "not finished" in blocked.json()["detail"]

    # Restart: a new controller over the same state directory, then resume.
    restarted = CpusetController(str(tmp_path / "fault"), CpusetConfig())
    app.dependency_overrides[get_controller] = lambda: restarted

    unfinished = http.get("/cpuset/allocation").json()["in_progress_operation"]
    assert unfinished == "000001"
    resume = http.post(f"/cpuset/operations/{unfinished}/resume")
    assert resume.status_code == 200
    assert resume.json()["status"] == "done"
    allocation = http.get("/cpuset/allocation").json()
    assert allocation["groups"]["online"]["cpus"] == [3, 4]
    assert allocation["groups"]["batch"]["cpus"] == [0, 1, 2]

    # After completion the next adjustment is accepted again.
    accepted = http.post(
        "/cpuset/rebind", json={"online_cpus": [6, 7], "batch_cpus": [2]}
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "done"


def test_operation_lookup_and_listing(client):
    http, _ = client
    assert http.get("/cpuset/operations").json() == []
    assert http.get("/cpuset/operations/000001").status_code == 404

    http.post("/cpuset/rebind", json={"online_cpus": [4, 5], "batch_cpus": [0, 1]})
    listing = http.get("/cpuset/operations").json()
    assert [op["operation_id"] for op in listing] == ["000001"]

    detail = http.get("/cpuset/operations/000001").json()
    assert detail["status"] == "done"
    assert detail["records"]


def test_resume_unknown_operation_returns_404(client):
    http, _ = client
    resp = http.post("/cpuset/operations/424242/resume")
    assert resp.status_code == 404


def test_cpuset_routes_published_in_openapi_and_docs(client):
    http, _ = client
    openapi = http.get("/openapi.json").json()
    for path in (
        "/cpuset/rebind",
        "/cpuset/allocation",
        "/cpuset/operations",
        "/cpuset/operations/{operation_id}",
        "/cpuset/operations/{operation_id}/resume",
    ):
        assert path in openapi["paths"]
    docs = http.get("/docs")
    assert docs.status_code == 200
