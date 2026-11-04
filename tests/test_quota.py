"""Reading quota: response headers, limit messages, and what is not quota."""

import time

from helpers import account, headers, load

proxy = load()


def test_reads_quota_from_response_headers():
    """Anthropic sends utilisation on every response; fractions, epoch resets."""
    q = proxy.quota_from_headers(headers(util_5h="0.0", util_7d="0.28"))
    assert q["five_hour"]["utilization"] == 0.0
    assert q["seven_day"]["utilization"] == 28.0      # not 28.000000000000004


def test_rejected_status_marks_the_account_drained():
    a = account("x")
    a.quota = proxy.quota_from_headers(
        headers(util_7d="1.0", status_7d="rejected"))
    assert a.out_of_quota


def test_malformed_headers_do_not_raise():
    assert proxy.quota_from_headers({}) == {}
    assert proxy.quota_from_headers({"unified-5h-utilization": "xx"}) == {}


def test_reads_the_reset_time_out_of_a_limit_message():
    """Without telemetry this is the only source for when quota comes back."""
    secs = proxy.seconds_until_reset(
        "You've hit your weekly limit - resets Nov 5 at 12pm")
    assert secs is not None and 0 < secs < 15 * 24 * 3600


def test_ignores_text_with_no_reset_in_it():
    assert proxy.seconds_until_reset('{"type":"error"}') is None
    assert proxy.seconds_until_reset("") is None


def test_generic_429_is_not_spent_quota():
    """The API 429s any client it does not recognise, with an empty message.

    Treating that as exhaustion would knock out every account at once - a
    single health check would drain the whole pool.
    """
    assert not proxy.is_quota_429(
        '{"type":"error","error":{"type":"rate_limit_error","message":"Error"}}')


def test_real_quota_429_carries_a_reason():
    assert proxy.is_quota_429("You've hit your weekly limit - resets Nov 5 at 12pm")
    assert proxy.is_quota_429('{"error":{"message":"You exceeded your usage limit"}}')
    # The error *type* contains "limit", so only the message may be inspected.
    assert proxy.is_quota_429(
        '{"error":{"type":"rate_limit_error","message":"You hit your 5-hour limit"}}')
