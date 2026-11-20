"""Provider-independent tunnel routing and interface-bound HTTPS transport.

No provider CLI or firewall setting is trusted. Route checks select candidates;
each actual socket is bound to the selected tunnel before connecting.
"""

import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class VPNUnavailable(OSError):
    def __init__(self, message, state="unknown"):
        super().__init__(message)
        self.state = state


_last_status = {"state": "unknown", "allowed": False, "checked_at": 0,
                "interface": None, "detail": "VPN unverified"}


def _record(state, detail, interface=None):
    global _last_status
    _last_status = {"state": state, "allowed": state == "connected",
                    "checked_at": time.time(), "interface": interface,
                    "detail": detail}


def _command(args):
    try:
        run = subprocess.run(args, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VPNUnavailable("Could not verify the VPN route; requests blocked") from exc
    if run.returncode:
        raise VPNUnavailable("VPN route unavailable; requests blocked", "disconnected")
    return run.stdout


def _tunnel_for(address):
    """Return (interface, index) only for an UP tunnel on the destination route."""
    if sys.platform == "darwin":
        family = ["-inet6"] if ":" in address else []
        route = _command(["/sbin/route", "-n", "get", *family, address])
        match = re.search(r"^\s*interface:\s*(\S+)\s*$", route, re.MULTILINE)
        interface = match.group(1) if match else ""
        # utun is macOS's user tunnel interface, shared by VPN providers.
        if not re.fullmatch(r"utun\d+", interface):
            raise VPNUnavailable("The destination route is outside the VPN; requests blocked",
                                 "disconnected")
        config = _command(["/sbin/ifconfig", interface])
        flags = re.search(r"flags=\w+<([^>]+)>", config)
        if not flags or not {"UP", "RUNNING", "POINTOPOINT"} <= set(flags.group(1).split(",")):
            raise VPNUnavailable("The VPN tunnel is not active; requests blocked", "disconnected")
    elif sys.platform.startswith("linux"):
        binary = shutil.which("ip")
        if not binary:
            raise VPNUnavailable("iproute2 is required to verify the VPN")
        try:
            routes = json.loads(_command([binary, "-j", "route", "get", address]))
            interface = routes[0]["dev"]
            links = json.loads(_command([binary, "-j", "-d", "link", "show", "dev", interface]))
            link = links[0]
            kind = link.get("linkinfo", {}).get("info_kind")
            if kind not in ("tun", "wireguard") or "UP" not in link.get("flags", []):
                raise VPNUnavailable("The destination route does not use an active VPN tunnel",
                                     "disconnected")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise VPNUnavailable("Could not verify the VPN interface") from exc
    else:
        raise VPNUnavailable("VPN verification is supported on macOS and Linux; requests blocked")
    try:
        return interface, socket.if_nametoindex(interface)
    except OSError as exc:
        raise VPNUnavailable("The VPN interface disappeared; requests blocked", "disconnected") from exc


def _candidates(host, port):
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise VPNUnavailable("Could not resolve the destination to verify its VPN route") from exc
    candidates = []
    errors = []
    seen = set()
    for family, socktype, proto, _, sockaddr in addresses:
        if (family, sockaddr) in seen:
            continue
        seen.add((family, sockaddr))
        try:
            address = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
            if not address.is_global:
                raise VPNUnavailable("The destination is not public; requests blocked")
            interface, index = _tunnel_for(sockaddr[0])
            candidates.append((family, socktype, proto, sockaddr, interface, index))
        except VPNUnavailable as exc:
            errors.append(exc)
    # IPv6 may bypass the VPN even when IPv4 is tunneled: never use it as fallback.
    if not candidates:
        error = errors[-1] if errors else VPNUnavailable("No verifiable VPN route is available")
        _record(error.state, str(error))
        raise error
    return candidates


def require_vpn(host=None, port=443):
    if host is None:
        upstream = urllib.parse.urlsplit(os.environ.get(
            "CLAUXY_UPSTREAM", "https://api.anthropic.com"))
        host, port = upstream.hostname, upstream.port or 443
        if upstream.scheme != "https" or not host:
            raise VPNUnavailable("The Clauxy upstream must use HTTPS")
    try:
        candidates = _candidates(host, port)
    except VPNUnavailable as exc:
        _record(exc.state, str(exc))
        raise
    interfaces = ", ".join(sorted({item[4] for item in candidates}))
    _record("connected", "Destination route verified through the VPN tunnel", interfaces)
    return candidates


def _bind_tunnel(sock, family, interface, index):
    if sys.platform == "darwin":
        # Darwin SDK: netinet/in.h IP_BOUND_IF=25; netinet6/in6.h IPV6_BOUND_IF=125.
        level, option = ((socket.IPPROTO_IP, 25) if family == socket.AF_INET
                         else (socket.IPPROTO_IPV6, 125))
        sock.setsockopt(level, option, index)
    elif sys.platform.startswith("linux"):
        # Kernel binding fails closed when the account lacks the required capability.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
    else:
        raise VPNUnavailable("Cannot bind the connection to a tunnel on this system")


def vpn_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    host, port = address
    candidates = require_vpn(host, port)
    errors = []
    for family, socktype, proto, sockaddr, interface, index in candidates:
        sock = None
        try:
            # Check again immediately before binding; connect to this numeric address
            # without a second DNS lookup that could select a different destination.
            if _tunnel_for(sockaddr[0]) != (interface, index):
                raise VPNUnavailable("The VPN route changed before connecting", "disconnected")
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            _bind_tunnel(sock, family, interface, index)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except (OSError, VPNUnavailable) as exc:
            if sock is not None:
                sock.close()
            errors.append(exc)
    error = VPNUnavailable("Could not connect through the VPN tunnel; no direct-network fallback",
                           "disconnected")
    _record(error.state, str(error))
    raise error from (errors[-1] if errors else None)


class _VPNHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = vpn_create_connection


class _VPNHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_VPNHTTPSConnection, request, context=self._context)


class _RejectHTTP(urllib.request.HTTPHandler):
    def http_open(self, request):
        raise VPNUnavailable("Only HTTPS connections through the VPN are allowed")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise VPNUnavailable("Redirect blocked to protect credentials")


def vpn_urlopen(request, *args, **kwargs):
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    if urllib.parse.urlsplit(url).scheme != "https":
        raise VPNUnavailable("Only HTTPS connections through the VPN are allowed")
    # Ignore environment/system proxies: otherwise their route replaces the
    # destination route and could bypass interface binding or expose credentials.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _VPNHTTPSHandler(), _RejectHTTP(), _RejectRedirects())
    try:
        return opener.open(request, *args, **kwargs)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, VPNUnavailable):
            raise exc.reason from exc
        raise


def vpn_status():
    status = dict(_last_status)
    if time.time() - status["checked_at"] > 10:
        status.update(state="unknown", allowed=False,
                      detail="The VPN verification is stale")
    return status


def watch_vpn():
    """Refresh the display even when no client is making requests."""
    while True:
        try:
            require_vpn()
        except VPNUnavailable:
            pass
        time.sleep(2)
