import json
import sys

from xnotebook.cli import main


def spec(prefix, name):
    return json.loads((prefix / "share" / "jupyter" / "kernels" / name / "kernel.json").read_text())


def test_install(tmp_path, capsys):
    env = tmp_path / "env.yaml"
    env.write_text("dependencies:\n  - numpy\n")
    data = tmp_path / "data"
    data.mkdir()
    args = ["kernel", "install", "--name", "foo", "-e", str(env), "-d", "pandas", "--pip", "six",
            "--mount", f"{data}:/data:rw", "--prefix", str(tmp_path)]
    assert main(args) == 0
    assert "installed kernelspec foo" in capsys.readouterr().out
    target = tmp_path / "share" / "jupyter" / "kernels" / "foo"
    ks = spec(tmp_path, "foo")
    assert ks["argv"][:5] == [sys.executable, "-m", "xnotebook", "kernel", "start"]
    assert ks["argv"][-2:] == ["-f", "{connection_file}"]
    assert ks["display_name"] == "foo (xnb)"
    assert ks["language"] == "python"
    xnb = ks["metadata"]["xnb"]
    assert xnb["kernel"] == "xpython"
    assert xnb["deps"] == ["pandas"] and xnb["pip"] == ["six"]
    assert xnb["mounts"] == [f"{data.resolve()}:/data:rw"]
    assert xnb["envFile"] == "environment.yaml"
    assert (target / "environment.yaml").read_text() == env.read_text()

    # Reinstalling replaces the earlier xnb spec.
    assert main(["kernel", "install", "--name", "foo", "--kernel", "xeus-r", "--prefix", str(tmp_path)]) == 0
    ks = spec(tmp_path, "foo")
    assert ks["language"] == "R" and ks["metadata"]["xnb"]["envFile"] is None
    assert not (target / "environment.yaml").exists()


def test_install_refuses_foreign_spec(tmp_path, capsys):
    other = tmp_path / "share" / "jupyter" / "kernels" / "python3"
    other.mkdir(parents=True)
    (other / "kernel.json").write_text(json.dumps({"argv": ["python"], "display_name": "Python 3", "language": "python"}))
    assert main(["kernel", "install", "--name", "python3", "--prefix", str(tmp_path)]) == 2
    assert "not an xnb kernel" in capsys.readouterr().err
    assert json.loads((other / "kernel.json").read_text())["display_name"] == "Python 3"


def test_install_validates(tmp_path, capsys):
    base = ["kernel", "install", "--prefix", str(tmp_path)]
    assert main([*base, "--name", "bad name"]) == 2
    assert main([*base, "--name", "foo", "--kernel", "nope"]) == 2
    assert main([*base, "--name", "foo", "-e", str(tmp_path / "missing.yaml")]) == 2
    assert main([*base, "--name", "foo", "--mount", "/does/not/exist:/data"]) == 2
    assert main([*base, "--name", "foo", "--sys-prefix"]) == 2
    assert not (tmp_path / "share").exists()
