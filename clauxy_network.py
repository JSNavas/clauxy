"""Explicit VPN/proxy egress selection. Never fall back to direct upstream traffic."""

import base64
import functools
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import clauxy_vpn as vpn

NetworkUnavailable = vpn.VPNUnavailable
HOME = os.path.expanduser(os.environ.get("CLAUXY_HOME", "~/.clauxy"))
CONFIG_FILE = os.path.join(HOME, "network.json")
_config_lock = threading.Lock()
_state_lock = threading.Lock()
_changed = threading.Event()
_proxy_states = {}


def _host(value):
    if not isinstance(value, str):
        raise ValueError("Enter a valid proxy host or IP address")
    value = value.strip().strip("[]")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    try:
        value = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Enter a valid proxy host or IP address") from exc
    if len(value) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                   for label in value.split(".")):
        raise ValueError("Enter a proxy host or IP address without a URL, path, or credentials")
    return value


def _validate_proxy(value):
    if not isinstance(value, dict) or value.get("type") not in ("http", "socks5"):
        raise ValueError("Choose HTTP CONNECT or SOCKS5")
    host = _host(value.get("host", ""))
    try:
        raw_port = value.get("port", 0)
        if isinstance(raw_port, bool) or not isinstance(raw_port, (str, int)):
            raise ValueError("Invalid port")
        port = int(raw_port)
    except (ValueError, TypeError) as exc:
        raise ValueError("Enter a proxy port between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Enter a proxy port between 1 and 65535")
    username, password = value.get("username", ""), value.get("password", "")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("Invalid proxy credentials")
    if bool(username) != bool(password):
        raise ValueError("Enter both a proxy username and password, or leave both empty")
    if any(ord(char) < 32 or ord(char) == 127 for char in username + password):
        raise ValueError("Proxy credentials cannot contain control characters")
    if len(username.encode()) > 255 or len(password.encode()) > 255:
        raise ValueError("Proxy credentials must be at most 255 bytes")
    if value["type"] == "http" and ":" in username:
        raise ValueError("HTTP proxy usernames cannot contain a colon")
    location = value.get("location", "")
    if not isinstance(location, str) or len(location) > 80:
        raise ValueError("Location labels must be at most 80 characters")
    return {"type": value["type"], "host": host, "port": port,
            "username": username, "password": password, "location": location.strip()}


def load_config():
    try:
        with open(CONFIG_FILE) as fh:
            value = json.load(fh)
    except FileNotFoundError:
        return {"mode": "vpn", "proxy": None}
    except (OSError, ValueError) as exc:
        raise NetworkUnavailable("Cannot read connection settings; requests blocked") from exc
    try:
        if not isinstance(value, dict) or value.get("mode") not in ("vpn", "proxy"):
            raise ValueError("Invalid connection mode")
        proxy = _validate_proxy(value["proxy"]) if value.get("proxy") else None
        if value["mode"] == "proxy" and proxy is None:
            raise ValueError("No proxy configured")
        return {"mode": value["mode"], "proxy": proxy}
    except (ValueError, TypeError, KeyError) as exc:
        raise NetworkUnavailable("Invalid connection settings; requests blocked") from exc


