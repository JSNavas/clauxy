"""A startup check is one-shot and follows the configured route."""

from unittest.mock import patch
from helpers import load
import clauxy_network as network

proxy = load()


def test_startup_proxy_check_runs_once():
    config = {"mode": "proxy", "proxy": {"type": "http"}}
    with patch.object(network, "load_config", return_value=config), \
         patch.object(network, "check_proxy", return_value={"ok": True}) as check, \
         patch.object(network.vpn, "require_vpn") as vpn_check:
        network.check_network_once()
        check.assert_called_once_with(config["proxy"])
        vpn_check.assert_not_called()


def test_startup_vpn_check_runs_once():
    with patch.object(network, "load_config", return_value={"mode": "vpn"}), \
         patch.object(network.vpn, "require_vpn") as check, \
         patch.object(network, "check_proxy") as proxy_check:
        network.check_network_once()
        check.assert_called_once_with()
        proxy_check.assert_not_called()


def test_startup_check_failure_does_not_crash_service():
    with patch.object(proxy, "check_network_once", side_effect=network.NetworkUnavailable("offline")) as check:
        proxy.check_network_on_start()
        check.assert_called_once_with()
