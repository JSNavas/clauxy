"""Account removal must apply to memory as well as stored credentials."""

import importlib.util
import io
import json
import os
import tempfile
from contextlib import ExitStack, redirect_stdout
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


def test_edit_updates_and_clears_dates_while_leaving_stopped_proxy_stopped():
    with tempfile.TemporaryDirectory() as folder, \
            patch.object(metadata, "RENEWALS_FILE", os.path.join(folder, "subscriptions.json")), \
            patch.object(cli, "all_accounts", return_value=["c2", "c3"]), \
            patch.object(cli, "proxy_answers", return_value=None), \
            patch.object(cli, "cmd_restart") as restart:
        cli.cmd_edit(SimpleNamespace(alias="c2", renewal="2026-12-18", port=4000))
        assert metadata.subscription_snapshot("c2")["renewal_date"] == "2026-12-18"
        cli.cmd_edit(SimpleNamespace(alias="c2", renewal=None, port=4000))
        assert metadata.subscription_snapshot("c2")["renewal_date"] is None
        restart.assert_not_called()


def test_edit_confirms_spanish_date_and_restarts_live_proxy_after_saving():
    with tempfile.TemporaryDirectory() as folder, \
            patch.object(metadata, "RENEWALS_FILE", os.path.join(folder, "subscriptions.json")), \
            patch.object(cli, "all_accounts", return_value=["c3"]), \
            patch.object(cli, "proxy_answers", return_value={"accounts": []}) as answers, \
            patch.object(cli, "cmd_restart") as restart, redirect_stdout(io.StringIO()) as output:
        for value in ("2026-11-29", None):
            args = SimpleNamespace(alias="c3", renewal=value, port=4099)

            def verify_saved(received):
                assert received is args
                assert metadata.subscription_snapshot("c3")["renewal_date"] == value

            restart.side_effect = verify_saved
            cli.cmd_edit(args)
            restart.assert_called_with(args)
        assert restart.call_count == 2
        assert answers.call_args.args == (4099,)
        assert "renovación el 29 de noviembre" in output.getvalue()


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


def test_label_restarts_after_saving_but_listing_labels_does_not():
    args = SimpleNamespace(alias="c2", label="updated", port=4099)
    with patch.object(cli, "write_label") as save, \
            patch.object(cli, "proxy_answers", return_value={}), \
            patch.object(cli, "cmd_restart") as restart:
        restart.side_effect = lambda _: save.assert_called_once_with("c2", "updated")
        cli.cmd_label(args)
        restart.assert_called_once_with(args)
    with patch.object(cli, "read_labels", return_value={}), \
            patch.object(cli, "all_accounts", return_value=[]), \
            patch.object(cli, "apply_configuration") as apply:
        cli.cmd_label(SimpleNamespace(alias=None, label=None, port=4000))
        apply.assert_not_called()


def test_login_applies_new_session_even_if_quota_probe_fails():
    for failed_probe in (False, True):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            stack.enter_context(patch.object(cli, "SESSIONS", folder))
            stack.enter_context(patch.object(cli, "require_vpn"))
            stack.enter_context(patch.object(cli, "require_claude_cli"))
            stack.enter_context(patch.object(cli, "read_labels", return_value={}))
            stack.enter_context(patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=0)))
            stack.enter_context(patch.object(cli, "session_credential", return_value={"accessToken": "test"}))
            stack.enter_context(patch.object(cli, "proxy_answers", return_value={}))
            restart = stack.enter_context(patch.object(cli, "cmd_restart"))
            response = stack.enter_context(patch.object(cli, "vpn_urlopen"))
            if failed_probe:
                response.side_effect = cli.urllib.error.HTTPError("https://example.com", 403, "denied", {}, None)
                with TestCase().assertRaises(SystemExit):
                    cli.cmd_login(SimpleNamespace(alias="test", port=4000))
            else:
                response.return_value.__enter__.return_value = io.StringIO("{}")
                cli.cmd_login(SimpleNamespace(alias="test", port=4000))
            restart.assert_called_once()


def test_add_applies_changes_once_after_nested_login_or_interruption():
    for interrupted in (False, True):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            stack.enter_context(patch.object(cli, "SESSIONS", folder))
            stack.enter_context(patch.object(cli, "require_vpn"))
            stack.enter_context(patch.object(cli, "require_claude_cli"))
            stack.enter_context(patch.object(cli, "run_showing", return_value="sk-ant-oat01-" + "x" * 40))
            stack.enter_context(patch.object(cli, "token_works", return_value=("ok", "OK")))
            saved = stack.enter_context(patch.object(cli, "save_token", return_value="test token file"))
            stack.enter_context(patch.object(cli, "write_label"))
            stack.enter_context(patch.object(cli, "proxy_answers", return_value={}))
            restart = stack.enter_context(patch.object(cli, "cmd_restart"))
            login = stack.enter_context(patch.object(cli, "cmd_login", side_effect=cli.apply_configuration))
            answers = [KeyboardInterrupt()] if interrupted else ["label", "y"]
            stack.enter_context(patch("builtins.input", side_effect=answers))
            args = SimpleNamespace(alias="test", keychain=False, port=4099)
            if interrupted:
                with TestCase().assertRaises(KeyboardInterrupt):
                    cli.cmd_add(args)
                login.assert_not_called()
            else:
                cli.cmd_add(args)
                login.assert_called_once_with(args)
            saved.assert_called_once()
            restart.assert_called_once_with(args)
            assert not args._defer_restart


def test_configuration_changes_leave_stopped_proxy_stopped():
    with patch.object(cli, "proxy_answers", return_value=None), \
            patch.object(cli, "cmd_restart") as restart:
        args = SimpleNamespace(alias="test")
        cli.apply_configuration(args)
        assert args.port == 4000
        restart.assert_not_called()
