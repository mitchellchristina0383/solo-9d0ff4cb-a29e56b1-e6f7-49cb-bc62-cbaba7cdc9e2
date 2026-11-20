"""Helpers for the test-suite: tar builders and an independent reference
implementation of OCI layer application semantics.

The reference applier is deliberately written independently of
``app.merger`` (a tree of mutable nodes rather than the flat dictionary
used by the production code), so agreement between both is a meaningful
cross-check rather than a tautology.
"""

from __future__ import annotations

import io
import os
import tarfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

WHITEOUT_PREFIX = ".wh."
OPAQUE_MARKER = ".wh..wh..opq"


# --------------------------------------------------------------------------- #
# tar construction
# --------------------------------------------------------------------------- #
def build_tar(entries: List[Tuple]) -> bytes:
    """Build an uncompressed tar in memory.

    Each entry is one of:
      ("dir",  "path/")                 directory
      ("file", "path", b"content")      regular file
      ("wh",   "dir/", "name")          whiteout .wh.name inside dir/
      ("opq",  "dir/")                  opaque marker inside dir/
    """
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
                path = directory + WHITEOUT_PREFIX + name
                info = tarfile.TarInfo(name=path)
                info.size = 0
                info.mode = 0o000
                tf.addfile(info)
            elif kind == "opq":
                info = tarfile.TarInfo(name=entry[1] + OPAQUE_MARKER)
                info.size = 0
                info.mode = 0o000
                tf.addfile(info)
            else:  # pragma: no cover
                raise AssertionError(f"unknown builder entry kind: {kind}")
    return buf.getvalue()


def corrupt_tar(data: bytes) -> bytes:
    """Truncate a valid tar so that reads fail partway through."""
    return data[: len(data) // 2]


# --------------------------------------------------------------------------- #
# independent reference layer applier
# --------------------------------------------------------------------------- #
@dataclass
class RefNode:
    kind: str  # 'file' | 'dir'
    content: bytes = b""
    children: Dict[str, "RefNode"] = field(default_factory=dict)


class ReferenceFS:
    """Reference OCI layer applier over an in-memory file-system tree."""

    def __init__(self) -> None:
        self.root = RefNode(kind="dir")

    def _walk_to(self, path: str, create_dirs: bool) -> Optional[RefNode]:
        node = self.root
        for part in path.split("/"):
            if node.kind != "dir":
                return None
            child = node.children.get(part)
            if child is None:
                if not create_dirs:
                    return None
                child = RefNode(kind="dir")
                node.children[part] = child
            node = child
        return node

    @staticmethod
    def _remove(node: RefNode, name: str) -> None:
        node.children.pop(name, None)

    def apply(self, tar_bytes: bytes) -> None:
        tf = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:")
        members = tf.getmembers()

        def norm(raw: str) -> str:
            n = raw.rstrip("/")
            while n.startswith("./"):
                n = n[2:]
            while n.startswith("/"):
                n = n[1:]
            return n

        # Paths materialised (file/dir) by this very tar, grouped by parent.
        # Opaque markers only hide *lower-layer* content, never same-tar
        # siblings, even if the sibling precedes the marker in the archive
        # (OCI/runc semantics).
        same_layer: dict[str, set[str]] = {}
        for m in members:
            n = norm(m.name)
            if not n or n.split("/")[-1].startswith(WHITEOUT_PREFIX):
                continue
            if not (m.isdir() or m.isreg()):
                continue
            parent, base = n.rsplit("/", 1) if "/" in n else ("", n)
            same_layer.setdefault(parent, set()).add(base)

        try:
            for member in members:
                name = norm(member.name)
                if name in ("", "."):
                    continue
                parts = name.split("/")
                base = parts[-1]
                parent_path = "/".join(parts[:-1])

                if base == OPAQUE_MARKER:
                    parent = (
                        self._walk_to(parent_path, create_dirs=False)
                        if parent_path
                        else self.root
                    )
                    if parent is not None and parent.kind == "dir":
                        keep = same_layer.get(parent_path, set())
                        for child in list(parent.children):
                            if child not in keep:
                                parent.children.pop(child)
                    continue

                if base.startswith(WHITEOUT_PREFIX):
                    target = base[len(WHITEOUT_PREFIX):]
                    parent = (
                        self._walk_to(parent_path, create_dirs=False)
                        if parent_path
                        else self.root
                    )
                    if parent is not None and parent.kind == "dir":
                        self._remove(parent, target)
                    continue

                parent = self.root
                if parent_path:
                    parent = self._walk_to(parent_path, create_dirs=True)
                    if parent is None or parent.kind != "dir":
                        continue

                if member.isdir():
                    existing = parent.children.get(base)
                    if existing is None or existing.kind != "dir":
                        parent.children[base] = RefNode(kind="dir")
                elif member.isreg():
                    src = tf.extractfile(member)
                    data = src.read() if src is not None else b""
                    parent.children[base] = RefNode(kind="file", content=data)
        finally:
            tf.close()

    def snapshot(self) -> Dict[str, Tuple[str, bytes]]:
        """Return {path: (kind, content)} for every visible entry."""
        result: Dict[str, Tuple[str, bytes]] = {}

        def walk(node: RefNode, prefix: str) -> None:
            for name in sorted(node.children):
                path = f"{prefix}{name}"
                child = node.children[name]
                if child.kind == "dir":
                    result[path] = ("dir", b"")
                    walk(child, path + "/")
                else:
                    result[path] = ("file", child.content)

        walk(self.root, "")
        return result


def apply_sequence(base_tars: List[bytes], layer_tars: List[bytes]) -> Dict[str, Tuple[str, bytes]]:
    fs = ReferenceFS()
    for blob in base_tars + layer_tars:
        fs.apply(blob)
    return fs.snapshot()