def config_from_form(form, current=None):
    if current is None:
        try:
            current = load_config()
        except NetworkUnavailable:
            current = {"mode": "vpn", "proxy": None}
    mode = form.get("mode", "")
    if mode not in ("vpn", "proxy"):
        raise ValueError("Choose VPN or Proxy")
    old = current.get("proxy") or {}
    # Selecting VPN never saves untested proxy edits or depends on a dead proxy.
    if mode == "vpn":
        return {"mode": mode, "proxy": current.get("proxy")}
    proxy = {key: form.get("proxy_" + key, "")
             for key in ("type", "host", "port", "username", "password", "location")}
    same_endpoint = (proxy["type"] == old.get("type")
                     and _host(proxy["host"]) == old.get("host")
                     and str(proxy["port"]) == str(old.get("port")))
    auth = form.get("proxy_auth", "password")
    if auth == "none":
        proxy["username"] = proxy["password"] = ""
    elif auth == "password":
        if not proxy["username"] and same_endpoint:
            proxy["username"] = old.get("username", "")
        if not proxy["username"]:
            raise ValueError("Enter a username for this proxy endpoint")
        if not proxy["password"] and same_endpoint and proxy["username"] == old.get("username"):
            proxy["password"] = old.get("password", "")
        if not proxy["password"]:
            raise ValueError("Enter the password for this proxy endpoint and username")
    else:
        raise ValueError("Choose a proxy authentication method")
    return {"mode": mode, "proxy": _validate_proxy(proxy)}


def _write_config(config):
    # Validate before replacing the file. Corrupt settings never become a direct mode.
    if config.get("mode") not in ("vpn", "proxy"):
        raise ValueError("Choose VPN or Proxy")
    proxy = _validate_proxy(config["proxy"]) if config.get("proxy") else None
    if config["mode"] == "proxy" and proxy is None:
        raise ValueError("Configure a proxy before activating Proxy mode")
    with _config_lock:
        folder = os.path.dirname(CONFIG_FILE)
        os.makedirs(folder, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".network-", dir=folder)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump({"mode": config["mode"], "proxy": proxy}, fh, indent=2)
                fh.write("\n")
            os.replace(temporary, CONFIG_FILE)  # mkstemp creates mode 0600.
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    _changed.set()


def activate_config(config):
    # A failed test must not persist credentials or replace the active policy.
    result = check_proxy(config["proxy"]) if config["mode"] == "proxy" else None
    _write_config(config)
    return result or {"ok": True, "message": "VPN mode saved. Requests require a verified VPN route."}


def _key(proxy):
    return hashlib.sha256(json.dumps(proxy, sort_keys=True).encode()).digest()


def _record_proxy(proxy, state, detail, exit_ip=None):
    key = _key(proxy)
    with _state_lock:
        previous = _proxy_states.get(key, {})
        _proxy_states[key] = {"state": state, "allowed": state == "connected",
                              "detail": detail, "checked_at": time.time(),
                              "exit_ip": exit_ip or previous.get("exit_ip")}
        if len(_proxy_states) > 16:
            oldest = min(_proxy_states, key=lambda item: _proxy_states[item]["checked_at"])
            del _proxy_states[oldest]


def _upstream():
    url = urllib.parse.urlsplit(os.environ.get("CLAUXY_UPSTREAM", "https://api.anthropic.com"))
    if url.scheme != "https" or not url.hostname:
        raise NetworkUnavailable("The Clauxy upstream must use HTTPS")
    return url.hostname, url.port or 443


def require_network(host=None, port=443, *, external=False):
    config = load_config()
    if external and config["mode"] != "vpn":
        raise NetworkUnavailable("Account sign-in and session renewal require VPN mode; "
                                 "external CLI traffic cannot use Clauxy's protected proxy transport")
    if config["mode"] == "vpn":
        return vpn.require_vpn(host, port)
    # The actual request authenticates, tunnels, and verifies destination TLS.
    # A cached Online badge never authorizes a direct socket.
    return config["proxy"]


def _read_exact(sock, length):
    data = bytearray()
    timeout = sock.gettimeout()
    deadline = time.monotonic() + (timeout if timeout is not None else 8)
    while len(data) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NetworkUnavailable("The proxy handshake timed out; requests blocked", "disconnected")
        sock.settimeout(remaining)
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise NetworkUnavailable("The proxy closed the connection; requests blocked", "disconnected")
        data.extend(chunk)
    sock.settimeout(timeout)
    return bytes(data)


