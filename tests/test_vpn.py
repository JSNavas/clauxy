"""VPN safety checks use local fakes; never disconnect the user's VPN."""

import subprocess
import json
import time
import importlib.util
from importlib.machinery import SourceFileLoader
from types import SimpleNamespace
from unittest.mock import patch

from helpers import ROOT, account, load

proxy = load()
import clauxy_vpn as vpn


ADDRESS = (vpn.socket.AF_INET, vpn.socket.SOCK_STREAM, 6, "", ("104.18.0.1", 443))
CANDIDATE = (vpn.socket.AF_INET, vpn.socket.SOCK_STREAM, 6,
             ("104.18.0.1", 443), "utun420", 24)


def test_macos_requires_destination_route_and_active_tunnel():
    with patch.object(vpn.sys, "platform", "darwin"), \
            patch.object(vpn, "_command", side_effect=[
                "  interface: utun420\n",
                "utun420: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1420\n"]), \
            patch.object(vpn.socket, "if_nametoindex", return_value=24):
        assert vpn._tunnel_for("104.18.0.1") == ("utun420", 24)
    for outputs in (["  interface: en0\n"], ["  interface: lo0\n"],
                    ["  interface: utun420\n", "utun420: flags=8010<POINTOPOINT,MULTICAST>\n"],
                    ["unexpected output"]):
        with patch.object(vpn.sys, "platform", "darwin"), \
                patch.object(vpn, "_command", side_effect=outputs):
            try:
                vpn._tunnel_for("104.18.0.1")
            except vpn.VPNUnavailable:
                continue
            raise AssertionError("direct, down or unknown interface was accepted")


def test_unverifiable_routes_fail_closed():
    for failure in (OSError("denied"), subprocess.TimeoutExpired("route", 3)):
        with patch.object(vpn.subprocess, "run", side_effect=failure):
            try:
                vpn._command(["route"])
            except vpn.VPNUnavailable:
                continue
            raise AssertionError("failed command was accepted")
    with patch.object(vpn.sys, "platform", "win32"):
        try:
            vpn._tunnel_for("104.18.0.1")
        except vpn.VPNUnavailable:
            return
        raise AssertionError("unsupported system was accepted")


def test_direct_ipv6_is_never_used_as_fallback():
    ipv6 = (vpn.socket.AF_INET6, vpn.socket.SOCK_STREAM, 6, "",
            ("2606:4700::1", 443, 0, 0))
    with patch.object(vpn.socket, "getaddrinfo", return_value=[ipv6, ADDRESS]), \
            patch.object(vpn, "_tunnel_for", side_effect=[
                vpn.VPNUnavailable("direct", "disconnected"), ("utun420", 24)]):
        assert vpn._candidates("api.anthropic.com", 443) == [CANDIDATE]


