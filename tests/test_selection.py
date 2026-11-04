"""Account selection: who serves next, and when to switch."""

import time

from helpers import account, pool


def test_spends_the_perishable_account_first():
    """Weekly quota is use-it-or-lose-it, so burn the one expiring soonest."""
    far = account("far", util_5h=10, util_7d=20, reset_5h_h=4, reset_7d_h=96)
    soon = account("soon", util_5h=10, util_7d=20, reset_5h_h=2, reset_7d_h=2)
    p = pool(far, soon)
    assert p.pick().alias == "soon"
    assert [a.alias for a in p.order()][0] == "soon"


def test_does_not_switch_on_every_request():
    """Each switch throws away the prompt cache, so stickiness is the point."""
    p = pool(account("a", util_7d=50, reset_7d_h=50),
             account("b", util_7d=50, reset_7d_h=51))
    first = [x.alias for x in p.order()][0]
    assert all([x.alias for x in p.order()][0] == first for _ in range(20))


def test_switches_before_hitting_the_wall():
    """At 80% of the 5-hour window, move rather than spend a 429 finding out."""
    p = pool(account("hot", util_5h=85), account("cool", util_5h=10))
    p.active = "hot"
    assert not p.find("hot").usable
    assert [x.alias for x in p.order()][0] == "cool"
    assert "80%" in p.events[0]["reason"]


def test_cooldown_stops_two_accounts_ping_ponging():
    p = pool(account("a", util_5h=82), account("b", util_5h=80.5))
    p.active = "a"
    p.last_switch = time.time()
    before = len(p.events)
    for _ in range(10):
        p.order()
    assert len(p.events) - before <= 1


def test_exhausted_weekly_skips_the_cooldown():
    p = pool(account("full", util_7d=99), account("free", util_7d=20))
    p.active = "full"
    p.last_switch = time.time()
    assert [x.alias for x in p.order()][0] == "free"
    assert "weekly" in p.events[0]["reason"]


def test_weekly_exhaustion_counts_even_with_a_free_hour():
    """Two independent limits: a free hour does not make a drained week usable."""
    drained = account("drained", util_5h=0, util_7d=100)
    assert drained.out_of_quota
    assert not drained.usable


def test_falls_back_to_whoever_revives_soonest():
    p = pool(account("late", util_5h=100, reset_5h_h=5),
             account("early", util_5h=100, reset_5h_h=1))
    assert p.pick().alias == "early"
