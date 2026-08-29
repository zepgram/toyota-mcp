import pytest
from loguru import logger

from toyota_mcp import __version__
from toyota_mcp.places import Places
from toyota_mcp.server import build_parser, main, silence_upstream_debug_logs


def _run(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> int:
    monkeypatch.setattr("sys.argv", ["toyota-mcp", *arguments])
    with pytest.raises(SystemExit) as excinfo:
        main()
    code = excinfo.value.code
    assert isinstance(code, int)
    return code


def test_help_lists_subcommands_and_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, "--help") == 0
    out = capsys.readouterr().out
    assert "doctor" in out and "probe" in out
    assert "--read-only" in out and "--addresses" in out and "--places" in out


def test_version(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(monkeypatch, "--version") == 0
    assert capsys.readouterr().out.strip() == __version__


def test_unknown_argument_exits_two_with_usage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, "docter") == 2
    assert "usage" in capsys.readouterr().err


def test_options_parse_with_commands_on_by_default() -> None:
    args = build_parser().parse_args([])
    assert args.read_only is False
    assert args.addresses == "off"
    assert not args.places
    args = build_parser().parse_args(
        ["--read-only", "--addresses", "fr", "--places", "home=43.6045,1.4440"]
    )
    assert args.read_only is True
    assert args.addresses == "fr"
    assert isinstance(args.places, Places) and args.places.match(43.6045, 1.4440) == "home"


def test_invalid_places_option_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--places", "home=oops"])
    assert excinfo.value.code == 2
    assert "name=lat,lon" in capsys.readouterr().err


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
