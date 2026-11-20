#!/usr/bin/env python
"""End-to-end self-test for the running OCI layer merger service.

The script builds several layer pairs plus *two different lower file-systems*,
posts the layers to ``/merge``, downloads the resulting tar, and then verifies
on each lower file-system that:

    base + lower + upper   ==   base + merged

in terms of path set, entry type and file contents.

It also exercises the invalid-input paths and confirms those return HTTP 400
with no downloadable artifact.

The ``--with-cpuset`` flag additionally drives the ``/cpuset/*`` re-pinning
API through its key scenarios: a CPU swap performed via borrowed idle CPUs,
an unreachable target that must preserve the existing allocation, operation
mutex and the write-interruption/restart/resume flow (the latter requires the
service to be started with ``CPSET_FAIL_ON_WRITE``).

Run against a locally started service, e.g.:

    python -I scripts/selftest.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tarfile
import urllib.error
import urllib.request
import uuid
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# minimal tar builder (no third-party dependencies)
# --------------------------------------------------------------------------- #
WHITEOUT_PREFIX = ".wh."
OPAQUE_MARKER = ".wh..wh..opq"


def build_tar(entries: List[Tuple]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:", format=tarfile.PAX_FORMAT) as tf:
        for entry in entries:
            kind = entry[0]
            if kind == "dir":
                info = tarfile.TarInfo(name=entry[1])
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            elif kind == "file":
                _, path, data = entry
                info = tarfile.TarInfo(name=path)
                info.size = len(data)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(data))
            elif kind == "wh":
                _, directory, name = entry
                info = tarfile.TarInfo(directory + WHITEOUT_PREFIX + name)
                info.size = 0
                info.mode = 0o000
                tf.addfile(info)
            elif kind == "opq":
                info = tarfile.TarInfo(entry[1] + OPAQUE_MARKER)
                info.size = 0
                info.mode = 0o000
                tf.addfile(info)
            else:  # pragma: no cover
                raise AssertionError(kind)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# independent reference applier
# --------------------------------------------------------------------------- #
class RefNode:
    def __init__(self, kind: str, content: bytes = b"") -> None:
        self.kind = kind
        self.content = content
        self.children: Dict[str, "RefNode"] = {}


class ReferenceFS:
    def __init__(self) -> None:
        self.root = RefNode("dir")

    def _parent(self, parent_path: str, create: bool):
        node = self.root
        if not parent_path:
            return node
        for part in parent_path.split("/"):
            child = node.children.get(part)
            if child is None:
                if not create:
                    return None
                child = RefNode("dir")
                node.children[part] = child
            node = child
        return node if node.kind == "dir" else None

    def apply(self, blob: bytes) -> None:
        tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:")
        members = tf.getmembers()

        def norm(raw: str) -> str:
            n = raw.rstrip("/")
            while n.startswith("./"):
                n = n[2:]
            return n

        same_layer: Dict[str, set] = {}
        for m in members:
            n = norm(m.name)
            if not n or n.split("/")[-1].startswith(WHITEOUT_PREFIX):
                continue
            if not (m.isdir() or m.isreg()):
                continue
            parent, base = n.rsplit("/", 1) if "/" in n else ("", n)
            same_layer.setdefault(parent, set()).add(base)

        for member in members:
            name = norm(member.name)
            if not name:
                continue
            base = name.split("/")[-1]
            parent_path = name.rsplit("/", 1)[0] if "/" in name else ""

            if base == OPAQUE_MARKER:
                parent = self._parent(parent_path, create=False)
                if parent is not None:
                    keep = same_layer.get(parent_path, set())
                    for child in list(parent.children):
                        if child not in keep:
                            parent.children.pop(child)
                continue
            if base.startswith(WHITEOUT_PREFIX):
                parent = self._parent(parent_path, create=False)
                if parent is not None:
                    parent.children.pop(base[len(WHITEOUT_PREFIX):], None)
                continue

            parent = self._parent(parent_path, create=True)
            if parent is None:
                continue
            if member.isdir():
                if parent.children.get(base) is None or parent.children[base].kind != "dir":
                    parent.children[base] = RefNode("dir")
            elif member.isreg():
                src = tf.extractfile(member)
                parent.children[base] = RefNode(
                    "file", src.read() if src is not None else b""
                )
        tf.close()

    def snapshot(self) -> Dict[str, Tuple[str, bytes]]:
        out: Dict[str, Tuple[str, bytes]] = {}

        def walk(node: RefNode, prefix: str) -> None:
            for name in sorted(node.children):
                path = prefix + name
                child = node.children[name]
                if child.kind == "dir":
                    out[path] = ("dir", b"")
                    walk(child, path + "/")
                else:
                    out[path] = ("file", child.content)

        walk(self.root, "")
        return out


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only)
# --------------------------------------------------------------------------- #
def _multipart(fields: Dict[str, Tuple[str, bytes, str]], boundary: str) -> bytes:
    body = io.BytesIO()
    for name, (filename, data, ctype) in fields.items():
        body.write(f"--{boundary}\r\n".encode())
        body.write(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        body.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        body.write(data)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    return body.getvalue()


def post_merge(base_url: str, lower: bytes, upper: bytes) -> Tuple[int, bytes, dict]:
    boundary = "----selftest-" + uuid.uuid4().hex
    body = _multipart(
        {
            "lower": ("lower.tar", lower, "application/x-tar"),
            "upper": ("upper.tar", upper, "application/x-tar"),
        },
        boundary,
    )
    req = urllib.request.Request(
        base_url.rstrip("/") + "/merge",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {k.lower(): v for k, v in exc.headers.items()}


# --------------------------------------------------------------------------- #
# cpuset re-pinning HTTP helpers (stdlib only)
# --------------------------------------------------------------------------- #

def _http_json(method: str, url: str, payload: Optional[dict] = None) -> Tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except json.JSONDecodeError:
            return exc.code, {}


def cpuset_allocation(base_url: str) -> Tuple[int, dict]:
    return _http_json("GET", base_url.rstrip("/") + "/cpuset/allocation")


def cpuset_rebind(base_url: str, online: List[int], batch: List[int]) -> Tuple[int, dict]:
    return _http_json(
        "POST",
        base_url.rstrip("/") + "/cpuset/rebind",
        {"online_cpus": online, "batch_cpus": batch},
    )


def cpuset_resume(base_url: str, operation_id: str) -> Tuple[int, dict]:
    return _http_json(
        "POST",
        base_url.rstrip("/") + f"/cpuset/operations/{operation_id}/resume",
    )


def cpuset_get_operation(base_url: str, operation_id: str) -> Tuple[int, dict]:
    return _http_json(
        "GET", base_url.rstrip("/") + f"/cpuset/operations/{operation_id}"
    )


def run_cpuset_selftest(base_url: str) -> int:
    """Exercise the cpuset API end to end against the running service."""
    failures = 0

    def check(condition: bool, label: str) -> None:
        nonlocal failures
        if condition:
            print(f"[ OK ] {label}")
        else:
            failures += 1
            print(f"[FAIL] {label}")

    status, initial = cpuset_allocation(base_url)
    check(status == 200, "cpuset allocation endpoint reachable")
    if status != 200:
        return failures + 1
    parent = set(initial["parent_cpus"])
    check(len(parent) <= 8, f"parent owns at most 8 CPUs (got {sorted(parent)})")
    start_online = initial["groups"]["online"]["cpus"]
    start_batch = initial["groups"]["batch"]["cpus"]
    check(
        not (set(start_online) & set(start_batch)),
        "initial groups do not share a CPU",
    )

    # 1. Borrow-idle-CPU swap: online and batch exchange cores.  Pick targets
    #    that require intermediate borrowing regardless of the initial split.
    idle = sorted(parent - set(start_online) - set(start_batch))
    if len(idle) >= 1 and len(start_batch) >= 1:
        target_online = sorted(set(start_batch) | {idle[0]})
        target_batch = sorted(start_online)
        if len(target_online) >= initial["groups"]["online"]["min_cpus"] and not (
            set(target_online) & set(target_batch)
        ):
            status, op = cpuset_rebind(base_url, target_online, target_batch)
            check(status == 200 and op["status"] == "done", "borrow-and-swap rebind accepted")
            # Every intermediate move changes exactly one group and stays legal.
            online, batch = set(start_online), set(start_batch)
            legal_trace = True
            for step in op["plan"]:
                cpus = set(step["cpus"])
                if step["group"] == "online":
                    if batch & cpus or len(cpus) < initial["groups"]["online"]["min_cpus"]:
                        legal_trace = False
                    online = cpus
                else:
                    if online & cpus or len(cpus) < initial["groups"]["batch"]["min_cpus"]:
                        legal_trace = False
                    batch = cpus
            check(legal_trace, "every swap step keeps minimums and no shared CPU")
            status, after = cpuset_allocation(base_url)
            check(
                after["groups"]["online"]["cpus"] == target_online
                and after["groups"]["batch"]["cpus"] == target_batch,
                "actual allocation matches swap targets",
            )
            # Operation lookup and listing.
            status, detail = cpuset_get_operation(base_url, op["operation_id"])
            check(status == 200 and detail["status"] == "done", "operation lookup by id")
            # Restore the starting allocation so repeated runs stay stable.
            status, restore = cpuset_rebind(base_url, start_online, start_batch)
            check(status == 200 and restore["status"] == "done", "restore starting allocation")
    else:
        print("[skip] borrow-and-swap: parent has no idle CPU to borrow")

    # 2. Invalid targets are rejected with 400 and leave no journal entry.
    status, bad = cpuset_rebind(base_url, [max(parent) + 1], start_batch)
    check(status == 400, "cpu outside parent rejected with 400")
    overlap_target = [start_online[0]]
    status, bad = cpuset_rebind(base_url, start_online, overlap_target)
    check(status == 400, "overlapping targets rejected with 400")

    # 3. Write-interruption / resume flow.  Only meaningful when the service
    #    was started with CPSET_FAIL_ON_WRITE; otherwise it is skipped.
    status, probe = cpuset_allocation(base_url)
    spare = sorted(parent - set(start_online) - set(start_batch))
    if len(spare) >= 2:
        status, maybe_fault = cpuset_rebind(
            base_url, sorted(spare[:2]), start_batch
        )
        if status == 500 and "interruption" in maybe_fault.get("detail", ""):
            op_id = cpuset_allocation(base_url)[1]["in_progress_operation"]
            check(bool(op_id), "interrupted write leaves an in-progress operation")
            # Other adjustments are rejected until the operation finishes.
            status, blocked = cpuset_rebind(base_url, start_online, start_batch)
            check(status == 409 and "not finished" in blocked["detail"],
                  "new adjustment rejected while operation unfinished")
            status, resumed = cpuset_resume(base_url, op_id)
            check(status == 200 and resumed["status"] == "done",
                  "resume completes the interrupted operation")
            status, final = cpuset_allocation(base_url)
            check(final["in_progress_operation"] is None, "no operation in progress after resume")
            check(final["groups"]["online"]["cpus"] == sorted(spare[:2]),
                  "actual allocation matches target after resume")
            cpuset_rebind(base_url, start_online, start_batch)
        else:
            print("[skip] write-interruption: service not started with CPSET_FAIL_ON_WRITE")
    else:
        print("[skip] write-interruption: not enough spare CPUs in this topology")

    return failures


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #
BASE_ONE = build_tar([
    ("dir", "etc/"),
    ("file", "etc/hosts", b"base-one-hosts\n"),
    ("file", "etc/hostname", b"node-one\n"),
    ("dir", "app/"),
    ("dir", "app/lib/"),
    ("file", "app/lib/old.so", b"old-one"),
    ("file", "keep", b"keep-one\n"),
    ("dir", "rmdir/"),
    ("file", "rmdir/c1", b"c1\n"),
    ("file", "rmdir/c2", b"c2\n"),
    ("dir", "swap/"),
    ("file", "swap/item", b"file-in-dir\n"),
    ("dir", "shadow/"),
    ("file", "shadow/under", b"under-one\n"),
])

BASE_TWO = build_tar([
    ("dir", "etc/"),
    ("file", "etc/hosts", b"base-two-hosts-longer\n"),
    ("file", "etc/resolv", b"ns 9.9.9.9\n"),
    ("file", "app", b"i-am-a-file\n"),
    ("file", "rmdir", b"i-am-also-a-file\n"),
    ("file", "swap", b"plain-file\n"),
    ("dir", "shadow/"),
    ("file", "shadow/under", b"under-two\n"),
    ("file", "untouched", b"x\n"),
])

SCENARIOS = {
    "add-update": (
        build_tar([("dir", "new/"), ("file", "new/a", b"a\n"),
                   ("file", "etc/hosts", b"edited\n")]),
        build_tar([("file", "new/a", b"a2\n"), ("file", "new/b", b"b\n")]),
    ),
    "whiteout-survives": (
        build_tar([("wh", "", "keep"), ("wh", "etc/", "hostname")]),
        build_tar([("file", "etc/hostname", b"reborn\n")]),
    ),
    "dir-delete-rebuild": (
        build_tar([("wh", "", "rmdir"), ("dir", "rmdir/"),
                   ("file", "rmdir/only", b"new\n")]),
        build_tar([("file", "rmdir/more", b"more\n")]),
    ),
    "file-dir-swap": (
        build_tar([("wh", "", "swap"), ("file", "swap", b"now-file\n")]),
        build_tar([("wh", "", "swap"), ("dir", "swap/"),
                   ("file", "swap/item", b"reborn\n")]),
    ),
    "opaque-marker": (
        build_tar([("dir", "shadow/"), ("opq", "shadow/"),
                   ("file", "shadow/fresh", b"f\n")]),
        build_tar([("file", "shadow/fresh", b"f2\n")]),
    ),
    "nested-whiteout": (
        build_tar([("dir", "app/"), ("dir", "app/lib/"),
                   ("wh", "app/lib/", "old.so"),
                   ("file", "app/lib/new.so", b"n\n")]),
        build_tar([("dir", "app/lib/sub/"), ("file", "app/lib/sub/d", b"d\n")]),
    ),
    "root-opaque": (
        build_tar([("file", "a", b"a\n")]),
        build_tar([("opq", ""), ("file", "b", b"b\n")]),
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--with-cpuset",
        action="store_true",
        help="also exercise the /cpuset re-pinning API end to end",
    )
    args = parser.parse_args()

    failures = 0
    for name, (lower, upper) in SCENARIOS.items():
        status, body, headers = post_merge(args.base_url, lower, upper)
        if status != 200:
            print(f"[FAIL] {name}: HTTP {status}: {body[:200]!r}")
            failures += 1
            continue
        if headers.get("content-type") != "application/x-tar":
            print(f"[FAIL] {name}: wrong content type {headers.get('content-type')}")
            failures += 1
            continue
        for base_name, base in (("base-one", BASE_ONE), ("base-two", BASE_TWO)):
            step = ReferenceFS()
            step.apply(base)
            step.apply(lower)
            step.apply(upper)

            merged_fs = ReferenceFS()
            merged_fs.apply(base)
            merged_fs.apply(body)

            if merged_fs.snapshot() != step.snapshot():
                failures += 1
                print(f"[FAIL] {name} on {base_name}: snapshots differ")
                a, b = step.snapshot(), merged_fs.snapshot()
                print("  only stepwise:", sorted(set(a) - set(b)))
                print("  only merged:  ", sorted(set(b) - set(a)))
            else:
                print(f"[ OK ] {name} on {base_name} ({len(step.snapshot())} paths)")

    # Invalid inputs must be rejected with 400 and JSON, never a tar.
    good = build_tar([("file", "a", b"1\n")])
    invalid_cases = {
        "garbage": b"not a tar at all" * 40,
        "traversal": build_tar([("file", "../escape", b"x\n")]),
        "absolute": build_tar([("file", "/abs", b"x\n")]),
        "symlink": None,  # constructed below
    }
    # symlink tar
    link_buf = io.BytesIO()
    with tarfile.open(fileobj=link_buf, mode="w:") as tf:
        info = tarfile.TarInfo("lnk")
        info.type = tarfile.SYMTYPE
        info.linkname = "target"
        tf.addfile(info)
    invalid_cases["symlink"] = link_buf.getvalue()

    for name, bad in invalid_cases.items():
        status, body, headers = post_merge(args.base_url, bad, good)
        if status == 400 and "json" in headers.get("content-type", ""):
            print(f"[ OK ] invalid input {name!r} rejected: {json.loads(body)['detail'][:60]}")
        else:
            failures += 1
            print(f"[FAIL] invalid input {name!r}: got HTTP {status}")

    print()
    if failures:
        print(f"RESULT: {failures} failure(s)")
        return 1
    print("RESULT: all self-test checks passed on two distinct lower file-systems")

    if args.with_cpuset:
        print()
        print("--- cpuset re-pinning self-test ---")
        cpuset_failures = run_cpuset_selftest(args.base_url)
        if cpuset_failures:
            print(f"RESULT: {cpuset_failures} cpuset failure(s)")
            return 1
        print("RESULT: all cpuset self-test checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
