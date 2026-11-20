#!/usr/bin/env python
"""End-to-end self-test for the running OCI layer merger service.

The script builds several layer pairs plus *two different lower file-systems*,
posts the layers to ``/merge``, downloads the resulting tar, and then verifies
on each lower file-system that:

    base + lower + upper   ==   base + merged

in terms of path set, entry type and file contents.

It also exercises the invalid-input paths and confirms those return HTTP 400
with no downloadable artifact.

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
from typing import Dict, List, Tuple

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
