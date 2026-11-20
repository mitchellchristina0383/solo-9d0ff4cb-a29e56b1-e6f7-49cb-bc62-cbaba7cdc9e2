"""Equivalence tests for the layer merger.

For every scenario we compare, on *two different lower file-systems*:

    base + lower + upper      (reference, layer-by-layer)
    base + merged             (reference, single merged layer)

The resulting path sets, entry kinds and file contents must be identical.
"""

from __future__ import annotations

import io
import os
import tarfile
import tempfile

import pytest

from app.merger import InvalidLayer, LayerMerger
from tests.helpers import (
    ReferenceFS,
    apply_sequence,
    build_tar,
)


# --------------------------------------------------------------------------- #
# two deliberately different lower layers (bases)
# --------------------------------------------------------------------------- #
BASE_A = build_tar([
    ("dir", "etc/"),
    ("file", "etc/hosts", b"baseA-hosts\n"),
    ("file", "etc/hostname", b"node-a\n"),
    ("dir", "app/"),
    ("dir", "app/lib/"),
    ("file", "app/lib/old.so", b"old-so-base-a"),
    ("file", "app/version", b"1\n"),
    ("dir", "swap/"),
    ("file", "swap/item", b"i-was-a-file\n"),
    ("file", "keep", b"baseA-keep\n"),
    ("dir", "rmdir/"),
    ("file", "rmdir/child1", b"c1\n"),
    ("file", "rmdir/child2", b"c2\n"),
    ("dir", "shadow/"),
    ("file", "shadow/below", b"below-a\n"),
    ("file", "shadow/below2", b"below2-a\n"),
])

BASE_B = build_tar([
    ("dir", "etc/"),
    ("file", "etc/hosts", b"baseB-hosts-longer-content\n"),
    ("file", "etc/resolv.conf", b"nameserver 8.8.8.8\n"),
    ("dir", "app/"),
    ("file", "app/lib/old.so", b"old-so-base-b"),
    ("dir", "app/extra/"),
    ("file", "app/extra/x", b"x\n"),
    ("file", "swap", b"i-was-a-dir-parent\n"),
    ("file", "rmdir", b"i-am-a-file-not-a-dir\n"),
    ("dir", "shadow/"),
    ("file", "shadow/below", b"below-b\n"),
    ("dir", "unrelated/"),
    ("file", "unrelated/data", b"d\n"),
])


