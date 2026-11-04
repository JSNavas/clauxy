"""Shared fixtures. Keeps the tests readable and free of plumbing."""

import importlib.util
import os
import sys
import time
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    """Imports clauxy_proxy.py, which has no .py-importable name of its own."""
    if "clauxy_proxy" in sys.modules:
        return sys.modules["clauxy_proxy"]
    path = os.path.join(ROOT, "clauxy_proxy.py")
    spec = importlib.util.spec_from_loader(
        "clauxy_proxy", SourceFileLoader("clauxy_proxy", path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clauxy_proxy"] = mod
    spec.loader.exec_module(mod)
    return mod


proxy = load()


def _iso(seconds_ahead):
    return datetime.fromtimestamp(
        time.time() + seconds_ahead, timezone.utc).isoformat()


def account(alias, util_5h=0, util_7d=0, reset_5h_h=3, reset_7d_h=50):
    """An account with known quota, so selection can be reasoned about."""
    a = proxy.Account(alias, "sk-ant-oat01-" + "x" * 40)
    a.quota = {
        "five_hour": {"utilization": util_5h,
                      "resets_at": _iso(reset_5h_h * 3600),
                      "locked_reason": None},
        "seven_day": {"utilization": util_7d,
                      "resets_at": _iso(reset_7d_h * 3600),
                      "locked_reason": None},
    }
    return a


def pool(*accounts, mode="sequential"):
    p = proxy.Pool(list(accounts))
    p.mode = mode
    return p


def headers(util_5h="0.0", util_7d="0.0", status_5h="allowed", status_7d="allowed"):
    now = int(time.time())
    return {
        "unified-5h-utilization": util_5h,
        "unified-7d-utilization": util_7d,
        "unified-5h-reset": str(now + 3600),
        "unified-7d-reset": str(now + 86400),
        "unified-5h-status": status_5h,
        "unified-7d-status": status_7d,
    }
