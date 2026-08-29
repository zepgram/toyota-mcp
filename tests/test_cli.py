import pytest
from loguru import logger

from toyota_mcp import __version__
from toyota_mcp.server import main, silence_upstream_debug_logs


def _run(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> int:
    monkeypatch.setattr("sys.argv", ["toyota-mcp", *arguments])
    with pytest.raises(SystemExit) as excinfo:
        main()
    code = excinfo.value.code
    assert isinstance(code, int)
    return code


def test_help_prints_usage_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, "--help") == 0
    assert "doctor" in capsys.readouterr().out


def test_version(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(monkeypatch, "--version") == 0
    assert capsys.readouterr().out.strip() == __version__


def test_unknown_argument_exits_two_with_usage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, "docter") == 2
    assert "usage" in capsys.readouterr().err


def test_upstream_debug_logs_are_silenced(capsys: pytest.CaptureFixture[str]) -> None:
    silence_upstream_debug_logs()
    try:
        logger.debug('Content: {{"latitude": 42.0}}')
        logger.warning("token refresh retried")
        captured = capsys.readouterr().err
    finally:
        logger.remove()
    assert "latitude" not in captured
    assert "token refresh retried" in captured
