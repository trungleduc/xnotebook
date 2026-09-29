import re

from xnotebook.cli import main

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(text: str) -> str:
    """Help output is coloured when the environment forces it (e.g. on CI)."""
    return ANSI.sub("", text)


def test_help(capsys):
    assert main(["--help"]) == 0
    out = plain(capsys.readouterr().out)
    assert "run" in out and "setup" in out and "cache" in out


def test_run_help_is_default_command(capsys):
    assert main(["run", "--help"]) == 0
    out = plain(capsys.readouterr().out)
    for flag in ("--env", "--dep", "--pip", "--mount", "--offline", "--strict", "--cell-timeout"):
        assert flag in out


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "xnb" in capsys.readouterr().out


def test_missing_file(capsys):
    assert main(["does-not-exist.ipynb"]) == 2
    assert "no such file" in capsys.readouterr().err


def test_inplace_requires_notebook(tmp_path, capsys):
    f = tmp_path / "x.py"
    f.write_text("print(1)")
    assert main([str(f), "--inplace"]) == 2


def test_bad_option(capsys):
    assert main(["x.ipynb", "--no-such-flag"]) == 2
    assert "no-such-flag" in plain(capsys.readouterr().err)


def test_cache_info(tmp_path, capsys):
    assert main(["cache", "info", "--cache-dir", str(tmp_path)]) == 0
    assert str(tmp_path) in capsys.readouterr().out
