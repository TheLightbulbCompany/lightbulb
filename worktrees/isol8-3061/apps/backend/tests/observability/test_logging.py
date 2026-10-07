"""Tests for core/observability/logging.py — JSON formatter + contextvars."""

import json
import logging

from core.observability.logging import (
    JsonFormatter,
    RedactTokenFilter,
    bind_request_context,
    configure_logging,
    request_id_var,
    user_id_var,
)


def _make_record(msg: str = "test", level: int = logging.INFO, **kwargs) -> logging.LogRecord:
    record = logging.LogRecord("test.logger", level, "", 0, msg, (), None)
    for k, v in kwargs.items():
        setattr(record, k, v)
    return record


def test_json_formatter_outputs_single_line():
    """Output should be parseable JSON with standard fields."""
    formatter = JsonFormatter()
    data = json.loads(formatter.format(_make_record("hello world")))
    assert data["message"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "test.logger"
    assert "timestamp" in data


def test_json_formatter_includes_contextvars():
    """request_id and user_id from contextvars should appear in output."""
    formatter = JsonFormatter()
    tok_r = request_id_var.set("req-123")
    tok_u = user_id_var.set("user-456")
    try:
        data = json.loads(formatter.format(_make_record()))
        assert data["request_id"] == "req-123"
        assert data["user_id"] == "user-456"
    finally:
        request_id_var.reset(tok_r)
        user_id_var.reset(tok_u)


def test_json_formatter_includes_extra_fields():
    """Extra attributes on the LogRecord should appear in output."""
    formatter = JsonFormatter()
    data = json.loads(formatter.format(_make_record(action="fleet_patch", actor_id="u1")))
    assert data["action"] == "fleet_patch"
    assert data["actor_id"] == "u1"


def test_json_formatter_handles_exceptions():
    """Exception info should be included when present."""
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _make_record("fail", logging.ERROR)
        record.exc_info = sys.exc_info()

    data = json.loads(formatter.format(record))
    assert "exception" in data
    assert "ValueError: boom" in data["exception"]


def test_json_formatter_null_contextvars():
    """When no contextvars are set, fields should be null, not missing."""
    formatter = JsonFormatter()
    tok_r = request_id_var.set(None)
    tok_u = user_id_var.set(None)
    try:
        data = json.loads(formatter.format(_make_record()))
        assert data["request_id"] is None
        assert data["user_id"] is None
    finally:
        request_id_var.reset(tok_r)
        user_id_var.reset(tok_u)


def test_bind_request_context():
    """bind_request_context should set both contextvars."""
    bind_request_context("req-abc", "user-xyz")
    assert request_id_var.get() == "req-abc"
    assert user_id_var.get() == "user-xyz"
    # cleanup
    request_id_var.set(None)
    user_id_var.set(None)


def test_bind_request_context_user_optional():
    """Calling without user_id should only set request_id."""
    user_id_var.set(None)
    bind_request_context("req-only")
    assert request_id_var.get() == "req-only"
    assert user_id_var.get() is None
    request_id_var.set(None)


def test_redact_token_filter_scrubs_message():
    """The relay's ?token=<jwt> query string, wherever it lands in the raw
    message, must never reach the log output."""
    record = _make_record("GET /api/v1/gateway/ws?token=abc.def.ghi&clientId=paperclip-operator 403")
    filtered = RedactTokenFilter().filter(record)
    assert filtered is True
    assert "token=REDACTED" in record.msg
    assert "abc.def.ghi" not in record.msg


def test_redact_token_filter_scrubs_args():
    """Uvicorn's WS handshake log formats as `logger.info('%s - "%s" %d', addr,
    request_line, status)` -- the token-bearing request line is in args, not
    the format string itself."""
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        "",
        0,
        '%s - "%s" %d',
        ("127.0.0.1:1234", "GET /api/v1/gateway/ws?token=SECRET-JWT&clientId=node-host HTTP/1.1", 101),
        None,
    )
    RedactTokenFilter().filter(record)
    assert "token=REDACTED" in record.args[1]
    assert "SECRET-JWT" not in record.args[1]
    assert record.args[0] == "127.0.0.1:1234" and record.args[2] == 101


def test_redact_token_filter_is_a_noop_without_a_token():
    record = _make_record("plain message, nothing to redact")
    RedactTokenFilter().filter(record)
    assert record.msg == "plain message, nothing to redact"


def test_configure_logging_installs_the_uvicorn_token_redaction_filter():
    configure_logging()
    assert any(isinstance(f, RedactTokenFilter) for f in logging.getLogger("uvicorn.error").filters)


def test_configure_logging_is_idempotent_about_the_redaction_filter():
    """Repeat calls (tests, a reload) must not stack a duplicate filter --
    logging.Filter has no add-time identity check, so a naive addFilter would
    keep appending one every call."""
    configure_logging()
    configure_logging()
    configure_logging()
    filters = [f for f in logging.getLogger("uvicorn.error").filters if isinstance(f, RedactTokenFilter)]
    assert len(filters) == 1


def test_configure_logging_quiets_httpx_request_urls():
    """httpx's INFO line is the full request URL, query string and all.

    Provider endpoints that only accept credentials as query params (Meta's token
    exchange) therefore wrote live secrets into CloudWatch. WARNING is the
    single-point fix for every httpx call site, so it must survive refactors.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    # configure_logging mutates three logger LEVELS as well as the root handlers
    # ("" is the root logger), and levels are process-global — leaving them set
    # would silently change what later tests in this session can capture.
    saved_levels = {name: logging.getLogger(name).level for name in ("", "httpx", "httpcore")}
    try:
        configure_logging()
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        root.handlers[:] = saved_handlers
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