def test_private_destinations_and_dns_failures_block():
    private = (vpn.socket.AF_INET, vpn.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
    with patch.object(vpn.socket, "getaddrinfo", return_value=[private]), \
            patch.object(vpn, "_tunnel_for") as route:
        try:
            vpn._candidates("localhost", 443)
        except vpn.VPNUnavailable:
            pass
        else:
            raise AssertionError("loopback upstream was accepted")
        route.assert_not_called()
    with patch.object(vpn.socket, "getaddrinfo", side_effect=OSError("DNS failed")):
        try:
            vpn.require_vpn()
        except vpn.VPNUnavailable:
            return
        raise AssertionError("failed DNS lookup was accepted")


def test_socket_binds_before_connecting_and_reuses_checked_address():
    events = []
    with patch.object(vpn, "require_vpn", return_value=[CANDIDATE]), \
            patch.object(vpn, "_tunnel_for", return_value=("utun420", 24)), \
            patch.object(vpn.socket, "socket") as factory, \
            patch.object(vpn, "_bind_tunnel", side_effect=lambda *args: events.append("bind")):
        sock = factory.return_value
        sock.connect.side_effect = lambda address: events.append(address)
        assert vpn.vpn_create_connection(("api.anthropic.com", 443), timeout=5) is sock
        assert events == ["bind", ("104.18.0.1", 443)]
        sock.settimeout.assert_called_once_with(5)


def test_bind_failure_or_route_change_never_connects_directly():
    for route, bind_error in ((("utun420", 24), OSError("bind denied")),
                              (("en0", 1), None)):
        with patch.object(vpn, "require_vpn", return_value=[CANDIDATE]), \
                patch.object(vpn, "_tunnel_for", return_value=route), \
                patch.object(vpn.socket, "socket") as factory, \
                patch.object(vpn, "_bind_tunnel", side_effect=bind_error):
            try:
                vpn.vpn_create_connection(("api.anthropic.com", 443))
            except vpn.VPNUnavailable:
                pass
            else:
                raise AssertionError("unsafe socket was returned")
            factory.return_value.connect.assert_not_called()
            if route[0] == "utun420":
                factory.return_value.close.assert_called_once()


def test_macos_binds_both_address_families():
    from unittest.mock import Mock
    sock = Mock()
    with patch.object(vpn.sys, "platform", "darwin"):
        vpn._bind_tunnel(sock, vpn.socket.AF_INET, "utun420", 24)
        sock.setsockopt.assert_called_with(vpn.socket.IPPROTO_IP, 25, 24)
        vpn._bind_tunnel(sock, vpn.socket.AF_INET6, "utun420", 24)
        sock.setsockopt.assert_called_with(vpn.socket.IPPROTO_IPV6, 125, 24)


def test_linux_requires_tunnel_kind_not_just_a_vpn_like_name():
    for kind in ("tun", "wireguard", "dummy", "bridge", None):
        outputs = [json.dumps([{"dev": "wg0"}]), json.dumps([
            {"flags": ["UP"], "linkinfo": {"info_kind": kind}}])]
        with patch.object(vpn.sys, "platform", "linux"), \
                patch.object(vpn.shutil, "which", return_value="/sbin/ip"), \
                patch.object(vpn, "_command", side_effect=outputs), \
                patch.object(vpn.socket, "if_nametoindex", return_value=7):
            if kind in ("tun", "wireguard"):
                assert vpn._tunnel_for("104.18.0.1") == ("wg0", 7)
            else:
                try:
                    vpn._tunnel_for("104.18.0.1")
                except vpn.VPNUnavailable:
                    continue
                raise AssertionError("ordinary Linux device was accepted as VPN")


def test_tunnel_failure_never_falls_back_to_socket_create_connection():
    from unittest.mock import Mock
    sock = Mock()
    sock.connect.side_effect = OSError("tunnel disappeared")
    with patch.object(vpn, "require_vpn", return_value=[CANDIDATE]), \
            patch.object(vpn, "_tunnel_for", return_value=("utun420", 24)), \
            patch.object(vpn.socket, "socket", return_value=sock), \
            patch.object(vpn, "_bind_tunnel"), \
            patch.object(vpn.socket, "create_connection") as direct:
        try:
            vpn.vpn_create_connection(("api.anthropic.com", 443))
        except vpn.VPNUnavailable:
            pass
        else:
            raise AssertionError("failed tunnel was accepted")
        sock.close.assert_called_once()
        direct.assert_not_called()


def test_bound_transport_keeps_tls_and_original_hostname():
    from unittest.mock import Mock
    import ssl
    context = ssl.create_default_context()
    connection = vpn._VPNHTTPSConnection("api.anthropic.com", context=context)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert connection._create_connection is vpn.vpn_create_connection
    sock = Mock()
    with patch.object(connection, "_create_connection", return_value=sock), \
            patch.object(context, "wrap_socket", return_value=sock) as wrap:
        connection.connect()
        wrap.assert_called_once_with(sock, server_hostname="api.anthropic.com")
    connection.close()


def test_display_tracks_disconnect_reconnect_and_stale_checks():
    with patch.object(vpn, "_last_status", dict(vpn._last_status)), \
            patch.object(vpn, "_candidates", side_effect=[
                [CANDIDATE], vpn.VPNUnavailable("off", "disconnected"), [CANDIDATE]]):
        vpn.require_vpn()
        assert vpn.vpn_status()["allowed"]
        assert vpn.vpn_status()["interface"] == "utun420"
        try:
            vpn.require_vpn()
        except vpn.VPNUnavailable:
            pass
        assert vpn.vpn_status()["state"] == "disconnected"
        vpn.require_vpn()
        assert vpn.vpn_status()["state"] == "connected"
        vpn._last_status["checked_at"] = time.time() - 11
        assert vpn.vpn_status()["state"] == "unknown"
        assert not vpn.vpn_status()["allowed"]


def test_transport_ignores_proxies_and_unwraps_vpn_errors():
    with patch.object(vpn.urllib.request, "build_opener") as build:
        build.return_value.open.side_effect = vpn.urllib.error.URLError(vpn.VPNUnavailable("off"))
        try:
            vpn.vpn_urlopen("https://api.anthropic.com")
        except vpn.VPNUnavailable:
            pass
        else:
            raise AssertionError("VPN failure was hidden in URLError")
        handlers = build.call_args.args
        assert isinstance(handlers[0], vpn.urllib.request.ProxyHandler)
        assert handlers[0].proxies == {}
        assert any(isinstance(h, vpn._VPNHTTPSHandler) for h in handlers)
    for action in (lambda: vpn.vpn_urlopen("http://api.anthropic.com"),
                   lambda: vpn._RejectRedirects().redirect_request(None, None, 302, "", {}, "https://other.com")):
        try:
            action()
        except vpn.VPNUnavailable:
            continue
        raise AssertionError("unsafe transport was accepted")


def test_blocked_quota_does_not_renew_or_send():
    a = account("test")
    with patch.object(proxy, "require_vpn", side_effect=vpn.VPNUnavailable("VPN off")), \
            patch.object(a, "session_token") as session, \
            patch.object(proxy, "vpn_urlopen") as network:
        a.probe_quota()
        assert a.quota_error == "VPN off"
        session.assert_not_called()
        network.assert_not_called()


def test_blocked_proxy_returns_503_but_panel_remains_available():
    handler = object.__new__(proxy.Handler)
    handler.path = "/v1/messages"
    with patch.object(proxy, "require_vpn", side_effect=vpn.VPNUnavailable("VPN off")), \
            patch.object(handler, "send_error") as error, \
            patch.object(handler, "_attempt") as attempt:
        handler._handle("POST")
        error.assert_called_once_with(503, "VPN off")
        attempt.assert_not_called()
        handler.path = "/_status.json"
        with patch.object(handler, "_panel") as panel:
            handler._handle("GET")
            panel.assert_called_once()


def test_disconnect_between_retries_is_not_swallowed():
    handler = object.__new__(proxy.Handler)
    handler.path = "/v1/messages"
    with patch.object(proxy, "vpn_urlopen", side_effect=vpn.VPNUnavailable("off")):
        try:
            handler._attempt("POST", {}, b"{}", account("test"))
        except vpn.VPNUnavailable:
            return
        raise AssertionError("VPN block must reach the HTTP 503 handler")


def test_blocked_session_renewal_never_launches_claude():
    with patch.object(proxy, "require_vpn", side_effect=vpn.VPNUnavailable("off")), \
            patch.object(proxy.subprocess, "run") as launch:
        assert proxy.renew_session("test") is None
        launch.assert_not_called()


def test_account_commands_block_before_launching_claude():
    spec = importlib.util.spec_from_loader(
        "clauxy_cli_vpn_test", SourceFileLoader("clauxy_cli_vpn_test", ROOT + "/clauxy"))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    for action in (lambda: cli.cmd_add(SimpleNamespace(alias="test")),
                   lambda: cli.cmd_login(SimpleNamespace(alias="test")),
                   lambda: cli.token_works("token", "/tmp/test"),
                   lambda: cli.run_showing(["claude", "setup-token"], {})):
        with patch.object(cli, "require_vpn", side_effect=vpn.VPNUnavailable("off")), \
                patch.object(cli.subprocess, "run") as run, \
                patch.object(cli.subprocess, "Popen") as popen, \
                patch.object(cli, "banner"):
            try:
                action()
            except vpn.VPNUnavailable:
                pass
            else:
                raise AssertionError("CLI command bypassed VPN guard")
            run.assert_not_called()
            popen.assert_not_called()
