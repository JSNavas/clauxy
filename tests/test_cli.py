"""Account removal must apply to memory as well as stored credentials."""

import importlib.util
import json
import os
import tempfile
from contextlib import ExitStack
from importlib.machinery import SourceFileLoader
from types import SimpleNamespace
from unittest.mock import patch
from unittest import TestCase

from helpers import ROOT

spec = importlib.util.spec_from_loader(
    "clauxy_cli_test", SourceFileLoader("clauxy_cli_test", ROOT + "/clauxy"))
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
import clauxy_accounts as metadata


def _remove_case(running, stored, port=4000):
    with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
        token = os.path.join(folder, "c1.token")
        labels = os.path.join(folder, "accounts.json")
        if stored:
            with open(token, "w") as fh:
                fh.write("test token")
        with open(labels, "w") as fh:
            json.dump({"c1": "first", "c2": "second"} if stored else {"c2": "second"}, fh)
        stack.enter_context(patch.object(cli, "IS_MAC", False))
        stack.enter_context(patch.object(cli, "token_path", return_value=token))
        stack.enter_context(patch.object(cli, "LABELS_FILE", labels))
        clear = stack.enter_context(patch.object(cli, "set_renewal"))
        answers = stack.enter_context(patch.object(cli, "proxy_answers", return_value=running))
        args = SimpleNamespace(alias="c1", port=port)

        def restart(received):
            assert received is args
            assert not os.path.exists(token), "restart must happen after deleting credentials"
            with open(labels) as fh:
                assert json.load(fh) == {"c2": "second"}

        reload = stack.enter_context(patch.object(cli, "cmd_restart", side_effect=restart))
        cli.cmd_remove(args)
        clear.assert_called_once_with("c1", None)
        answers.assert_called_once_with(port)
        return reload.call_count


def test_removing_stored_account_restarts_live_proxy():
    assert _remove_case({"accounts": [{"alias": "c1"}, {"alias": "c2"}]}, True) == 1


def test_removing_account_again_also_drops_stale_in_memory_token():
    assert _remove_case({"accounts": [{"alias": "c1"}, {"alias": "c2"}]}, False) == 1


def test_removing_from_stopped_proxy_does_not_start_it():
    assert _remove_case(None, True) == 0


def test_removing_unknown_account_does_not_restart_proxy():
    assert _remove_case({"accounts": [{"alias": "c2"}]}, False) == 0


def test_removing_account_can_target_custom_proxy_port():
    assert _remove_case({"accounts": [{"alias": "c1"}, {"alias": "c2"}]}, True, 4099) == 1


def test_edit_updates_and_clears_dates_without_restarting():
    with tempfile.TemporaryDirectory() as folder, \
            patch.object(metadata, "RENEWALS_FILE", os.path.join(folder, "subscriptions.json")), \
            patch.object(cli, "all_accounts", return_value=["c2", "c3"]), \
            patch.object(cli, "cmd_restart") as restart:
        cli.cmd_edit(SimpleNamespace(alias="c2", renewal="2026-12-18"))
        assert metadata.subscription_snapshot("c2")["renewal_date"] == "2026-12-18"
        cli.cmd_edit(SimpleNamespace(alias="c2", renewal=None))
        assert metadata.subscription_snapshot("c2")["renewal_date"] is None
        restart.assert_not_called()


def test_edit_rejects_unknown_accounts_and_invalid_dates():
    with patch.object(cli, "all_accounts", return_value=["c2"]), \
            patch.object(cli, "set_renewal") as save:
        with TestCase().assertRaises(SystemExit):
            cli.cmd_edit(SimpleNamespace(alias="missing", renewal="2026-12-18"))
        save.assert_not_called()
    with tempfile.TemporaryDirectory() as folder, \
            patch.object(metadata, "RENEWALS_FILE", os.path.join(folder, "subscriptions.json")), \
            patch.object(cli, "all_accounts", return_value=["c2"]):
        with TestCase().assertRaises(SystemExit):
            cli.cmd_edit(SimpleNamespace(alias="c2", renewal="2026-02-31"))
        assert not os.path.exists(metadata.RENEWALS_FILE)
