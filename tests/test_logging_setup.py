"""
Tests for `src/logging_setup.py`.

Covers level normalization, idempotency, force-reinstall, and file-sink wiring.
We patch `settings` directly rather than mutating the env (pydantic-settings
caches its values at import time).
"""

from __future__ import annotations

import pytest
from loguru import logger

import src.logging_setup as logging_setup
from src.config import settings


@pytest.fixture(autouse=True)
def _reset_logger_state():
    """Each test starts with a clean loguru + clean module flag."""
    logging_setup.reset_for_testing()
    yield
    logging_setup.reset_for_testing()


# --------------------------------------------------------------------------- #
# Level normalization                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("DEBUG", "DEBUG"),
        ("info", "INFO"),
        (" warning ", "WARNING"),
        ("ERROR", "ERROR"),
        ("trace", "TRACE"),
        ("Critical", "CRITICAL"),
        ("Success", "SUCCESS"),
    ],
)
def test_normalize_level_accepts_valid_levels(raw, expected):
    assert logging_setup._normalize_level(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "VERBOSE", "off", "loud", "12345"])
def test_normalize_level_falls_back_to_info_on_invalid(raw):
    assert logging_setup._normalize_level(raw) == "INFO"


# --------------------------------------------------------------------------- #
# configure_logging — sink installation                                        #
# --------------------------------------------------------------------------- #


def _sink_count() -> int:
    """Loguru exposes the handler dict via a private attribute. Adequate
    for tests; we'd never reach for this in production code."""
    return len(logger._core.handlers)  # type: ignore[attr-defined]


def test_configure_logging_returns_resolved_level(monkeypatch):
    monkeypatch.setattr(settings, "LOG_LEVEL", "WARNING")
    monkeypatch.setattr(settings, "LOG_FILE", "")
    assert logging_setup.configure_logging() == "WARNING"


def test_configure_logging_installs_single_stderr_sink_by_default(monkeypatch):
    monkeypatch.setattr(settings, "LOG_LEVEL", "INFO")
    monkeypatch.setattr(settings, "LOG_FILE", "")
    logging_setup.configure_logging()
    # Just stderr — no file sink.
    assert _sink_count() == 1


def test_configure_logging_is_idempotent(monkeypatch):
    """Two calls without force=True must NOT stack a second sink."""
    monkeypatch.setattr(settings, "LOG_LEVEL", "INFO")
    monkeypatch.setattr(settings, "LOG_FILE", "")
    logging_setup.configure_logging()
    count_after_first = _sink_count()
    logging_setup.configure_logging()
    count_after_second = _sink_count()
    assert count_after_first == count_after_second == 1


def test_configure_logging_force_reinstalls_without_doubling(monkeypatch):
    """force=True must wipe and reinstall — still one sink, not two."""
    monkeypatch.setattr(settings, "LOG_LEVEL", "INFO")
    monkeypatch.setattr(settings, "LOG_FILE", "")
    logging_setup.configure_logging()
    logging_setup.configure_logging(force=True)
    assert _sink_count() == 1


def test_configure_logging_invalid_level_falls_back_to_info(monkeypatch):
    monkeypatch.setattr(settings, "LOG_LEVEL", "BOGUS")
    monkeypatch.setattr(settings, "LOG_FILE", "")
    assert logging_setup.configure_logging() == "INFO"


# --------------------------------------------------------------------------- #
# File sink                                                                    #
# --------------------------------------------------------------------------- #


def test_configure_logging_adds_file_sink_when_log_file_set(monkeypatch, tmp_path):
    log_path = tmp_path / "app.log"
    monkeypatch.setattr(settings, "LOG_LEVEL", "INFO")
    monkeypatch.setattr(settings, "LOG_FILE", str(log_path))
    monkeypatch.setattr(settings, "LOG_FILE_ROTATION", "daily")
    monkeypatch.setattr(settings, "LOG_FILE_RETENTION", "14 days")

    logging_setup.configure_logging()

    # stderr + file = 2 sinks.
    assert _sink_count() == 2

    # Emit a message and confirm it landed in the file.
    logger.info("hello-from-test")
    # loguru in the file sink uses enqueue=True (background thread). Force a
    # flush by removing all sinks (which awaits the queue).
    logging_setup.reset_for_testing()
    assert log_path.exists()
    contents = log_path.read_text()
    assert "hello-from-test" in contents


def test_configure_logging_blank_log_file_treated_as_unset(monkeypatch):
    """A LOG_FILE of '   ' (whitespace) should NOT register a file sink."""
    monkeypatch.setattr(settings, "LOG_LEVEL", "INFO")
    monkeypatch.setattr(settings, "LOG_FILE", "   ")
    logging_setup.configure_logging()
    assert _sink_count() == 1


# --------------------------------------------------------------------------- #
# Filtering by level                                                           #
# --------------------------------------------------------------------------- #


def test_configured_level_filters_lower_messages(monkeypatch, capsys):
    """If level=WARNING, an INFO-level message must NOT reach stderr."""
    monkeypatch.setattr(settings, "LOG_LEVEL", "WARNING")
    monkeypatch.setattr(settings, "LOG_FILE", "")
    logging_setup.configure_logging()

    logger.info("ignored-info")
    logger.warning("kept-warning")

    captured = capsys.readouterr()
    assert "ignored-info" not in captured.err
    assert "kept-warning" in captured.err


def test_reset_for_testing_clears_flag_and_sinks():
    logging_setup.configure_logging()
    assert logging_setup._configured is True
    logging_setup.reset_for_testing()
    assert logging_setup._configured is False
    assert _sink_count() == 0
