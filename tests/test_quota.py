"""Reading quota: response headers, limit messages, and what is not quota."""

import os
import time
from datetime import datetime, timedelta

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


def _limit_message(days_ahead):
    """A limit message naming a date relative to today.

    Hard-coding "Nov 5" made this test pass or fail depending on the day it
    was run, which is how a real bug in the year rollover hid behind it.
    """
    when = datetime.now().astimezone() + timedelta(days=days_ahead)
    return (f"You've hit your weekly limit - resets "
            f"{when.strftime('%b')} {when.day} at 12pm")


def test_reads_the_reset_time_out_of_a_limit_message():
    """Without telemetry this is the only source for when quota comes back."""
    secs = proxy.seconds_until_reset(_limit_message(2))
    assert secs is not None and 0 < secs < 15 * 24 * 3600


def test_a_date_that_just_passed_is_not_a_year_away():
    """It used to roll a whole year forward, parking the account for days.

    The cap is 8 days, so a message naming yesterday took the account out of
    rotation for the better part of a week.
    """
    assert proxy.seconds_until_reset(_limit_message(-1)) == 0


def test_a_date_a_few_days_out_lands_a_few_days_out():
    """Run near New Year this is also the rollover case: on December 30 the
    message names a January date, and the answer must still be three days."""
    secs = proxy.seconds_until_reset(_limit_message(3))
    assert 2 * 24 * 3600 < secs < 4 * 24 * 3600


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


def test_quota_survives_a_restart(tmp=None):
    """Counters used to persist but quota did not, so the bars went blank
    after every restart until that account served again."""
    import json
    import os
    import tempfile

    from helpers import account, pool

    a = account("a", util_5h=42, util_7d=17)
    p = pool(a)
    a.requests = 7

    with tempfile.TemporaryDirectory() as d:
        proxy.STATE_FILE = os.path.join(d, "state.json")
        proxy.save_state(p)
        assert json.load(open(proxy.STATE_FILE))["accounts"]["a"]["quota"]

        fresh = account("a")          # a brand new process
        fresh.quota = {}
        p2 = pool(fresh)
        assert proxy.load_state(p2)
        assert fresh.requests == 7
        assert fresh.window("five_hour")[0] == 42
        assert fresh.window("seven_day")[0] == 17


def test_a_throttled_usage_endpoint_is_not_the_accounts_quota():
    """"HTTP 429" on screen read like the account was drained. It is not:

    /api/oauth/usage rate-limits on its own, and the bars already on screen
    stay valid — they just stop being refreshed for a while.
    """
    a = account("x")
    a.probe_blocked_until = time.time() + 90
    before = dict(a.quota)
    a.probe_quota()
    assert "too many quota checks" in a.quota_error
    assert "429" not in a.quota_error
    assert a.quota == before, "a throttled probe must not wipe what we knew"
    assert a.available, "the endpoint being busy says nothing about quota"


def test_the_backoff_clears_once_it_can_read_again():
    a = account("x")
    a.probe_blocked_until = time.time() - 1
    assert a.probe_wait == 0


def test_a_restart_does_not_re_ask_for_a_fresh_reading():
    """Restarting threw away a reading taken seconds earlier and asked again.

    A handful of restarts in a row was enough to earn a 429 from an endpoint
    that rate-limits separately from the quota it reports.
    """
    a = account("x")
    a.quota_at = time.time() - 5
    assert time.time() - a.quota_at < proxy.PROBE_EVERY, "still fresh: do not ask"
    a.quota_at = time.time() - 3 * proxy.PROBE_EVERY
    assert time.time() - a.quota_at > proxy.PROBE_EVERY, "stale: ask"


def test_the_backoff_outlives_a_restart():
    import json
    import tempfile
    from helpers import pool
    a = account("x")
    a.probe_blocked_until = time.time() + 300
    a.quota_at = time.time()
    p = pool(a)
    with tempfile.TemporaryDirectory() as d:
        old = proxy.STATE_FILE
        proxy.STATE_FILE = os.path.join(d, "state.json")
        try:
            proxy.save_state(p)
            saved = json.load(open(proxy.STATE_FILE))["accounts"]["x"]
            assert saved["probe_blocked_until"] > time.time()
            fresh_pool = pool(account("x"))
            proxy.load_state(fresh_pool)
        finally:
            proxy.STATE_FILE = old
    assert fresh_pool.find("x").probe_wait > 0, "a restart must not skip the wait"


def test_the_profile_is_not_fetched_on_every_probe():
    """Asking for an identity that never changes doubled the traffic."""
    a = account("x")
    a.email = "someone@example.com"
    a.profile_at = time.time()
    assert proxy.PROFILE_EVERY > proxy.PROBE_EVERY * 60


