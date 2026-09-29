import base64
import os

import pytest

from xnb.mounts import MountError, mount_to_job, parse_mount, write_back


def b64(s: bytes) -> str:
    return base64.b64encode(s).decode()


def test_parse_mount(tmp_path):
    m = parse_mount(f"{tmp_path}:/data")
    assert m.dst == "/data" and m.mode == "ro" and m.src == tmp_path.resolve()
    assert parse_mount(f"{tmp_path}:/data/x/../y:rw").dst == "/data/y"
    for bad in (f"{tmp_path}:relative", f"{tmp_path}:/", f"{tmp_path}:/lib/x", "nosuchdir:/data", "nocolon"):
        with pytest.raises(MountError):
            parse_mount(bad)


def test_mount_to_job_skips_symlinks(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("B")
    os.symlink("/etc/passwd", tmp_path / "link")
    job = mount_to_job(parse_mount(f"{tmp_path}:/data"))
    paths = sorted(f["path"] for f in job["files"])
    assert paths == ["/data/a.txt", "/data/sub/b.txt"]
    assert "/data/sub" in job["dirs"]


def test_write_back_confined(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    m = parse_mount(f"{root}:/out:rw")
    written = write_back(
        m,
        [
            {"path": "/out/ok.txt", "data": b64(b"ok")},
            {"path": "/out/deep/x.txt", "data": b64(b"deep")},
            {"path": "/out/../outside.txt", "data": b64(b"escape")},
            {"path": "/elsewhere/x.txt", "data": b64(b"nope")},
            {"path": "/out", "data": b64(b"dir itself")},
        ],
    )
    assert (root / "ok.txt").read_text() == "ok"
    assert (root / "deep" / "x.txt").read_text() == "deep"
    assert not outside.exists()
    assert len(written) == 2


def test_write_back_refuses_symlinks(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    target_dir = tmp_path / "victim"
    target_dir.mkdir()
    os.symlink(target_dir, root / "link")
    os.symlink(tmp_path / "victim.txt", root / "file_link")
    m = parse_mount(f"{root}:/out:rw")
    with pytest.raises(MountError):
        write_back(m, [{"path": "/out/link/pwned.txt", "data": b64(b"x")}])
    assert not (target_dir / "pwned.txt").exists()
    with pytest.raises(MountError):
        write_back(m, [{"path": "/out/file_link", "data": b64(b"x")}])
    assert not (tmp_path / "victim.txt").exists()


def test_ro_mount_never_written(tmp_path):
    (tmp_path / "a.txt").write_text("orig")
    m = parse_mount(f"{tmp_path}:/data")
    job = mount_to_job(m)
    assert job["mode"] == "ro"
