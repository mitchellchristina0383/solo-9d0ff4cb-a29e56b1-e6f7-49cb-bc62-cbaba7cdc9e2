"""End-to-end HTTP tests for the FastAPI service."""

from __future__ import annotations

import gzip
import io
import tarfile

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import ReferenceFS, build_tar

client = TestClient(app)


def _files(lower: bytes, upper: bytes) -> dict:
    return {
        "lower": ("lower.tar", io.BytesIO(lower), "application/x-tar"),
        "upper": ("upper.tar", io.BytesIO(upper), "application/x-tar"),
    }


def test_health_and_docs_available():
    assert client.get("/health").json() == {"status": "ok"}
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "OCI Layer Merger" in docs.text
    openapi = client.get("/openapi.json").json()
    assert "/merge" in openapi["paths"]


def test_merge_endpoint_returns_downloadable_tar():
    lower = build_tar([
        ("dir", "etc/"),
        ("file", "etc/a", b"aaa\n"),
        ("wh", "", "gone"),
    ])
    upper = build_tar([
        ("file", "etc/a", b"aaa2\n"),
        ("file", "etc/b", b"bbb\n"),
    ])
    resp = client.post("/merge", files=_files(lower, upper))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-tar"
    assert "merged-layer.tar" in resp.headers["content-disposition"]

    merged = resp.content
    with tarfile.open(fileobj=io.BytesIO(merged), mode="r:") as tf:
        names = tf.getnames()
    assert "etc/a" in names and "etc/b" in names and ".wh.gone" in names

    # The downloaded tar must be functionally equivalent.
    base = build_tar([
        ("file", "gone", b"should-be-hidden\n"),
        ("file", "etc/old", b"old\n"),
    ])
    ref_step = ReferenceFS()
    ref_step.apply(base)
    ref_step.apply(lower)
    ref_step.apply(upper)

    ref_merged = ReferenceFS()
    ref_merged.apply(base)
    ref_merged.apply(merged)

    assert ref_merged.snapshot() == ref_step.snapshot()
    assert "gone" not in ref_merged.snapshot()


def test_invalid_tar_returns_400_and_no_download():
    good = build_tar([("file", "a", b"1\n")])
    resp = client.post(
        "/merge",
        files={
            "lower": ("lower.tar", io.BytesIO(b"not a tar at all" * 40), "application/x-tar"),
            "upper": ("upper.tar", io.BytesIO(good), "application/x-tar"),
        },
    )
    assert resp.status_code == 400
    assert "detail" in resp.json()
    assert resp.headers["content-type"] == "application/json"


def test_compressed_tar_returns_400():
    good = build_tar([("file", "a", b"1\n")])
    resp = client.post(
        "/merge",
        files={
            "lower": ("lower.tar", io.BytesIO(good), "application/x-tar"),
            "upper": ("upper.tar.gz", io.BytesIO(gzip.compress(good)), "application/gzip"),
        },
    )
    assert resp.status_code == 400


def test_unsafe_path_returns_400():
    bad = build_tar([("file", "../escape", b"x\n")])
    good = build_tar([("file", "a", b"1\n")])
    resp = client.post("/merge", files=_files(bad, good))
    assert resp.status_code == 400


def test_missing_form_field_returns_422():
    good = build_tar([("file", "a", b"1\n")])
    resp = client.post(
        "/merge",
        files={"lower": ("lower.tar", io.BytesIO(good), "application/x-tar")},
    )
    assert resp.status_code == 422


def test_empty_upload_returns_400():
    good = build_tar([("file", "a", b"1\n")])
    resp = client.post(
        "/merge",
        files={
            "lower": ("lower.tar", io.BytesIO(b""), "application/x-tar"),
            "upper": ("upper.tar", io.BytesIO(good), "application/x-tar"),
        },
    )
    assert resp.status_code == 400