def test_a_warning_status_is_not_a_lock():
    """`allowed_warning` means "nearly full", not "blocked".

    Reading it as a lock knocked an account that could still serve out of the
    rotation, and the proxy spent its turns on accounts that really were dry.
    """
    q = proxy.quota_from_headers(headers(util_7d="0.93", status_7d="allowed_warning"))
    assert q["seven_day"]["locked_reason"] is None

    a = account("warned", util_7d=93)
    a.quota["seven_day"]["locked_reason"] = None
    assert a.available and a.usable


def test_a_refusing_status_is_still_a_lock():
    q = proxy.quota_from_headers(headers(status_7d="rejected"))
    assert q["seven_day"]["locked_reason"] == "rejected"


def test_the_limit_message_is_read_out_of_the_json_body():
    """The panel shows this line: it should be the sentence, not the envelope."""
    body = ('{"type":"error","error":{"type":"rate_limit_error","message":'
            '"This request would exceed your weekly limit, resets Nov 15 at 1am"}}')
    assert proxy.limit_message(body).startswith("This request would exceed")
    assert "rate_limit_error" not in proxy.limit_message(body)
    # Plain text, or a body that is not the expected shape, passes through.
    assert proxy.limit_message("usage limit reached") == "usage limit reached"
    assert proxy.limit_message("") == ""
    assert proxy.is_quota_429(body)


def test_a_restart_drops_a_stale_lock_verdict(tmpdir=None):
    """The saved `quota` carries an OLD build's verdict; `limits` is raw.

    A version that mistook `allowed_warning` for a lock wrote
    `locked_reason: "allowed_warning"` into the state file. Loading that back
    verbatim kept the account out of the rotation after every restart, long
    after the rule was fixed, because nothing re-reads it until fresh headers
    arrive — and a drained account never gets any.
    """
    import json
    import tempfile

    pool = proxy.Pool([account("c2")])
    raw = headers(util_5h="0.33", util_7d="0.95", status_7d="allowed_warning")
    poisoned = proxy.quota_from_headers(raw)
    poisoned["seven_day"]["locked_reason"] = "allowed_warning"
    # A real lock must survive the load; only the `allowed*` family is cleared.
    poisoned["five_hour"]["locked_reason"] = None

    state = {"accounts": {"c2": {"limits": raw, "quota": poisoned}}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(state, fh)
        path = fh.name

    old, proxy.STATE_FILE = proxy.STATE_FILE, path
    try:
        proxy.load_state(pool)
    finally:
        proxy.STATE_FILE = old

    c2 = pool.find("c2")
    assert c2.quota["seven_day"]["locked_reason"] is None, "the old verdict survived"
    assert not c2.out_of_quota, "95% weekly is not drained"
    assert c2.available and c2.usable
    # The measurements are left alone: a probe may have refreshed them past
    # whatever the stored headers say, so re-deriving them would walk back a
    # fresher reading.
    assert c2.quota["five_hour"]["utilization"] == 33.0
    assert c2.quota["seven_day"]["utilization"] == 95.0


def test_a_real_lock_survives_the_restart():
    """Clearing the stale verdict must not clear a genuine one."""
    import json
    import tempfile

    pool = proxy.Pool([account("c1")])
    raw = headers(util_5h="1.0", status_5h="rejected")
    state = {"accounts": {"c1": {"limits": raw,
                                 "quota": proxy.quota_from_headers(raw)}}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(state, fh)
        path = fh.name

    old, proxy.STATE_FILE = proxy.STATE_FILE, path
    try:
        proxy.load_state(pool)
    finally:
        proxy.STATE_FILE = old

    assert pool.find("c1").quota["five_hour"]["locked_reason"] == "rejected"
    assert pool.find("c1").out_of_quota


def test_a_window_with_no_reading_does_not_kill_the_startup():
    """A window never read is stored as null, not as an empty block.

    Walking the quota to clear a stale verdict assumed every value was a
    dict, so `load_state` raised on boot and the proxy stayed stopped — the
    worst possible failure for a file read before anything can serve.
    """
    import json
    import tempfile

    pool = proxy.Pool([account("c1")])
    state = {"accounts": {"c1": {"quota": {"five_hour": None,
                                           "seven_day": {"utilization": 20.0}}}}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(state, fh)
        path = fh.name

    old, proxy.STATE_FILE = proxy.STATE_FILE, path
    try:
        assert proxy.load_state(pool) is True
    finally:
        proxy.STATE_FILE = old
    assert pool.find("c1").quota["seven_day"]["utilization"] == 20.0