def _http_tunnel(sock, host, port, proxy):
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    headers = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
    if proxy["username"]:
        auth = base64.b64encode((proxy["username"] + ":" + proxy["password"]).encode()).decode()
        headers.append("Proxy-Authorization: Basic " + auth)
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    response = bytearray()
    timeout = sock.gettimeout()
    deadline = time.monotonic() + (timeout if timeout is not None else 8)
    # Read only the CONNECT headers, bounded in size and elapsed time. Do not
    # consume bytes from the destination TLS stream or echo untrusted headers.
    while not response.endswith(b"\r\n\r\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or len(response) >= 8192:
            raise NetworkUnavailable("Invalid or timed-out proxy response; requests blocked", "disconnected")
        sock.settimeout(remaining)
        byte = sock.recv(1)
        if not byte:
            raise NetworkUnavailable("The proxy closed the connection; requests blocked", "disconnected")
        response.extend(byte)
    sock.settimeout(timeout)
    status = re.match(rb"HTTP/1\.[01] ([0-9]{3})(?: |\r)", response)
    if not status:
        raise NetworkUnavailable("Invalid HTTP proxy response; requests blocked", "disconnected")
    code = int(status.group(1))
    if code == 407:
        raise NetworkUnavailable("Proxy authentication failed; check the username and password", "disconnected")
    if code != 200:
        raise NetworkUnavailable(f"Proxy tunnel rejected (HTTP {code}); requests blocked", "disconnected")


def _socks_tunnel(sock, host, port, proxy):
    method = 2 if proxy["username"] else 0
    sock.sendall(bytes((5, 1, method)))
    if _read_exact(sock, 2) != bytes((5, method)):
        raise NetworkUnavailable("SOCKS5 authentication method rejected; requests blocked", "disconnected")
    if method == 2:
        username, password = proxy["username"].encode(), proxy["password"].encode()
        sock.sendall(bytes((1, len(username))) + username + bytes((len(password),)) + password)
        if _read_exact(sock, 2) != b"\x01\x00":
            raise NetworkUnavailable("Proxy authentication failed; check the username and password", "disconnected")
    # Domain names are resolved by the proxy; literal addresses use their RFC
    # address type without performing a local destination DNS lookup.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        destination = host.encode("idna")
        if not 1 <= len(destination) <= 255:
            raise NetworkUnavailable("Invalid destination hostname")
        destination = b"\x03" + bytes((len(destination),)) + destination
    else:
        destination = bytes((1 if address.version == 4 else 4,)) + address.packed
    sock.sendall(b"\x05\x01\x00" + destination + port.to_bytes(2, "big"))
    reply = _read_exact(sock, 4)
    if reply[:3] != b"\x05\x00\x00":
        raise NetworkUnavailable("SOCKS5 tunnel rejected; requests blocked", "disconnected")
    length = {1: 4, 4: 16}.get(reply[3])
    if reply[3] == 3:
        length = _read_exact(sock, 1)[0]
    if length is None:
        raise NetworkUnavailable("Invalid SOCKS5 tunnel response", "disconnected")
    _read_exact(sock, length + 2)


def proxy_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *, proxy):
    host, port = address
    sock = None
    try:
        effective = socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
        # Bound setup time separately from potentially long streamed responses.
        setup_timeout = min(effective, 8) if effective is not None else 8
        sock = socket.create_connection((proxy["host"], proxy["port"]), setup_timeout, source_address)
        host = _host(host)
        if proxy["type"] == "http":
            _http_tunnel(sock, host, port, proxy)
        else:
            _socks_tunnel(sock, host, port, proxy)
        return sock
    except Exception as exc:
        if sock is not None:
            sock.close()
        error = exc if isinstance(exc, NetworkUnavailable) else NetworkUnavailable(
            "Cannot connect through the configured proxy; requests blocked", "disconnected")
        _record_proxy(proxy, "disconnected", str(error))
        raise error from None