# --------------------------------------------------------------------------- #
# merger invocation helper
# --------------------------------------------------------------------------- #
def merge_bytes(lower: bytes, upper: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        lo = os.path.join(tmp, "lower.tar")
        up = os.path.join(tmp, "upper.tar")
        out = os.path.join(tmp, "merged.tar")
        with open(lo, "wb") as fh:
            fh.write(lower)
        with open(up, "wb") as fh:
            fh.write(upper)
        with LayerMerger() as merger:
            merger.add_layer(lo, origin=1)
            merger.add_layer(up, origin=2)
            with open(out, "wb") as fh:
                merger.write_tar(fh)
        with open(out, "rb") as fh:
            return fh.read()


def assert_equivalent(layer1: bytes, layer2: bytes) -> None:
    """Compare (base, l1, l2) vs (base, merged) on both base file-systems."""
    merged = merge_bytes(layer1, layer2)

    # The merged artifact must itself be a valid, uncompressed tar.
    with tarfile.open(fileobj=io.BytesIO(merged), mode="r:") as tf:
        names = tf.getnames()
    assert len(names) == len(set(names)), "duplicate members in merged tar"

    for base in (BASE_A, BASE_B):
        stepwise = apply_sequence([base], [layer1, layer2])
        combined = apply_sequence([base], [merged])
        assert combined == stepwise, (
            "merged layer diverges from stepwise application\n"
            f"only stepwise: {set(stepwise) - set(combined)}\n"
            f"only merged:   {set(combined) - set(stepwise)}\n"
            f"diff values:   "
            f"{ {k: (stepwise.get(k), combined.get(k)) for k in stepwise.keys() & combined.keys() if stepwise[k] != combined[k]} }"
        )


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #
def test_simple_file_add_and_update():
    lower = build_tar([
        ("dir", "newdir/"),
        ("file", "newdir/a", b"aaa\n"),
        ("file", "app/version", b"2\n"),
    ])
    upper = build_tar([
        ("file", "newdir/a", b"aaa-v2\n"),
        ("file", "newdir/b", b"bbb\n"),
    ])
    assert_equivalent(lower, upper)


def test_whiteout_survives_into_merged_layer():
    lower = build_tar([
        ("wh", "", "keep"),
        ("dir", "app/"),
        ("wh", "app/", "version"),
    ])
    upper = build_tar([
        ("file", "app/version", b"3\n"),  # re-create what lower deleted
    ])
    assert_equivalent(lower, upper)


def test_whiteout_from_lower_still_hides_base():
    # Lower deletes a base path, upper never mentions it: the merged layer
    # must still carry a whiteout so every base layer keeps hiding it.
    lower = build_tar([("wh", "", "keep")])
    upper = build_tar([("file", "other", b"x\n")])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    assert ".wh.keep" in tarfile.open(fileobj=io.BytesIO(merged), mode="r:").getnames()


def test_directory_delete_then_rebuild():
    # rmdir exists as a directory in BASE_A and as a file in BASE_B.
    lower = build_tar([
        ("wh", "", "rmdir"),
        ("dir", "rmdir/"),
        ("file", "rmdir/only", b"new-content\n"),
    ])
    upper = build_tar([
        ("file", "rmdir/more", b"more\n"),
    ])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    names = tarfile.open(fileobj=io.BytesIO(merged), mode="r:").getnames()
    # Rebuilt directory must be opaque so old children (BASE_A) disappear.
    assert "rmdir/.wh..wh..opq" in names


def test_file_directory_swap_both_directions():
    # BASE_A: swap/ is a dir containing a file; BASE_B: swap is a file.
    lower = build_tar([
        ("wh", "", "swap"),
        ("file", "swap", b"now-a-file\n"),
    ])
    upper = build_tar([
        ("wh", "", "swap"),
        ("dir", "swap/"),
        ("file", "swap/item", b"reborn\n"),
    ])
    assert_equivalent(lower, upper)

    # reverse: lower makes dir, upper turns it back into a file
    lower2 = build_tar([
        ("wh", "", "swap"),
        ("dir", "swap/"),
        ("file", "swap/a", b"a\n"),
    ])
    upper2 = build_tar([
        ("wh", "", "swap"),
        ("file", "swap", b"file-again\n"),
    ])
    assert_equivalent(lower2, upper2)


def test_opaque_directory_marker_hides_base_children():
    lower = build_tar([
        ("dir", "shadow/"),
        ("opq", "shadow/"),
        ("file", "shadow/new", b"new\n"),
    ])
    upper = build_tar([
        ("file", "shadow/new", b"new2\n"),
        ("file", "shadow/extra", b"e\n"),
    ])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    names = tarfile.open(fileobj=io.BytesIO(merged), mode="r:").getnames()
    assert "shadow/.wh..wh..opq" in names
    # No redundant whiteouts should leak out below an opaque directory.
    assert not any(n.startswith("shadow/.wh.") and n != "shadow/.wh..wh..opq" for n in names)


def test_opaque_marker_in_upper_over_lower_children():
    lower = build_tar([
        ("dir", "shadow/"),
        ("file", "shadow/from-lower", b"lower\n"),
    ])
    upper = build_tar([
        ("opq", "shadow/"),
        ("file", "shadow/from-upper", b"upper\n"),
    ])
    assert_equivalent(lower, upper)


def test_opaque_marker_does_not_remove_same_layer_siblings():
    lower = build_tar([("file", "etc/hosts", b"x\n")])
    # Marker first, same-layer sibling afterwards: the canonical OCI layout.
    upper = build_tar([
        ("dir", "etc/"),
        ("opq", "etc/"),
        ("file", "etc/keep-me", b"yes\n"),
    ])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    fs = ReferenceFS()
    fs.apply(BASE_A)
    fs.apply(merged)
    snap = fs.snapshot()
    assert snap["etc/keep-me"] == ("file", b"yes\n")
    assert "etc/hosts" not in snap  # base content hidden
    assert "etc/hostname" not in snap


def test_opaque_marker_keeps_same_layer_sibling_even_when_ordered_after():
    # Some toolchains emit the marker after siblings; OCI semantics still
    # guarantee that same-layer content survives.
    lower = build_tar([("file", "etc/hosts", b"x\n")])
    upper = build_tar([
        ("dir", "etc/"),
        ("file", "etc/keep-me", b"yes\n"),
        ("opq", "etc/"),
    ])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    fs = ReferenceFS()
    fs.apply(BASE_A)
    fs.apply(merged)
    snap = fs.snapshot()
    assert snap["etc/keep-me"] == ("file", b"yes\n")
    assert "etc/hosts" not in snap


def test_nested_whiteouts_and_grandchildren():
    lower = build_tar([
        ("dir", "app/"),
        ("dir", "app/lib/"),
        ("wh", "app/lib/", "old.so"),
        ("file", "app/lib/new.so", b"new\n"),
    ])
    upper = build_tar([
        ("wh", "app/", "extra"),
        ("dir", "app/lib/sub/"),
        ("file", "app/lib/sub/deep", b"deep\n"),
    ])
    assert_equivalent(lower, upper)


def test_whiteout_of_directory_with_base_grandchildren():
    lower = build_tar([("wh", "app/", "lib")])
    upper = build_tar([
        ("dir", "app/lib/"),
        ("file", "app/lib/rebuilt", b"r\n"),
    ])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    names = tarfile.open(fileobj=io.BytesIO(merged), mode="r:").getnames()
    assert "app/lib/.wh..wh..opq" in names


def test_whiteout_then_rebuild_across_two_layers_hides_grandchildren():
    # Lower layer removes a base directory; upper rebuilds it.  The rebuilt
    # directory must be opaque against *every* base layer it can meet.
    lower = build_tar([("wh", "", "rmdir")])
    upper = build_tar([
        ("dir", "rmdir/"),
        ("file", "rmdir/fresh", b"f\n"),
    ])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    names = tarfile.open(fileobj=io.BytesIO(merged), mode="r:").getnames()
    assert "rmdir/.wh..wh..opq" in names

    fs = ReferenceFS()
    fs.apply(BASE_A)  # BASE_A has rmdir/child1 and rmdir/child2
    fs.apply(merged)
    snap = fs.snapshot()
    assert snap["rmdir/fresh"] == ("file", b"f\n")
    assert "rmdir/child1" not in snap
    assert "rmdir/child2" not in snap


def test_root_opaque_marker():
    lower = build_tar([("file", "a", b"a\n")])
    upper = build_tar([
        ("opq", ""),
        ("file", "b", b"b\n"),
    ])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    names = tarfile.open(fileobj=io.BytesIO(merged), mode="r:").getnames()
    assert ".wh..wh..opq" in names


def test_empty_layers():
    assert_equivalent(build_tar([]), build_tar([]))
    assert_equivalent(build_tar([]), build_tar([("file", "x", b"1\n")]))
    assert_equivalent(build_tar([("file", "x", b"1\n")]), build_tar([]))


def test_directory_then_file_with_same_path_in_upper():
    lower = build_tar([
        ("dir", "d/"),
        ("file", "d/child", b"c\n"),
    ])
    upper = build_tar([("file", "d", b"i-am-a-file\n")])
    assert_equivalent(lower, upper)


def test_file_then_directory_replacement_is_opaque():
    # BASE_B has swap as a file; BASE_A has swap/ as a dir.
    lower = build_tar([("file", "swap", b"f\n")])
    upper = build_tar([
        ("dir", "swap/"),
        ("file", "swap/inside", b"i\n"),
    ])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    names = tarfile.open(fileobj=io.BytesIO(merged), mode="r:").getnames()
    assert "swap/.wh..wh..opq" in names


def test_merged_tar_has_no_duplicate_and_is_deterministic():
    lower = build_tar([
        ("dir", "z/"),
        ("file", "z/a", b"1\n"),
        ("wh", "", "keep"),
    ])
    upper = build_tar([
        ("file", "z/a", b"2\n"),
        ("file", "z/b", b"3\n"),
    ])
    first = merge_bytes(lower, upper)
    second = merge_bytes(lower, upper)
    assert first == second  # deterministic serialization
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:") as tf:
        names = tf.getnames()
    assert len(names) == len(set(names))
    # Every directory is emitted before its children.
    for name in names:
        if name.endswith("/"):
            parent = name.rstrip("/")
            for other in names:
                if other != name and other.startswith(parent + "/"):
                    assert names.index(name) < names.index(other)


def test_whiteout_removed_by_upper_does_not_leak_into_output():
    # Lower hides a path, upper re-creates it as a real file: no whiteout
    # should remain for that path in the merged layer.
    lower = build_tar([("wh", "", "keep")])
    upper = build_tar([("file", "keep", b"restored\n")])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    names = tarfile.open(fileobj=io.BytesIO(merged), mode="r:").getnames()
    assert ".wh.keep" not in names
    assert "keep" in names


def test_whiteout_under_opaque_dir_is_not_emitted():
    lower = build_tar([
        ("dir", "shadow/"),
        ("opq", "shadow/"),
        ("file", "shadow/a", b"a\n"),
    ])
    upper = build_tar([("wh", "shadow/", "a"), ("file", "shadow/b", b"b\n")])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    names = tarfile.open(fileobj=io.BytesIO(merged), mode="r:").getnames()
    assert "shadow/.wh.a" not in names
    assert "shadow/.wh..wh..opq" in names


def test_deeply_nested_paths_with_absent_parents():
    # Layers may reference deep paths without emitting every parent; the
    # merger must synthesize parents and still remain equivalent.
    lower = build_tar([("file", "a/b/c/d/deep", b"x\n")])
    upper = build_tar([("file", "a/b/c/d/deep", b"y\n"),
                       ("wh", "a/b/c/", "d")])
    assert_equivalent(lower, upper)


def test_large_file_content_roundtrip():
    payload = bytes((i % 251 for i in range(300_000)))
    lower = build_tar([("file", "big", payload)])
    upper = build_tar([("file", "big", payload + b"-v2")])
    assert_equivalent(lower, upper)


def test_directory_replacing_file_inside_input_layers_is_opaque():
    # When the file -> directory swap is *observable inside the input
    # layers* themselves, the merger must mark the replacement opaque.
    # BASE_B has swap as a file, but the input layers also perform the
    # swap, so the condition is provable from the two input tars alone.
    lower = build_tar([("file", "swap", b"a file\n")])
    upper = build_tar([
        ("dir", "swap/"),
        ("file", "swap/inside", b"i\n"),
    ])
    assert_equivalent(lower, upper)
    merged = merge_bytes(lower, upper)
    names = tarfile.open(fileobj=io.BytesIO(merged), mode="r:").getnames()
    assert "swap/.wh..wh..opq" in names


def test_whiteout_of_path_below_non_directory_rejected():
    # A whiteout whose parent is a regular file cannot be applied.
    lower = build_tar([("file", "f", b"data\n")])
    upper = build_tar([("wh", "f/", "child")])
    with pytest.raises(InvalidLayer):
        merge_bytes(lower, upper)


def test_opaque_marker_on_file_path_rejected():
    lower = build_tar([("file", "f", b"data\n")])
    upper = build_tar([("opq", "f/")])
    with pytest.raises(InvalidLayer):
        merge_bytes(lower, upper)


def test_dot_slash_prefixes_are_normalised():
    # "./" prefixes are valid and equivalent to bare relative names.
    lower = build_tar([("file", "./a/b", b"1\n")])
    upper = build_tar([("dir", "./a/"), ("file", "./a/c", b"2\n")])
    assert_equivalent(lower, upper)


# --------------------------------------------------------------------------- #
# invalid inputs
# --------------------------------------------------------------------------- #
INVALID_CASES = {
    "traversal": build_tar([("file", "../escape", b"x\n")]),
    "absolute": build_tar([("file", "/etc/evil", b"x\n")]),
    "dotdot component": build_tar([("file", "a/../../b", b"x\n")]),
    "backslash": build_tar([("file", "a\\b", b"x\n")]),
    "empty whiteout name": build_tar([("wh", "etc/", "")]),
}


@pytest.mark.parametrize("blob", list(INVALID_CASES.values()), ids=list(INVALID_CASES))
def test_invalid_member_names_rejected(blob):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.tar")
        with open(path, "wb") as fh:
            fh.write(blob)
        with LayerMerger() as merger:
            with pytest.raises(InvalidLayer):
                merger.add_layer(path, origin=1)


def test_symlinks_are_rejected():
    blob = build_tar([("file", "target", b"x\n")])
    # Append a symlink member manually.
    buf = io.BytesIO(blob)
    out = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="r:") as src, tarfile.open(
        fileobj=out, mode="w:", format=tarfile.PAX_FORMAT
    ) as dst:
        for m in src.getmembers():
            dst.addfile(m, src.extractfile(m) if m.isreg() else None)
        link = tarfile.TarInfo("lnk")
        link.type = tarfile.SYMTYPE
        link.linkname = "target"
        dst.addfile(link)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.tar")
        with open(path, "wb") as fh:
            fh.write(out.getvalue())
        with LayerMerger() as merger:
            with pytest.raises(InvalidLayer):
                merger.add_layer(path, origin=1)


