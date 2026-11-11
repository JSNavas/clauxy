"""The panel adapts to however many accounts are loaded."""

import os
import re
import time

from helpers import account, load, pool

proxy = load()


def _cards(html):
    return len(re.findall(r'<article class="card', html))


def _header_count(html):
    m = re.search(r"Accounts</span>\s*<b>(\d+)</b>\s*<i class=hint>([^<]+)", html)
    return (int(m.group(1)), m.group(2)) if m else (None, None)


def test_renders_any_number_of_accounts():
    for n in (1, 2, 3, 5, 8):
        html = proxy.render_panel(
            pool(*[account(f"a{i}") for i in range(n)]).snapshot())
        assert _cards(html) == n
        assert _header_count(html)[0] == n


def test_header_summarises_drained_accounts():
    html = proxy.render_panel(pool(
        account("ok1"), account("ok2"),
        account("gone1", util_7d=100), account("gone2", util_7d=100)).snapshot())
    _, summary = _header_count(html)
    assert "2 with quota" in summary and "2 drained" in summary


def test_names_the_account_that_will_serve_before_any_traffic():
    html = proxy.render_panel(pool(account("first"), account("second")).snapshot())
    assert "no requests yet" in html
    assert "IN USE" in html


def test_a_drained_week_is_called_out():
    html = proxy.render_panel(pool(account("a", util_7d=100)).snapshot())
    assert "WEEKLY LIMIT REACHED" in html


def test_an_unused_window_says_so_instead_of_dashes():
    """No usage means no reset time; 'resets in --' looks like missing data."""
    assert "unused" in proxy.reset_label({"pct": 0, "in_s": None})
    assert proxy.reset_label({"pct": 40, "in_s": None}) == ""


def test_never_claims_quota_it_does_not_know_about():
    """An account with no telemetry is not an account with quota.

    Saying 'Quota available' for one we have never heard from is how work
    gets sent to a drained subscription.
    """
    blank = account("blank")
    blank.quota = {}
    html = proxy.render_panel(pool(blank).snapshot())
    assert "Quota available" not in html
    assert "Quota not known yet" in html
    assert "QUOTA UNKNOWN" in html


def test_header_counts_unknown_apart_from_with_quota():
    good = account("good", util_5h=10, util_7d=10)
    blank = account("blank")
    blank.quota = {}
    html = proxy.render_panel(pool(good, blank).snapshot())
    _, summary = _header_count(html)
    assert "1 with quota" in summary and "1 unknown" in summary


def test_the_buttons_ask_for_something_the_pool_understands():
    """Every button was dead: the form posted English, the handler read Spanish.

    Pinning the rendered values against the accepted ones is what stops the
    two sides drifting apart again without a word.
    """
    p = pool(account("a"), account("b"))
    html = proxy.render_panel(p.snapshot())
    asked = set(re.findall(r'name="action" value="(\w+)"', html))
    assert asked, "no buttons rendered"
    assert asked <= set(proxy.Pool.CONTROLS), asked - set(proxy.Pool.CONTROLS)


def test_pausing_from_the_panel_pauses():
    p = pool(account("a"), account("b"))
    assert p.apply_control("a", "pause")
    assert p.find("a").paused
    assert p.apply_control("a", "resume")
    assert not p.find("a").paused


def test_an_unknown_button_changes_nothing():
    p = pool(account("a"))
    assert not p.apply_control("a", "rm -rf")
    assert not p.apply_control("nobody", "pause")
    assert not p.find("a").paused


def test_the_panel_refreshes_itself_without_a_reload():
    """A 10 s meta-refresh could show IN USE on an account already left."""
    html = proxy.render_panel(pool(account("a")).snapshot())
    assert "http-equiv=refresh" not in html
    assert 'id=live' in html and "setInterval(tick, 1000)" in html


def test_spend_reads_like_money():
    """"$5.3123" is not a dollar amount anyone writes."""
    assert proxy.money(5.3123) == "$5.31"
    assert proxy.money(1234.5) == "$1,234.50"
    assert proxy.money(0) == "$0.00"
    assert proxy.money(None) == "—"
    # A single request costs fractions of a cent: rounding those to two
    # decimals would turn the whole log into a column of "$0.02".
    assert proxy.money(0.01828) == "$0.018"
    assert proxy.money(0.00031) == "$0.00031"


def test_switch_history_survives_a_restart(tmp=None):
    """The table used to show a lone "startup" row after every restart."""
    import json
    import tempfile
    p = pool(account("a"), account("b"))
    p.record_switch(p.find("a"), p.find("b"), "5h >= 80%")
    with tempfile.TemporaryDirectory() as d:
        old = proxy.STATE_FILE
        proxy.STATE_FILE = os.path.join(d, "state.json")
        try:
            proxy.save_state(p)
            assert json.load(open(proxy.STATE_FILE))["events"]
            fresh = pool(account("a"), account("b"))
            proxy.load_state(fresh)
        finally:
            proxy.STATE_FILE = old
    assert [e["reason"] for e in fresh.events] == ["5h >= 80%"]
    assert fresh.events[0]["from"] == "a" and fresh.events[0]["to"] == "b"


