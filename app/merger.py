"""Merge two consecutive, uncompressed OCI image diff layers into one.

The merger understands the three member classes allowed by the service
contract:

* regular files
* directories
* whiteout files (``.wh.<name>``) including the opaque directory marker
  (``.wh..wh..opq``)

The merged layer is itself an uncompressed tar that passes the same
validation as the inputs and is semantically equivalent to applying the two
input layers one after another on *any* lower layer.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from typing import BinaryIO, Dict, Optional

WHITEOUT_PREFIX = ".wh."
OPAQUE_MARKER = ".wh..wh..opq"

# Magic numbers of common compression formats.  The service only accepts
# uncompressed tar streams.
_COMPRESSION_MAGIC = (
    b"\x1f\x8b",                 # gzip
    b"BZh",                      # bzip2
    b"\xfd7zXZ\x00",             # xz
    b"\x28\xb5\x2f\xfd",         # zstd
)


class InvalidLayer(ValueError):
    """Raised when an uploaded layer cannot be processed."""


@dataclass
class Node:
    """A single path in the accumulated, merged file-system tree."""

    kind: str  # 'file' | 'dir' | 'whiteout'
    origin: int  # layer index (1 = lower input, 2 = upper input)
    mode: int = 0o644
    uid: int = 0
    gid: int = 0
    uname: str = ""
    gname: str = ""
    mtime: int = 0
    opaque: bool = False
    spool: Optional[str] = None  # spooled regular-file content
    size: int = 0


class LayerMerger:
    """Accumulates two layers and serializes the merged result."""

    def __init__(self) -> None:
        self.nodes: Dict[str, Node] = {}
        self.root_opaque: bool = False
        self._tmpdir = tempfile.mkdtemp(prefix="layermerger-")
        self._spool_seq = 0

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self) -> "LayerMerger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # tar validation / ingestion
    # ------------------------------------------------------------------ #
    def add_layer(self, tar_path: str, origin: int) -> None:
        """Apply one uncompressed tar layer on top of the current state."""
        if origin not in (1, 2):
            raise ValueError("origin must be 1 or 2")
        self._reject_compression(tar_path)
        try:
            tf = tarfile.open(name=tar_path, mode="r:")
        except tarfile.ReadError as exc:
            raise InvalidLayer(f"input is not a valid uncompressed tar: {exc}") from exc
        total_size = os.path.getsize(tar_path)
        try:
            while True:
                try:
                    member = tf.next()
                except tarfile.ReadError as exc:
                    raise InvalidLayer(f"truncated or malformed tar: {exc}") from exc
                if member is None:
                    break
                self._ingest_member(tf, member, origin)
            # Unconditionally re-parse headers by offset: tarfile may stop
            # iteration early when a truncated payload leaves NUL blocks that
            # look like an end-of-archive marker.
            self._assert_proper_tar_end(tf, total_size)
        finally:
            tf.close()

    @staticmethod
    def _assert_proper_tar_end(tf: tarfile.TarFile, total_size: int) -> None:
        """Reject truncated or malformed tar streams.

        The archive is walked again using only header offsets and the sizes
        recorded in those headers, so data content can never be mistaken for
        a header or for the end-of-archive marker.
        """
        fobj = tf.fileobj
        if fobj is None:
            return
        end_blocks = tarfile.BLOCKSIZE * 2
        if total_size < end_blocks:
            raise InvalidLayer("truncated tar: missing end-of-archive blocks")

        expected_end = 0
        saw_member = False
        fobj.seek(0)
        while True:
            header_pos = fobj.tell()
            header = fobj.read(tarfile.BLOCKSIZE)
            if len(header) < tarfile.BLOCKSIZE:
                raise InvalidLayer("truncated tar: incomplete header block")
            if header == b"\x00" * tarfile.BLOCKSIZE:
                break
            try:
                info = tarfile.TarInfo.frombuf(
                    header, encoding="utf-8", errors="surrogateescape"
                )
            except (tarfile.HeaderError, tarfile.InvalidHeaderError) as exc:
                raise InvalidLayer(f"malformed tar header: {exc}") from exc
            saw_member = True
            data_blocks = (info.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE
            expected_end = header_pos + tarfile.BLOCKSIZE + data_blocks * tarfile.BLOCKSIZE
            if expected_end > total_size:
                raise InvalidLayer(
                    "truncated tar: member payload extends past end of file"
                )
            fobj.seek(expected_end)

        if not saw_member:
            fobj.seek(0)
            if fobj.read(end_blocks) != b"\x00" * end_blocks:
                raise InvalidLayer("truncated tar: missing end-of-archive blocks")
            return

        if expected_end + end_blocks > total_size:
            raise InvalidLayer("truncated tar: missing end-of-archive blocks")
        fobj.seek(expected_end)
        if fobj.read(end_blocks) != b"\x00" * end_blocks:
            raise InvalidLayer("truncated tar: missing end-of-archive blocks")

    @staticmethod
    def _reject_compression(tar_path: str) -> None:
        with open(tar_path, "rb") as fh:
            head = fh.read(6)
        for magic in _COMPRESSION_MAGIC:
            if head.startswith(magic):
                raise InvalidLayer(
                    "compressed layers are not supported; upload an uncompressed tar"
                )

    def _ingest_member(
        self, tf: tarfile.TarFile, member: tarfile.TarInfo, origin: int
    ) -> None:
        name = self._normalize_name(member.name)
        if name is None:
            return  # explicit root entry (".", "/"): the root is implicit

        base = posixpath.basename(name)
        parent = posixpath.dirname(name)

        if base == OPAQUE_MARKER:
            self._ingest_opaque(parent, member, origin)
            return

        if base.startswith(WHITEOUT_PREFIX):
            self._ingest_whiteout(parent, base, member, origin)
            return

        if member.isdir():
            self._ingest_directory(name, member, origin)
        elif member.isreg():
            self._ingest_file(tf, name, member, origin)
        else:
            raise InvalidLayer(
                f"unsupported member type for {name!r}: only regular files, "
                "directories and whiteout files are allowed"
            )

    # ------------------------------------------------------------------ #
    # member handlers
    # ------------------------------------------------------------------ #
    def _ingest_opaque(
        self, dir_path: str, member: tarfile.TarInfo, origin: int
    ) -> None:
        if not member.isreg() or member.size != 0:
            raise InvalidLayer(
                f"opaque marker {member.name!r} must be a zero-size regular file"
            )
        if dir_path:
            node = self._ensure_dir(dir_path, origin)
            if node.kind != "dir":
                raise InvalidLayer(
                    f"opaque marker {member.name!r} targets a non-directory path"
                )
            node.opaque = True
            # The marker hides everything below this directory that came from
            # a lower layer.  Entries of the *current* layer are always kept,
            # regardless of their order inside the tar (OCI/runc semantics).
            for path in [p for p in self.nodes if p.startswith(dir_path + "/")]:
                if self.nodes[path].origin < origin:
                    self._delete_subtree(path)
        else:
            # Opaque marker at the archive root.
            self.root_opaque = True
            for path in [p for p in self.nodes if self.nodes[p].origin < origin]:
                self._delete_subtree(path)

    def _ingest_whiteout(
        self,
        parent: str,
        base: str,
        member: tarfile.TarInfo,
        origin: int,
    ) -> None:
        target_base = base[len(WHITEOUT_PREFIX):]
        if not target_base:
            raise InvalidLayer(f"whiteout with empty name: {member.name!r}")
        if target_base.startswith(WHITEOUT_PREFIX) and target_base != OPAQUE_MARKER:
            raise InvalidLayer(f"whiteout targeting reserved name: {member.name!r}")
        if not member.isreg() or member.size != 0:
            raise InvalidLayer(
                f"whiteout {member.name!r} must be a zero-size regular file"
            )
        target = target_base if not parent else parent + "/" + target_base

        if parent:
            parent_node = self._ensure_dir(parent, origin)
            if parent_node.kind != "dir":
                raise InvalidLayer(
                    f"whiteout {member.name!r} lives below a non-directory path"
                )
            # An opaque ancestor already hides every lower child, so an
            # explicit whiteout underneath it carries no additional meaning.
            if parent_node.opaque:
                self._delete_subtree(target)
                return

        self._delete_subtree(target)
        self.nodes[target] = Node(kind="whiteout", origin=origin)

    def _ingest_directory(
        self, name: str, member: tarfile.TarInfo, origin: int
    ) -> None:
        parent = posixpath.dirname(name)
        if parent:
            parent_node = self._ensure_dir(parent, origin)
            if parent_node.kind != "dir":
                raise InvalidLayer(
                    f"directory {name!r} cannot be created below non-directory {parent!r}"
                )
        existing = self.nodes.get(name)
        if existing is None:
            # Brand-new directory: transparent, no lower children to hide.
            self.nodes[name] = Node(
                kind="dir", origin=origin, **self._meta(member)
            )
        elif existing.kind == "whiteout":
            # The whiteout stands in for a lower directory (or file).  A
            # directory rebuilt on that spot must be opaque so any lower
            # directory children stay hidden on every possible base layer.
            self.nodes[name] = Node(
                kind="dir",
                origin=origin,
                opaque=True,
                **self._meta(member),
            )
        elif existing.kind == "file":
            # File -> directory swap: hide any lower children as well.
            self._delete_subtree(name)
            self.nodes[name] = Node(
                kind="dir", origin=origin, opaque=True, **self._meta(member)
            )
        else:  # existing directory: refresh metadata, keep opacity/children
            existing.__dict__.update(self._meta(member))

    def _ingest_file(
        self,
        tf: tarfile.TarFile,
        name: str,
        member: tarfile.TarInfo,
        origin: int,
    ) -> None:
        parent = posixpath.dirname(name)
        if parent:
            parent_node = self._ensure_dir(parent, origin)
            if parent_node.kind != "dir":
                raise InvalidLayer(
                    f"file {name!r} cannot be created below non-directory {parent!r}"
                )
        self._spool_seq += 1
        spool_path = os.path.join(
            self._tmpdir, f"f-{origin}-{self._spool_seq:08d}"
        )
        src = tf.extractfile(member)
        if src is None:
            raise InvalidLayer(f"could not read content of {name!r}")
        read_total = 0
        with open(spool_path, "wb") as dst:
            while True:
                try:
                    chunk = src.read(1024 * 1024)
                except tarfile.ReadError as exc:
                    raise InvalidLayer(f"truncated content for {name!r}: {exc}") from exc
                if not chunk:
                    break
                read_total += len(chunk)
                dst.write(chunk)
        if read_total != member.size:
            raise InvalidLayer(
                f"truncated content for {name!r}: expected {member.size} bytes, "
                f"got {read_total}"
            )

        self._delete_subtree(name)
        self.nodes[name] = Node(
            kind="file",
            origin=origin,
            spool=spool_path,
            size=member.size,
            **self._meta(member),
        )

    # ------------------------------------------------------------------ #
    # tree helpers
    # ------------------------------------------------------------------ #
    def _ensure_dir(self, path: str, origin: int) -> Node:
        node = self.nodes.get(path)
        if node is not None:
            return node
        parent = posixpath.dirname(path)
        if parent:
            parent_node = self._ensure_dir(parent, origin)
            if parent_node.kind != "dir":
                raise InvalidLayer(f"path {path!r} conflicts with non-directory {parent!r}")
        node = Node(kind="dir", origin=origin, mode=0o755, mtime=0)
        self.nodes[path] = node
        return node

    def _delete_subtree(self, path: str) -> None:
        for victim in [p for p in self.nodes if p == path or p.startswith(path + "/")]:
            self.nodes.pop(victim, None)

    @staticmethod
    def _normalize_name(raw: str) -> Optional[str]:
        name = raw
        if "\x00" in name or "\\" in name:
            raise InvalidLayer(f"illegal characters in member name: {raw!r}")
        if name.startswith("/"):
            raise InvalidLayer(f"absolute member names are not allowed: {raw!r}")
        # Normalise leading "./" sequences only.
        while name.startswith("./"):
            name = name[2:]
        if name.endswith("/"):
            name = name[:-1]
        if name in ("", "."):
            return None
        for part in name.split("/"):
            if part in ("", ".", ".."):
                raise InvalidLayer(
                    f"unsafe member name (empty or '..' component): {raw!r}"
                )
        return name

    @staticmethod
    def _meta(member: tarfile.TarInfo) -> dict:
        return {
            "mode": member.mode,
            "uid": member.uid,
            "gid": member.gid,
            "uname": member.uname or "",
            "gname": member.gname or "",
            "mtime": int(member.mtime),
        }

    # ------------------------------------------------------------------ #
    # output
    # ------------------------------------------------------------------ #
    def write_tar(self, out: BinaryIO) -> None:
        """Serialize the merged layer deterministically to ``out``."""
        with tarfile.open(fileobj=out, mode="w:", format=tarfile.PAX_FORMAT) as tf:
            if self.root_opaque:
                tf.addfile(self._marker_info(OPAQUE_MARKER))

            emitted_dirs: set[str] = set()
            for path in sorted(self.nodes):
                node = self.nodes[path]
                if node.kind == "dir":
                    self._emit_dir(tf, path, node, emitted_dirs)

            # Files and whiteouts are emitted after their parent directories;
            # a directory's opaque marker precedes its other children.
            for path in sorted(self.nodes):
                node = self.nodes[path]
                if node.kind == "file":
                    self._emit_file(tf, path, node)
                elif node.kind == "whiteout":
                    parent = posixpath.dirname(path)
                    base = posixpath.basename(path)
                    tf.addfile(
                        self._marker_info(
                            (parent + "/" if parent else "") + WHITEOUT_PREFIX + base
                        )
                    )

    def _emit_dir(
        self, tf: tarfile.TarFile, path: str, node: Node, emitted: set[str]
    ) -> None:
        if path in emitted:
            return
        parent = posixpath.dirname(path)
        if parent:
            parent_node = self.nodes.get(parent)
            if parent_node is not None and parent_node.kind == "dir":
                self._emit_dir(tf, parent, parent_node, emitted)
        info = tarfile.TarInfo(name=path + "/")
        info.type = tarfile.DIRTYPE
        info.mode = node.mode
        info.uid = node.uid
        info.gid = node.gid
        info.uname = node.uname
        info.gname = node.gname
        info.mtime = node.mtime
        tf.addfile(info)
        emitted.add(path)
        if node.opaque:
            tf.addfile(self._marker_info(path + "/" + OPAQUE_MARKER))

    def _emit_file(self, tf: tarfile.TarFile, path: str, node: Node) -> None:
        assert node.spool is not None
        info = tarfile.TarInfo(name=path)
        info.type = tarfile.REGTYPE
        info.size = node.size
        info.mode = node.mode
        info.uid = node.uid
        info.gid = node.gid
        info.uname = node.uname
        info.gname = node.gname
        info.mtime = node.mtime
        with open(node.spool, "rb") as fh:
            tf.addfile(info, fh)

    @staticmethod
    def _marker_info(name: str) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name=name)
        info.type = tarfile.REGTYPE
        info.size = 0
        info.mode = 0o000
        info.uid = 0
        info.gid = 0
        info.mtime = 0
        return info


def merge_layers(lower_tar: str, upper_tar: str, output: BinaryIO) -> None:
    """Convenience wrapper: merge two spooled tar files into ``output``."""
    with LayerMerger() as merger:
        merger.add_layer(lower_tar, origin=1)
        merger.add_layer(upper_tar, origin=2)
        merger.write_tar(output)