def test_compressed_tar_rejected():
    import gzip

    blob = gzip.compress(build_tar([("file", "a", b"1\n")]))
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.tar.gz")
        with open(path, "wb") as fh:
            fh.write(blob)
        with LayerMerger() as merger:
            with pytest.raises(InvalidLayer):
                merger.add_layer(path, origin=1)


def test_truncated_inside_payload_rejected():
    # Cut inside the 2000-byte payload (data spans 512..2512).
    blob = build_tar([("file", "a", b"1\n" * 1000), ("file", "b", b"b" * 100)])
    cut = blob[:1024]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.tar")
        with open(path, "wb") as fh:
            fh.write(cut)
        with LayerMerger() as merger:
            with pytest.raises(InvalidLayer):
                merger.add_layer(path, origin=1)


def test_truncated_header_block_rejected():
    # Cut where the second member's header block should begin.
    blob = build_tar([("file", "a", b"1\n" * 1000), ("file", "b", b"b" * 100)])
    # First member: header 0..512, data blocks 512..2560, then second header.
    cut = blob[: 512 + 2048]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.tar")
        with open(path, "wb") as fh:
            fh.write(cut)
        with LayerMerger() as merger:
            with pytest.raises(InvalidLayer):
                merger.add_layer(path, origin=1)