class _ProxyHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, proxy, **kwargs):
        super().__init__(*args, **kwargs)
        self._proxy = proxy
        self._create_connection = functools.partial(proxy_create_connection, proxy=proxy)

    def connect(self):
        try:
            super().connect()  # TLS and SNI use the destination, never the proxy.
            self.sock.settimeout(self.timeout if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT else None)
        except Exception as exc:
            self.close()
            error = exc if isinstance(exc, NetworkUnavailable) else NetworkUnavailable(
                "Proxy TLS connection failed; requests blocked", "disconnected")
            # Preserve only our sanitized errors, never a proxy's response text.
            _record_proxy(self._proxy, "disconnected", str(error))
            raise error from None
        _record_proxy(self._proxy, "connected", "Proxy tunnel and destination TLS verified")


class _ProxyHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy):
        super().__init__()
        self.proxy = proxy

    def https_open(self, request):
        factory = functools.partial(_ProxyHTTPSConnection, proxy=self.proxy)
        return self.do_open(factory, request, context=self._context)


def _proxy_urlopen(request, *, proxy, timeout=30):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        _ProxyHTTPSHandler(proxy), vpn._RejectHTTP(), vpn._RejectRedirects())
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, NetworkUnavailable):
            raise exc.reason from exc
        raise


def network_urlopen(request, *, timeout=30):
    config = load_config()
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    if urllib.parse.urlsplit(url).scheme != "https":
        raise NetworkUnavailable("Only protected HTTPS upstream connections are allowed")
    if config["mode"] == "vpn":
        return vpn.vpn_urlopen(request, timeout=timeout)
    if isinstance(request, urllib.request.Request):
        headers = {key: value for key, value in request.header_items()
                   if key.lower() not in ("proxy-authorization", "proxy-connection")}
        request = urllib.request.Request(request.full_url, data=request.data,
                                         headers=headers, method=request.get_method())
    return _proxy_urlopen(request, proxy=config["proxy"], timeout=timeout)


def check_proxy(proxy, report_ip=False):
    proxy = _validate_proxy(proxy)
    host, port = _upstream()
    connection = _ProxyHTTPSConnection(host, port, timeout=5, proxy=proxy)
    try:
        connection.connect()
    finally:
        connection.close()
    exit_ip = None
    if report_ip:
        try:
            # Webshare's public check endpoint works through other providers too.
            with _proxy_urlopen("https://ipv4.webshare.io/", proxy=proxy, timeout=5) as response:
                exit_ip = str(ipaddress.ip_address(response.read(128).decode().strip()))
        except Exception:
            pass
    _record_proxy(proxy, "connected", "Proxy tunnel and destination TLS verified", exit_ip)
    return {"ok": True, "message": "Proxy connection verified" +
            (f" · Exit IP: {exit_ip}" if exit_ip else ""), "exit_ip": exit_ip}


def network_snapshot():
    try:
        config = load_config()
    except NetworkUnavailable as exc:
        return {"mode": "blocked", "proxy": None, "state": "unknown", "allowed": False,
                "detail": str(exc), "checked_at": 0, "interface": None}
    public = dict(config["proxy"]) if config["proxy"] else None
    if public:
        public["has_password"] = bool(public.pop("password"))
        public["has_username"] = bool(public.pop("username"))
    if config["mode"] == "vpn":
        status = vpn.vpn_status()
    else:
        with _state_lock:
            cached = dict(_proxy_states.get(_key(config["proxy"]), {}))
        if cached and time.time() - cached.get("checked_at", 0) <= 90:
            status = cached
        else:
            status = {"state": "unknown", "allowed": False, "checked_at": 0,
                      "detail": "Proxy connection has not been verified yet", "exit_ip": None}
    return dict(status, mode=config["mode"], proxy=public)


def watch_network():
    while True:
        delay = 2
        try:
            config = load_config()
            if config["mode"] == "vpn":
                vpn.require_vpn()
            else:
                delay = 30
                check_proxy(config["proxy"])
        except NetworkUnavailable:
            pass
        _changed.wait(delay)
        _changed.clear()