def test_an_old_switch_is_dated_not_just_clocked():
    now = proxy.switch_when({"ts": time.time(), "time": "x"})
    then = proxy.switch_when({"ts": time.time() - 3 * 86400, "time": "x"})
    assert re.fullmatch(r"\d\d:\d\d:\d\d", now), now
    assert re.match(r"[A-Z][a-z]{2} \d+ \d\d:\d\d:\d\d", then), then
    assert proxy.switch_when({"time": "16:14:50"}) == "16:14:50"


def test_a_restart_row_does_not_read_as_a_broken_switch():
    """An empty FROM looked like missing data. Nothing was left: it booted."""
    p = pool(account("a"), account("b"))
    p.order()
    html = proxy.render_panel(p.snapshot())
    assert "No switches yet" in html
    assert "proxy started" in html
    assert "<td>—</td>" not in html


def test_a_real_switch_still_names_both_sides():
    p = pool(account("hot", util_5h=85), account("cool", util_5h=10))
    p.active = "hot"
    p.order()
    html = proxy.render_panel(p.snapshot())
    assert "No switches yet" not in html
    assert "<td>hot</td>" in html and "<b>cool</b>" in html


def _busy_pool(switches=14, requests=27):
    p = pool(account("a"), account("b"))
    for i in range(switches):
        p.record_switch(p.find("a"), p.find("b"), f"r{i}")
    row = {"time": "10:00:00", "model": "m", "account": "a", "status": 200,
           "switched": False, "own": True, "ms": 10,
           "tokens": {"input": 1, "output": 1, "cache_write": 0, "cache_read": 0},
           "usd": 0.001}
    for _ in range(requests):
        p.audit.appendleft(dict(row))
    return p


def _slices(html):
    return re.findall(r"<span>([\d\-]+ of \d+)</span>", html)


def test_only_the_page_you_asked_for_is_built():
    """The page used to carry every row and hide most with CSS, once a second."""
    snap = _busy_pool().snapshot()
    assert _slices(proxy.render_panel(snap)) == ["1-5 of 15", "1-10 of 27"]
    assert _slices(proxy.render_panel(snap, sw_page=1, log_page=2)) \
        == ["6-10 of 15", "21-27 of 27"]


def test_a_page_past_the_end_is_clamped_not_blank():
    snap = _busy_pool().snapshot()
    html = proxy.render_panel(snap, sw_page=99, log_page=99)
    assert _slices(html) == ["11-15 of 15", "21-27 of 27"]
    assert 'data-sw="2" data-log="2"' in html, "the page must say where it landed"


def test_no_pager_when_everything_fits():
    p = pool(account("a"))
    p.record_switch(p.find("a"), p.find("a"), "once")
    assert "class=pager" not in proxy.render_panel(p.snapshot())


def test_the_pager_buttons_name_the_page_they_load():
    html = proxy.render_panel(_busy_pool().snapshot())
    assert 'data-page="sw:1"' in html and 'data-page="log:1"' in html
    # ‹ newer on page 0 has nowhere to go
    assert 'data-page="sw:-1" disabled' in html


def test_a_full_hour_says_when_it_comes_back():
    """DRAINED and "5-hour window at 100%" on the same card answered the
    wrong question. Telemetry reaches 100% without any 429, so there is no
    `waiting_s` to trigger the usual banner — the window's own reset is the
    answer, and the card should lead with it."""
    html = proxy.render_panel(pool(account("spent", util_5h=100)).snapshot())
    assert "DRAINED" in html
    assert "Back in" in html, "a spent hour must say when it returns"
    assert "5-hour window at <b>100%" not in html


def test_a_half_spent_hour_still_reads_as_a_status():
    html = proxy.render_panel(pool(account("busy", util_5h=50)).snapshot())
    assert "5-hour window at <b>50%" in html
    assert "Back in" not in html


def _banner(html, alias):
    import re
    card = [c for c in re.findall(r'<article class="card.*?</article>', html, re.S)
            if f"\u00b7 {alias}</h2>" in c][0]
    m = re.search(r'<div class="reset[^"]*">(.*?)</div>', card, re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())


def test_the_same_state_reads_the_same_whichever_way_we_found_out():
    """One account stopped by a 429, one caught at 100% by telemetry.

    They are held for the same reason and must say the same thing. The banner
    used to be written twice — Anthropic's sentence on one branch, ours on the
    other — so the two sat side by side on the panel wording it differently.
    """
    by_429 = account("told", util_5h=100)
    by_429.cooling_until = time.time() + 1500
    by_429.limit_text = ('{"error":{"message":"This request would exceed your '
                         'account\'s rate limit. Please try again later."}}')
    by_telemetry = account("seen", util_5h=100)

    html = proxy.render_panel(pool(by_429, by_telemetry).snapshot())
    assert "5-hour window spent" in _banner(html, "told")
    assert "5-hour window spent" in _banner(html, "seen")
    assert "rate limit" not in _banner(html, "told"), "generic 429 adds nothing"


def test_a_429_that_names_the_reset_is_kept():
    """Only the sentences that say more than the line above them survive."""
    a = account("dated", util_5h=100)
    a.cooling_until = time.time() + 1500
    a.limit_text = ('{"error":{"message":"This request would exceed your '
                    'weekly limit, resets Nov 15 at 1am"}}')
    assert "resets Nov 15 at 1am" in _banner(
        proxy.render_panel(pool(a).snapshot()), "dated")