def test_truncated_missing_end_blocks_rejected():
    # Keep every member intact but strip the mandatory end-of-archive blocks.
    blob = build_tar([("file", "a", b"1\n" * 1000)])
    cut = blob[:2560]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.tar")
        with open(path, "wb") as fh:
            fh.write(cut)
        with LayerMerger() as merger:
            with pytest.raises(InvalidLayer):
                merger.add_layer(path, origin=1)


def test_garbage_input_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.tar")
        with open(path, "wb") as fh:
            fh.write(b"this is not a tar at all" * 50)
        with LayerMerger() as merger:
            with pytest.raises(InvalidLayer):
                merger.add_layer(path, origin=1)


def test_nonempty_whiteout_rejected():
    # A whiteout file that actually carries bytes is invalid per OCI semantics.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        info = tarfile.TarInfo(".wh.x")
        data = b"not empty"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.tar")
        with open(path, "wb") as fh:
            fh.write(buf.getvalue())
        with LayerMerger() as merger:
            with pytest.raises(InvalidLayer):
                merger.add_layer(path, origin=1)


def test_spool_files_are_cleaned_up():
    lower = build_tar([("file", "a", b"1\n")])
    upper = build_tar([("file", "a", b"2\n")])
    with tempfile.TemporaryDirectory() as tmp:
        lo = os.path.join(tmp, "l.tar")
        up = os.path.join(tmp, "u.tar")
        open(lo, "wb").write(lower)
        open(up, "wb").write(upper)
        merger = LayerMerger()
        spool = merger._tmpdir
        assert os.path.isdir(spool)
        merger.add_layer(lo, 1)
        merger.add_layer(up, 2)
        merger.close()
        assert not os.path.exists(spool)
