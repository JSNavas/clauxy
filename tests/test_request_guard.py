"""Avoid repeating requests against rejected or unavailable accounts."""

import io
from unittest.mock import patch
from helpers import account, pool, load

proxy = load()


def test_auth_failure_blocks_subsequent_attempts_until_resume():
    a = account("a")
    handler = object.__new__(proxy.Handler)
    handler.path = "/v1/messages"
    handler.headers = {"Content-Length": "0"}
    handler.rfile = io.BytesIO()
    handler.pool = pool(a)
    with patch.object(proxy, "require_network"), \
         patch.object(handler, "_attempt", return_value=(401, {}, io.BytesIO(), None)) as attempt, \
         patch.object(handler, "_relay"), patch.object(handler, "send_error") as error:
        handler._handle("POST")
        handler._handle("POST")
        assert attempt.call_count == 1
        assert error.call_args.args[0] == 503
    assert handler.pool.apply_control("a", "resume")
    assert a.available


def test_unavailable_accounts_are_not_selected_in_either_mode():
    a, b = account("a"), account("b")
    a.paused = True
    b.cool_down(300)
    for mode in ("sequential", "sticky"):
        assert pool(a, b, mode=mode).order() == []


def test_rate_limit_blocks_further_attempts():
    a = account("a")
    handler = object.__new__(proxy.Handler)
    handler.path = "/v1/messages"
    handler.headers = {"Content-Length": "0"}
    handler.rfile = io.BytesIO()
    handler.pool = pool(a)
    with patch.object(proxy, "require_network"), \
         patch.object(handler, "_attempt", return_value=(429, {"retry-after": "60"}, io.BytesIO(), None)) as attempt, \
         patch.object(handler, "_relay"), patch.object(handler, "send_error"):
        handler._handle("POST")
        handler._handle("POST")
        assert attempt.call_count == 1
        assert a.cooling_for > 0
