"""Manual renewal dates never pretend to be confirmed subscription expiry."""

import json
import os
import tempfile
from datetime import date
from unittest import TestCase
from unittest.mock import patch

from helpers import account, load
import clauxy_accounts as metadata

proxy = load()


def test_renewal_dates_require_real_iso_calendar_dates():
    assert metadata.parse_renewal("2028-02-29") == date(2028, 2, 29)
    for value in ("2026-02-29", "2026-04-31", "2026-13-01", "20/11/2026", "20261120",
                  "2026-11-20T12:00:00", "", None):
        with TestCase().assertRaises(ValueError):
            metadata.parse_renewal(value)


def test_dates_are_persistent_per_account_and_can_be_cleared():
    with tempfile.TemporaryDirectory() as folder, \
            patch.object(metadata, "RENEWALS_FILE", os.path.join(folder, "subscriptions.json")):
        metadata.set_renewal("c2", "2026-12-18")
        metadata.set_renewal("c3", "2026-12-07")
        assert metadata.subscription_snapshot("c2", date(2026, 11, 20)) == {
            "renewal_date": "2026-12-18", "days_remaining": 28, "source": "manual"}
        assert metadata.subscription_snapshot("c3", date(2026, 11, 20))["days_remaining"] == 17
        metadata.set_renewal("c2", None)
        assert metadata.subscription_snapshot("c2")["renewal_date"] is None
        assert metadata.subscription_snapshot("c3")["renewal_date"] == "2026-12-07"
        assert os.stat(metadata.RENEWALS_FILE).st_mode & 0o777 == 0o600
        assert os.listdir(folder) == ["subscriptions.json"]


def test_updating_metadata_reaches_existing_account_snapshot_without_restart():
    with tempfile.TemporaryDirectory() as folder, \
            patch.object(metadata, "RENEWALS_FILE", os.path.join(folder, "subscriptions.json")):
        a = account("c2")
        assert a.snapshot()["subscription"]["renewal_date"] is None
        metadata.set_renewal("c2", "2026-12-18")
        assert a.snapshot()["subscription"]["renewal_date"] == "2026-12-18"
        metadata.set_renewal("c2", "2026-12-19")
        assert a.snapshot()["subscription"]["renewal_date"] == "2026-12-19"
        metadata.set_renewal("c2", None)
        assert a.snapshot()["subscription"]["renewal_date"] is None


def test_past_date_is_not_rolled_forward_or_called_expired():
    with tempfile.TemporaryDirectory() as folder, \
            patch.object(metadata, "RENEWALS_FILE", os.path.join(folder, "subscriptions.json")):
        metadata.set_renewal("c2", "2026-11-18")
        snap = metadata.subscription_snapshot("c2", date(2026, 11, 20))
        assert snap["days_remaining"] == -2
        assert snap["renewal_date"] == "2026-11-18"
        html = proxy.renewal_html(snap)
        assert "Fecha pasada hace 2 días" in html and "Confirma" in html
        assert "expired" not in html.lower() and "vencida" not in html.lower()


def test_panel_shows_unknown_upcoming_today_and_manual_source():
    assert "sin fecha registrada" in proxy.renewal_html(None)
    for days, expected in ((28, "Faltan 28 días"), (1, "Falta 1 día"), (0, "Renueva hoy")):
        html = proxy.renewal_html({"renewal_date": "2026-12-18", "days_remaining": days})
        assert expected in html and "2026-12-18" in html and "Fecha manual" in html
        assert "no confirmada por Claude" in html


def test_corrupt_metadata_does_not_crash_panel_or_get_overwritten():
    with tempfile.TemporaryDirectory() as folder, \
            patch.object(metadata, "RENEWALS_FILE", os.path.join(folder, "subscriptions.json")):
        for value in ("bad JSON", "[]"):
            with open(metadata.RENEWALS_FILE, "w") as fh:
                fh.write(value)
            assert metadata.subscription_snapshot("c2")["renewal_date"] is None
            with TestCase().assertRaises(ValueError):
                metadata.set_renewal("c2", "2026-12-18")
            with open(metadata.RENEWALS_FILE) as fh:
                assert fh.read() == value
        for value in ({"c2": "invalid"}, {"c2": {"renewal_date": "2026-02-31"}}):
            with open(metadata.RENEWALS_FILE, "w") as fh:
                json.dump(value, fh)
            assert metadata.subscription_snapshot("c2")["renewal_date"] is None
