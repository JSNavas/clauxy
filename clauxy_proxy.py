#!/usr/bin/env python3
"""clauxy proxy: one endpoint, many Claude subscriptions.

Clients point ANTHROPIC_BASE_URL at this proxy. It forwards every request
upstream untouched except for the Authorization header, which it swaps for
the token of the account it picks. When an account runs out of quota the
same request is retried on the next one, so the client never sees the 429.

Not a translating gateway: the client's own headers go through verbatim, and
so does the body but for one addition — the CLI banner is put first in
`system`, in its own block, because a subscription token is only served to
Claude Code and that block is how the upstream tells. The caller's own prompt
rides along untouched behind it. See with_cli_system().

Standard library only.
"""

import argparse
import collections
import gzip
import hashlib
import http.server
import json
import math
import os
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html import escape as esc
from datetime import datetime, timedelta, timezone

if sys.version_info < (3, 10):
    sys.exit(f"clauxy needs Python 3.10 or newer (this is "
             f"{sys.version_info.major}.{sys.version_info.minor}).")

UPSTREAM = os.environ.get("CLAUXY_UPSTREAM", "https://api.anthropic.com")
HOME = os.path.expanduser(os.environ.get("CLAUXY_HOME", "~/.clauxy"))
TOKENS = os.path.join(HOME, "tokens")
KEYCHAIN_PREFIX = "clauxy-account-"
RENEW_EVERY = 600          # seconds between attempts to renew a sign-in

# Hop-by-hop headers: they belong to the connection, not the message, or we
# rewrite them ourselves.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "host", "content-length", "authorization", "x-api-key",
}

# Statuses where the server did no work, so retrying on another account is
# safe and cannot duplicate anything.
RETRYABLE = {429, 529}

# The upstream serves a subscription token only to Claude Code, and this exact
# sentence, first and alone in its own `system` block, is how it tells. See
# with_cli_system().
CLI_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
# Endpoints that take a `system` prompt and so need the banner.
SYSTEM_PATHS = ("/v1/messages",)

DEFAULT_COOLDOWN = 300        # si la respuesta no trae `retry-after`
PROBE_EVERY = 60              # how often the background probe asks
PROFILE_EVERY = 6 * 3600      # identity barely changes; ask for it rarely
PROBE_BACKOFF_S = 120         # `/api/oauth/usage` rate-limits separately
MAX_COOLDOWN = 8 * 24 * 3600  # tope. Alto a proposito: el limite SEMANAL puede
                              # be more than 24 h away, and a short cap makes the
                              # panel promise a comeback that will not happen.
LOG_PAGE = 10                 # rows per page in the request log
SWITCH_PAGE = 5               # rows per page in the switch table
AUDIT_MAX = 500           # requests kept in the in-memory log

# Switch thresholds, as a % of each window. We move BEFORE hitting the wall:
# waiting for 100% means spending a 429 just to find out.
THRESHOLD_5H = 80.0
# The WEEKLY window is the one that is actually lost. Whatever is left in it
# when the week rolls over is gone, so there is no margin to keep here: an
# account serves until telemetry says 100%, and `out_of_quota` catches that
# without needing a 429. Stopping at 98 handed back the last 2% of every
# subscription, week after week. The hour is different and keeps its margin:
# it refills, so leaving a little in it costs nothing.
THRESHOLD_7D = 100.0
# Hysteresis, so two accounts hovering at the line never ping-pong.
SWITCH_MARGIN = 5.0           # points the other must win by
COOLDOWN_S = 300          # minimo entre cambios no forzados
PERISHABLE_MARGIN_S = 3600    # how much sooner it must expire to jump
EVENTS_MAX = 100
LABELS = {}          # alias -> label, filled at startup

# USD per million tokens. The spend shown is INDICATIVE: on a subscription you
# do not pay per token, so this is not a bill — it is what the same traffic
# would cost at public API prices, useful to compare accounts.
PRICES = {
    "claude-opus-5-5":   {"in": 4.00, "out": 20.00, "cache_read": 0.20},
    "claude-opus-5":     {"in": 5.00, "out": 25.00, "cache_read": 0.50},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_read": 0.20},
    "claude-sonnet-5":   {"in": 2.00, "out": 10.00, "cache_read": 0.20},
    "claude-haiku-5-5":  {"in": 0.10, "out": 0.50,  "cache_read": 0.01},
    "claude-haiku-4-5":  {"in": 1.00, "out": 5.00,  "cache_read": 0.10},
}
DEFAULT_PRICE = {"in": 2.00, "out": 10.00, "cache_read": 0.20}


def cost_usd(model_, usage):
    """Indicative USD cost of one request, at public API list prices."""
    p = PRICES.get(model_ or "", DEFAULT_PRICE)
    # Cache writes bill at 1.25x input; cache reads at the reduced rate.
    return (
        usage.get("input", 0) * p["in"]
        + usage.get("output", 0) * p["out"]
        + usage.get("cache_write", 0) * p["in"] * 1.25
        + usage.get("cache_read", 0) * p["cache_read"]
    ) / 1_000_000


def decompress(buffer, codificacion):
    """Makes the body readable so it can be counted. Accounting only.

    The client is ALWAYS sent the original body, compressed or not: this is a
    separate copy. Clients ask for gzip, and without inflating it the counter
    read binary and reported 0 tokens on every request.
    """
    cod = (codificacion or "").lower().strip()
    try:
        if cod == "gzip":
            return gzip.decompress(buffer)
        if cod in ("deflate", "zlib"):
            return zlib.decompress(buffer, -zlib.MAX_WBITS)
    except Exception:
        # A truncated body (only 256 KB are kept) cannot be fully inflated.
        # Give up on counting rather than break the request.
        return b""
    return buffer


def usage_from_body(buffer):
    """Pulls model and token counts out of a Messages API response.

    Handles both shapes: an SSE stream (`message_start` carries the input and
    both cache counters, `message_delta` the output) and a single JSON
    response, which has `usage` at the root.
    """
    usage = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
    model_ = None

    # Non-streaming response: plain JSON with `usage` at the top.
    try:
        item = json.loads(buffer)
        if isinstance(item, dict) and "usage" in item:
            u = item.get("usage") or {}
            return item.get("model"), {
                "input": u.get("input_tokens", 0) or 0,
                "output": u.get("output_tokens", 0) or 0,
                "cache_write": u.get("cache_creation_input_tokens", 0) or 0,
                "cache_read": u.get("cache_read_input_tokens", 0) or 0,
            }
    except (ValueError, UnicodeDecodeError, TypeError):
        pass

    for line in buffer.split(b"\n"):
        if not line.startswith(b"data: "):
            continue
        try:
            item = json.loads(line[6:])
        except (ValueError, UnicodeDecodeError):
            continue
        if item.get("type") == "message_start":
            msg = item.get("message", {})
            model_ = msg.get("model") or model_
            u = msg.get("usage", {})
            usage["input"] = u.get("input_tokens", 0)
            usage["cache_write"] = u.get("cache_creation_input_tokens", 0) or 0
            usage["cache_read"] = u.get("cache_read_input_tokens", 0) or 0
            usage["output"] = u.get("output_tokens", 0) or 0
        elif item.get("type") == "message_delta":
            u = item.get("usage", {})
            if u.get("output_tokens") is not None:
                usage["output"] = u["output_tokens"]
    return model_, usage


LABELS_FILE = os.path.join(HOME, "accounts.json")


def read_labels():
    """alias -> label, set by hand with `clauxy label`.

    It cannot be probed: a `setup-token` gets a 403 on /api/oauth/profile,
    the same as on /api/oauth/usage.
    """
    try:
        with open(LABELS_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


STATE_FILE = os.path.join(HOME, "state.json")


def session_credential(alias):
    """The credential `claude auth login` leaves for that config folder.

    On macOS it is NOT a file: it goes to the Keychain, one entry per config
    folder, named `Claude Code-credentials-<sha256(folder)[:8]>`. The user's
    own session lives in `Claude Code-credentials`, with no suffix, and is
    never touched.
    """
    folder = os.path.join(HOME, "sessions", alias)
    # Literal path and realpath: the Keychain entry is keyed by the folder
    # path, so a session dir symlinked in from elsewhere is only found through
    # its target. That is how an existing sign-in can be reused.
    for candidate in dict.fromkeys((folder, os.path.realpath(folder))):
        suffix = hashlib.sha256(candidate.encode()).hexdigest()[:8]
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", f"Claude Code-credentials-{suffix}", "-w"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            continue
        try:
            oauth = json.loads(out.stdout.strip()).get("claudeAiOauth")
        except ValueError:
            continue
        if oauth:
            return oauth
    return None


def claude_cli():
    """Where the Claude CLI lives, for a service with a bare PATH.

    launchd gives the daemon almost no PATH, so `which` alone finds nothing
    and the renewal below would look broken on exactly the machines it is
    meant to run on.
    """
    found = shutil.which("claude")
    if found:
        return found
    for guess in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                  os.path.expanduser("~/.claude/local/claude"),
                  os.path.expanduser("~/.local/bin/claude")):
        if os.path.isfile(guess) and os.access(guess, os.X_OK):
            return guess
    return None


def renew_session(alias):
    """Renews an expired sign-in by letting the Claude CLI do it.

    The session token lasts hours and the refresh token weeks, so the sign-in
    does not really need repeating — something just has to spend the refresh
    token before it lapses. Anthropic's token endpoint refuses callers that
    are not the CLI, so clauxy asks the CLI: one throwaway prompt against that
    account's config dir, which makes it notice the expiry, renew, and write
    the new pair back to the Keychain, where probe_quota reads it. The prompt
    itself may well fail — a drained account answers with its limit — and
    that is fine: the renewal happens before the answer does.

    The environment is scrubbed of ANTHROPIC_* on purpose: inherited from the
    proxy they would send this through clauxy itself, which serves it with the
    long-lived work token and renews nothing.
    """
    binary = claude_cli()
    # Realpath, like session_credential: the Keychain entry is keyed by the
    # folder the CLI resolves to, and handed the symlink it reports "not
    # logged in" and renews nothing.
    folder = os.path.realpath(os.path.join(HOME, "sessions", alias))
    if not binary or not os.path.isdir(folder):
        return None
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))}
    env["CLAUDE_CONFIG_DIR"] = folder
    try:
        subprocess.run([binary, "-p", "ping", "--model", "haiku"],
                       env=env, cwd=tempfile.gettempdir(),
                       capture_output=True, text=True, timeout=180)
    except Exception:
        return None
    oauth = session_credential(alias)
    if oauth and (oauth.get("expiresAt") or 0) / 1000 > time.time():
        return oauth
    return None


def save_state(pool):
    """Flushes counters, holds and the request log to disk.

    Without this a restart wiped the whole history and — worse — the time each
    drained account comes back, so the proxy retried it as if it had quota and
    spent another 429 finding out.

    Written to a temp file and renamed, which is atomic: if the process dies
    halfway the previous file is left intact instead of corrupted.
    """
    datos = {
        "saved": time.time(),
        "failovers": pool.failovers,
        "audit": list(pool.audit),
        # The switch table was the one thing a restart still wiped, so it
        # always showed a lone "startup" row and nothing else.
        "events": list(pool.events),
        "accounts": {
            a.alias: {
                "requests": a.requests,
                "switches_away": a.switches_away,
                "tokens": a.tokens,
                "usd": a.usd,
                "cooling_until": a.cooling_until,
                "limit_text": a.limit_text,
                "last_status": a.last_status,
                # Quota too: without it the bars go blank after every restart
                # and stay blank until that account serves something.
                "quota": a.quota,
                "limits": a.limits,
                # When it was read, so a restart does not re-ask for something
                # measured seconds ago, and does not jump a backoff either.
                "quota_at": a.quota_at,
                "profile_at": a.profile_at,
                "probe_blocked_until": a.probe_blocked_until,
                "email": a.email,
                "plan": a.plan,
            }
            for a in pool.accounts
        },
    }
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(datos, fh)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        sys.stderr.write(f"[proxy] no pude guardar el estado: {exc}\n")


def load_state(pool):
    """Restores what was saved. Accounts that no longer exist are ignored."""
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    pool.failovers = data.get("failovers", 0)
    for entry in reversed(data.get("events") or []):
        pool.events.appendleft(entry)
    for entry in reversed(data.get("audit", [])):
        # Records from before favicon requests were answered locally: they
        # were never real traffic and only clutter the log.
        if entry.get("path") == "/favicon.ico" or (
                entry.get("status") == 404 and entry.get("model") is None
                and not entry.get("path")):
            continue
        pool.audit.appendleft(entry)
    for account in pool.accounts:
        saved = (data.get("accounts") or {}).get(account.alias)
        if not saved:
            continue
        account.requests = saved.get("requests", 0)
        account.switches_away = saved.get("switches_away", 0)
        account.tokens.update(saved.get("tokens") or {})
        account.usd = saved.get("usd", 0.0)
        # The wait is an absolute time: once it passes the account returns.
        account.cooling_until = saved.get("cooling_until", 0.0)
        account.limit_text = saved.get("limit_text")
        account.last_status = saved.get("last_status")
        account.quota_at = saved.get("quota_at", 0.0)
        account.profile_at = saved.get("profile_at", 0.0)
        account.probe_blocked_until = saved.get("probe_blocked_until", 0.0)
        account.email = saved.get("email") or account.email
        account.plan = saved.get("plan") or account.plan
        account.quota = saved.get("quota") or {}
        account.limits = saved.get("limits") or {}
        # `quota` holds values ALREADY interpreted when they were saved, and
        # `locked_reason` is the one that decides whether an account may
        # serve. Restoring it verbatim resurrects the reading of whatever
        # version wrote the file: a build that took `allowed_warning` for a
        # lock left a perfectly usable account marked drained across every
        # later restart, because nothing re-reads it until fresh headers
        # arrive. `limits` is stored raw, so derive the quota again here and
        # let today's rules decide.
        if account.limits:
            account.quota = quota_from_headers(account.limits) or account.quota
    return True


def _keychain_get(alias):
    """macOS Keychain, when the user opted into it. Returns None elsewhere."""
    if sys.platform != "darwin":
        return None
    out = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_PREFIX + alias, "-w"],
        capture_output=True, text=True,
    )
    return out.stdout.strip() if out.returncode == 0 else None


def token_for(alias):
    """The account's long-lived token.

    Looked up in order: the environment (handy for containers), a file under
    ~/.clauxy/tokens/, then the macOS Keychain. Files are the portable default;
    the Keychain is opt-in and macOS only.
    """
    from_env = os.environ.get("CLAUXY_TOKEN_" + alias.upper().replace("-", "_"))
    if from_env:
        return from_env.strip()
    path = os.path.join(TOKENS, f"{alias}.token")
    try:
        with open(path) as fh:
            token = fh.read().strip()
        if token:
            return token
    except OSError:
        pass
    return _keychain_get(alias)


def known_accounts():
    """Every alias with a token, in alphabetical order.

    `CLAUXY_ACCOUNTS` overrides it (comma separated, priority order) — that is
    how a container passes its roster without a writable home.
    """
    from_env = os.environ.get("CLAUXY_ACCOUNTS")
    if from_env:
        return [a.strip() for a in from_env.split(",") if a.strip()]
    try:
        aliases = {f[:-6] for f in os.listdir(TOKENS) if f.endswith(".token")}
    except OSError:
        aliases = set()
    if sys.platform == "darwin":
        out = subprocess.run(["security", "dump-keychain"],
                             capture_output=True, text=True)
        aliases |= set(re.findall(
            r'"' + re.escape(KEYCHAIN_PREFIX) + r'([^"]+)"', out.stdout))
    return sorted(aliases)


class Account:
    """One subscription: its token and whatever is known about its quota."""

    def __init__(self, alias, token):
        self.alias = alias
        self.token = token
        self.cooling_until = 0.0
        self.paused = False       # pausa manual desde el panel
        self.requests = 0
        self.switches_away = 0
        self.limits = {}          # last values the headers reported
        self.last_status = None
        self.tokens = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        self.usd = 0.0
        self.email = None          # de /api/oauth/profile
        self.plan = None
        self.quota = {}            # de /api/oauth/usage
        # The usage endpoint rate-limits on its own, separately from the
        # quota it reports. Asking harder after a 429 only extends it.
        self.probe_blocked_until = 0.0
        self.quota_at = 0.0        # when the reading was taken
        self.profile_at = 0.0      # identity barely changes; ask for it rarely
        self.quota_error = None
        self.session_expired = False
        self.renew_tried = 0.0     # last attempt to renew the sign-in
        self.limit_text = None   # the last 429's message, if any

    def anota(self, model_, usage):
        for clave in self.tokens:
            self.tokens[clave] += usage.get(clave, 0)
        self.usd += cost_usd(model_, usage)

    def _pct(self, which):
        pct, _, _ = self.window(which)
        return pct if pct is not None else 0.0

    def reset_label(self, which):
        """Epoch when that window resets, or infinity when unknown."""
        _, in_s, _ = self.window(which)
        return time.time() + in_s if in_s else float("inf")

    @property
    def usable(self):
        """Can take work without getting close to the wall."""
        return (self.available
                and self._pct("five_hour") < THRESHOLD_5H
                and self._pct("seven_day") < THRESHOLD_7D)

    @property
    def quota_known(self):
        """True when telemetry has actually told us something.

        Not knowing is not the same as having quota. Claiming "available" for
        an account we have never heard from is how work gets sent to a drained
        subscription.
        """
        return any(self.window(w)[0] is not None
                   for w in ("five_hour", "seven_day"))

    def window_stale(self, which):
        """True when the last reading describes a window that already ended.

        Telling this apart from "never heard anything" is what stops the
        panel from greeting a busy account with "shows once it is used".
        """
        block = (self.quota or {}).get(which) or {}
        if block.get("utilization") is None or not block.get("resets_at"):
            return False
        try:
            reset = datetime.fromisoformat(block["resets_at"])
        except ValueError:
            return False
        return reset <= datetime.now(timezone.utc)

    @property
    def out_of_quota(self):
        """Out of quota in either window, according to telemetry."""
        for which in ("five_hour", "seven_day"):
            pct, _, locked = self.window(which)
            if locked is not None or (pct is not None and pct >= 100):
                return True
        return False

    @property
    def available(self):
        return (not self.paused
                and time.time() >= self.cooling_until
                and not self.out_of_quota)

    def window(self, which="five_hour"):
        """Percent used, and seconds until the window resets."""
        block = (self.quota or {}).get(which) or {}
        pct = block.get("utilization")
        in_s = None
        if not block and which == "five_hour" and self.cooling_until:
            # No telemetry yet: all we know is what the limit message said.
            # Report 100%, because being on hold means it ran out.
            return 100.0, self.cooling_for, None
        if block.get("resets_at"):
            try:
                reset = datetime.fromisoformat(block["resets_at"])
                in_s = math.ceil(
                    (reset - datetime.now(timezone.utc)).total_seconds())
            except ValueError:
                pass
        if in_s is not None and in_s <= 0:
            # The window this reading describes has already rolled over, so
            # the percentage is history: it froze accounts at 100% and showed
            # a limit that had already lifted. What replaced it is not known
            # either — the account keeps being used outside this proxy — so
            # the honest answer is "no idea", not "empty".
            return None, None, None

        return pct, in_s, block.get("locked_reason")

    def reset_at(self, which):
        """The reset as an absolute epoch, for display.

        Rebuilding it from the countdown made the minute wobble between page
        refreshes: the countdown is whole seconds, so now + in_s lands either
        side of the true reset.
        """
        block = (self.quota or {}).get(which) or {}
        if self.window(which)[1] is None or not block.get("resets_at"):
            return None
        try:
            return datetime.fromisoformat(block["resets_at"]).timestamp()
        except ValueError:
            return None

    def session_token(self):
        """The token from a regular login, if this folder has one.

        A `setup-token` is for work only: `/api/oauth/usage` answers 403. A
        regular session token does read telemetry, and that is what fills the
        percentage bars. Used ONLY for probing; client requests keep going out
        with the long-lived token, which does not expire.
        """
        oauth = session_credential(self.alias)
        if not oauth:
            return None
        if (oauth.get("expiresAt") or 0) / 1000 <= time.time():
            # Expired is not signed out: the refresh token is still good for
            # weeks. Spend it, at most once every few minutes so a CLI that
            # cannot fix it is not run on every probe.
            if time.time() - self.renew_tried < RENEW_EVERY:
                self.session_expired = True
                return None
            self.renew_tried = time.time()
            oauth = renew_session(self.alias)
            if not oauth:
                self.session_expired = True
                return None
        self.session_expired = False
        return oauth.get("accessToken")

    @property
    def probe_wait(self):
        """Seconds left before the usage endpoint will talk to us again."""
        return max(0, int(self.probe_blocked_until - time.time()))

    def probe_quota(self):
        """Asks Anthropic how much this account has left. Costs no quota.

        It does cost patience: `/api/oauth/usage` has a rate limit of its own.
        While it is refusing, this returns without asking again — a button
        that fires one more 429 is worse than a button that says to wait.
        """
        if self.probe_wait:
            self.quota_error = (
                f"too many quota checks - retrying in {clock(self.probe_wait)}")
            return
        self.session_expired = False
        credential = self.session_token() or self.token
        error = None
        # The profile never really changes, and asking for it on every probe
        # doubled the traffic to an endpoint that rate-limits.
        wanted = [("/api/oauth/usage", "quota")]
        if not self.email or time.time() - self.profile_at > PROFILE_EVERY:
            wanted.append(("/api/oauth/profile", "perfil"))
        for path_, target in wanted:
            req = urllib.request.Request(UPSTREAM + path_)
            req.add_header("Authorization", "Bearer " + credential)
            req.add_header("anthropic-beta", "oauth-2025-04-20")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    item = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                # Not an error: a work token simply cannot read telemetry.
                # Say what to do about it instead of flashing a failure.
                if target == "quota":
                    # A sign-in that has lapsed and a work token that was
                    # never allowed to read telemetry look identical here;
                    # only the first one is news, so name it.
                    if exc.code == 429:
                        # Not the account's quota: the telemetry endpoint
                        # itself. The bars on screen stay valid, they just
                        # stop being refreshed for a while.
                        wait = parse_retry_after(
                            exc.headers.get("retry-after")) if exc.headers \
                            else PROBE_BACKOFF_S
                        wait = max(PROBE_BACKOFF_S, min(wait, MAX_COOLDOWN))
                        self.probe_blocked_until = time.time() + wait
                        error = ("too many quota checks - retrying in "
                                 f"{clock(wait)}")
                    else:
                        error = (f"session expired - run: clauxy login {self.alias}"
                                 if self.session_expired else
                                 f"quota unknown - run: clauxy login {self.alias}"
                                 if exc.code in (401, 403)
                                 else f"could not read quota (HTTP {exc.code})")
                continue
            except Exception as exc:
                if target == "quota":
                    error = f"{type(exc).__name__}"
                continue
            if target == "quota":
                self.quota = item
                self.quota_at = time.time()
                self.probe_blocked_until = 0.0
            else:
                who = item.get("account", {})
                self.email = who.get("email")
                self.plan = ("max" if who.get("has_claude_max")
                             else "pro" if who.get("has_claude_pro") else "?")
                self.profile_at = time.time()
        # Only the usage call decides this: a profile that will not answer
        # says nothing about whether the quota numbers on screen are real.
        self.quota_error = error

    @property
    def cooling_for(self):
        return max(0, int(self.cooling_until - time.time()))

    def cool_down(self, seconds):
        seconds = max(1, min(int(seconds), MAX_COOLDOWN))
        self.cooling_until = time.time() + seconds
        self.switches_away += 1

    def snapshot(self):
        return {
            "alias": self.alias,
            "available_": self.available,
            "paused_": self.paused,
            "out_of_quota": self.out_of_quota,
            "quota_known": self.quota_known,
            "usable": self.usable,
            "waiting_s": self.cooling_for,
            "requests_": self.requests,
            "switches": self.switches_away,
            "last_status_": self.last_status,
            "limits_": self.limits,
            "tokens": dict(self.tokens),
            "usd": round(self.usd, 4),
            "email": self.email or LABELS.get(self.alias),
            "plan": self.plan,
            "window_5h": dict(zip(("pct", "in_s", "locked"), self.window("five_hour")),
                              stale=self.window_stale("five_hour"),
                              at=self.reset_at("five_hour")),
            "window_7d": dict(zip(("pct", "in_s", "locked"), self.window("seven_day")),
                              stale=self.window_stale("seven_day"),
                              at=self.reset_at("seven_day")),
            "quota_error_": self.quota_error,
            "limit_text": self.limit_text,
        }


class Pool:
    def __init__(self, accounts):
        self.accounts = accounts
        self.lock = threading.Lock()
        self.started = time.time()
        self.failovers = 0
        self.audit = collections.deque(maxlen=AUDIT_MAX)
        self.active = None        # alias of the account that served last
        self.only = None          # when set, no switching (checks only)
        self.dirty = False        # unsaved changes
        self.events = collections.deque(maxlen=EVENTS_MAX)
        self.last_switch = 0.0
        self.mode = "sequential"  # "sequential" | "sticky"
        self.by_token = {a.token: a for a in accounts}

    def account_for_token(self, cabecera):
        """The account the client's own token maps to, when we recognise it."""
        if not cabecera:
            return None
        return self.by_token.get(cabecera.split(" ", 1)[-1].strip())

    def log_request(self, entry):
        with self.lock:
            self.audit.appendleft(entry)
        self.dirty = True          # the saver thread will pick it up

    def move(self, alias, delta):
        """Moves an account up or down the priority order."""
        with self.lock:
            for i, a in enumerate(self.accounts):
                if a.alias == alias:
                    j = max(0, min(len(self.accounts) - 1, i + delta))
                    self.accounts.insert(j, self.accounts.pop(i))
                    return True
        return False

    def find(self, alias):
        for a in self.accounts:
            if a.alias == alias:
                return a
        return None

    def pick(self):
        """The best account for the work coming next.

        Main rule, borrowed from claude-rotate: among usable accounts, the one
        whose WEEKLY window resets soonest. That quota is perishable — whatever
        you do not spend before the reset is lost — so burn the one that
        expires first. Ties go to the least-used hour.

        If none is usable, the one that comes back soonest.
        """
        candidates = [a for a in self.accounts if a.usable]
        if candidates:
            return min(candidates, key=lambda a: (a.reset_label("seven_day"),
                                              a._pct("five_hour")))
        # Nobody is comfortably below the thresholds. An account that is over
        # them but can still serve beats one that is actually drained: going
        # by "whose hour resets soonest" alone sent the work to the account on
        # cooldown, which answers 429 and bounces straight back.
        serving = [a for a in self.accounts if a.available]
        if serving:
            return min(serving, key=lambda a: (a.reset_label("seven_day"),
                                               a._pct("five_hour")))
        alive = [a for a in self.accounts if not a.paused] or list(self.accounts)
        return min(alive, key=lambda a: a.reset_label("five_hour"))

    def switch_reason(self, current, best):
        """Why we should leave `current` for `best`, or None to stay put.

        Switching accounts throws away the prompt cache, which is the
        expensive part: that is why we do not jump to the best account on
        every request, only when there is a clear reason.
        """
        if current is None:
            return "startup"
        if best is current:
            return None
        if not current.available:
            # Only worth moving if the one we would move TO can actually
            # serve. When every account is dry this used to fire on every
            # single request, and since the fallback pick keeps changing its
            # mind the proxy spent its time hopping between drained accounts
            # — a hundred switches, not one of them able to answer. Staying
            # put costs nothing and keeps the log readable.
            return "out of quota" if best.available else None
        if current.paused:
            return "paused_ a mano"
        # What follows honours the cooldown: these are improvements, not emergencies.
        if time.time() - self.last_switch < COOLDOWN_S:
            return None
        if (current._pct("five_hour") >= THRESHOLD_5H
                and best._pct("five_hour") <= current._pct("five_hour") - SWITCH_MARGIN):
            return f"5h >= {THRESHOLD_5H:.0f}%"
        if best.reset_label("seven_day") < current.reset_label("seven_day") - PERISHABLE_MARGIN_S:
            return "la otra caduca antes"
        return None

    #: What the panel's buttons may ask for. The form in `render_panel`
    #: posts these exact strings; keeping the list here is what lets a test
    #: catch the two drifting apart again.
    CONTROLS = ("pause", "resume", "up", "down", "probe")

    def apply_control(self, alias, action):
        """Act on one of the panel buttons. True when something happened."""
        account = self.find(alias)
        if not account or action not in self.CONTROLS:
            return False
        if action == "pause":
            account.paused = True
        elif action == "resume":
            account.paused = False
            account.cooling_until = 0.0
        elif action == "up":
            self.move(alias, -1)
        elif action == "down":
            self.move(alias, +1)
        elif action == "probe":
            account.probe_quota()
        self.dirty = True
        return True

    def record_switch(self, frm, to, reason):
        self.last_switch = time.time()
        # Every departure counts, not only the ones a 429 forced. Leaving an
        # account because its hour filled up or because another one's quota
        # expires sooner is still leaving it, and the panel promised a count.
        # Startup has no `frm`: arriving somewhere is not a switch.
        if frm is not None:
            frm.switches_away += 1
            self.failovers += 1
        self.events.appendleft({
            "time": time.strftime("%H:%M:%S"),
            # The table outlives restarts now, so a bare clock would show
            # yesterday's switch as if it had just happened.
            "ts": time.time(),
            "from": frm.alias if frm else None,
            "to": to.alias,
            "reason": reason,
        })
        self.dirty = True
        sys.stderr.write(
            f"[clauxy] account switch: "
            f"{frm.alias if frm else '-'} -> {to.alias} ({reason})\n")

    def order(self, preferred=None):
        """Accounts in serving order, the client's own first when it can serve.

        `preferred` is the account the client's own `CLAUDE_CODE_OAUTH_TOKEN`
        maps to. While it can serve it goes first, so in `sticky` mode each
        client stays on its own account — and keeps its warm cache. Only when
        that account runs out do we fall back to the others.

        An account whose window is at 100% counts as unavailable even before it
        returns a 429, so no turn is wasted finding out.
        """
        if self.only is not None:
            return [self.only]
        if self.mode == "sequential":
            # One account at a time, chosen by how soon its quota expires.
            # Whatever token the client sent is ignored on purpose.
            # The current account is evaluated as-is: blanking it when it is
            # no longer usable lost the real reason and skipped the cooldown.
            current = self.find(self.active)
            best = self.pick()
            reason = self.switch_reason(current, best)
            if reason:
                self.record_switch(current, best, reason)
                self.active = best.alias
                head = best
            else:
                head = current or best
            rest = sorted((a for a in self.accounts if a is not head),
                           key=lambda a: (not a.usable, a.reset_label("seven_day")))
            return [head] + rest
        with self.lock:
            # `available` already checks both windows: an account with a free
            # hour and an exhausted WEEKLY limit is just as useless.
            free = [a for a in self.accounts if a.available]
            busy = sorted(
                (a for a in self.accounts if a not in free),
                key=lambda a: a.cooling_until,
            )
            if preferred is not None and preferred in free:
                free.remove(preferred)
                free.insert(0, preferred)
        # Accounts on hold go last: if every one is, try the one with the
        # shortest wait rather than hand the client a 429.
        return free + busy

    def snapshot(self):
        queue = [a.alias for a in self.order()]   # outside the lock: order() takes it
        with self.lock:
            return {
                "started_s_ago": int(time.time() - self.started),
                "switches": self.failovers,
                "accounts": [a.snapshot() for a in self.accounts],
                "active": self.active,
                "mode": self.mode,
                # The real serving order right now, so the panel can name who
                # takes the next request even before any has arrived.
                "queue": queue,
                # Paging is served from here, so keep more than one page.
                # Only the rows actually on screen reach the browser.
                "events": list(self.events),
                "thresholds": {"five_hour": THRESHOLD_5H, "weekly": THRESHOLD_7D},
                "usd_total": round(sum(a.usd for a in self.accounts), 4),
                "requests__total": sum(a.requests for a in self.accounts),
                "audit": list(self.audit)[:200],
            }


# "resets Nov 5 at 12pm", "resets at 3pm", "resets Nov 5 at 12:30pm"
LIMITE_RE = re.compile(
    r"resets?\s+(?:(?P<mes>[A-Z][a-z]{2})\s+(?P<dia>\d{1,2})\s+)?at\s+"
    r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)",
    re.IGNORECASE)
MESES = {m: i + 1 for i, m in enumerate(
    "ene feb mar abr may jun jul ago sep oct nov dic".split())}
MESES.update({m: i + 1 for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split())})


def limit_message(texto):
    """The sentence a 429 is trying to say, without the JSON around it.

    The body arrives as `{"type":"error","error":{"type":"rate_limit_error",
    "message":"This request would exceed..."}}`. What a person needs is the
    message; the envelope only pushes it out of the panel's one line.
    """
    try:
        error = json.loads(texto or "{}").get("error", {})
        if isinstance(error, dict) and error.get("message"):
            return error["message"]
    except (ValueError, AttributeError, TypeError):
        pass
    return texto or ""


def is_quota_429(texto):
    """Tells a spent-quota 429 from a "you are not the CLI" 429.

    The API answers 429 to any client it does not recognise as the CLI, with a
    generic body: `"message":"Error"`, no detail. ANY account would answer the
    same, so neither switching nor holding helps — and marking them drained
    knocks the whole pool out at once. A single health check would do it.

    A real quota 429 carries the reason: "You've hit your weekly limit",
    "resets Nov 5 at 12pm", "usage limit"...
    """
    # Look ONLY at the message, never the whole body: the error *type* is
    # `rate_limit_error`, which contains "limit" and would make every 429 look
    # like spent quota.
    bajo = limit_message(texto).strip().lower()
    if bajo in ("", "error"):
        return False
    return any(x in bajo for x in ("limit", "resets", "quota", "usage", "upgrade"))


def seconds_until_reset(texto):
    """Reads 'resets Nov 5 at 12pm' out of a limit message.

    When telemetry is unavailable this is the only source for when quota comes
    back. Returns seconds, or None when the text does not say.
    """
    m = LIMITE_RE.search(texto or "")
    if not m:
        return None
    now = datetime.now().astimezone()
    hour_ = int(m.group("hour")) % 12
    if m.group("ampm").lower() == "pm":
        hour_ += 12
    target = now.replace(hour=hour_, minute=int(m.group("min") or 0),
                            second=0, microsecond=0)
    if m.group("mes"):
        mes = MESES.get(m.group("mes").lower())
        if mes:
            # The message carries no year. Picking the candidate closest to
            # now is what makes 'Jan 2' read as next year on December 31 and
            # 'Dec 31' as last year on January 2 — while a date that merely
            # passed a few hours ago stays in the past, where it belongs.
            # Rolling it a whole year forward parked the account for days.
            best = None
            for year in (now.year - 1, now.year, now.year + 1):
                try:
                    cand = target.replace(year=year, month=mes,
                                          day=int(m.group("dia")))
                except ValueError:
                    continue            # Feb 29 of a year that has no Feb 29
                if best is None or abs(cand - now) < abs(best - now):
                    best = cand
            if best is not None:
                target = best
    elif target <= now:                        # time only: means tomorrow
        target += timedelta(days=1)
    return max(0, int((target - now).total_seconds()))


def parse_retry_after(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN


def with_cli_system(payload):
    """Puts the CLI banner first in `system`, so a subscription token is accepted.

    A subscription token only works for Claude Code, and the upstream decides
    who is calling by looking at the system prompt: its first block has to be
    exactly CLI_SYSTEM. Anything else — no system at all, the sentence with
    more text glued behind it, the sentence in the second block — answers 429
    `rate_limit_error` with a bare "Error", which reads like spent quota and is
    not. Measured on eight requests that differed only in `system`, against an
    account at 0% of its window.

    What makes this safe for other clients is that the banner only has to be
    the FIRST block, alone in it: the caller's own prompt rides along intact in
    the blocks behind it. So a code reviewer keeps its review prompt and still
    gets served.

    Returns the payload to send (the same object when nothing was missing).
    """
    system = payload.get("system")
    block = {"type": "text", "text": CLI_SYSTEM}
    if isinstance(system, str):
        if system.startswith(CLI_SYSTEM):
            # Already the banner, or the banner with text glued behind it. The
            # second form is rejected upstream, so split it into two blocks.
            rest = system[len(CLI_SYSTEM):].strip()
            if not rest:
                return payload
            payload["system"] = [block, {"type": "text", "text": rest}]
        else:
            payload["system"] = [block, {"type": "text", "text": system}]
    elif isinstance(system, list):
        first = system[0] if system else None
        if (isinstance(first, dict) and first.get("type") == "text"
                and (first.get("text") or "").strip() == CLI_SYSTEM):
            return payload
        payload["system"] = [block] + system
    else:
        # None, or something we do not understand: the banner on its own.
        payload["system"] = [block]
    return payload


def collect_limits(headers):
    """Pulls out the rate-limit headers Anthropic sends."""
    out = {}
    for key, value in headers.items():
        low = key.lower()
        if low.startswith("anthropic-ratelimit-"):
            out[low[len("anthropic-ratelimit-"):]] = value
    return out


def quota_from_headers(limits_):
    """Converts the `unified-*` headers into the /api/oauth/usage shape.

    Anthropic sends quota on EVERY response:

        unified-5h-utilization: 0.0     unified-5h-reset: 1793844000
        unified-7d-utilization: 0.28    unified-7d-reset: 1794114000
        unified-5h-status: allowed      unified-7d-status: allowed

    This is the best source there is: it costs no extra request and needs no
    session credential that can expire. `/api/oauth/usage` is only needed for
    an account that has not served anything yet, because without traffic there
    are no headers. Values are fractions, and the reset is a unix epoch.
    """
    out = {}
    for clave, target in (("5h", "five_hour"), ("7d", "seven_day")):
        usage = limits_.get(f"unified-{clave}-utilization")
        if usage is None:
            continue
        block = {}
        try:
            # Rounded: 0.28 * 100 is 28.000000000000004 in floating point.
            block["utilization"] = round(float(usage) * 100, 1)
        except (TypeError, ValueError):
            continue
        resets_at = limits_.get(f"unified-{clave}-reset")
        if resets_at:
            try:
                block["resets_at"] = datetime.fromtimestamp(
                    int(resets_at), timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                pass
        estado = limits_.get(f"unified-{clave}-status")
        # Only a status that actually REFUSES work is a lock. Anthropic also
        # sends `allowed_warning` as the window fills up, and reading that as
        # "blocked" took a perfectly good account out of the rotation at ~90%
        # - leaving the proxy bouncing between accounts that really were
        # drained. Anything in the `allowed*` family can still serve.
        estado_s = str(estado).strip().lower() if estado is not None else ""
        block["locked_reason"] = (
            None if not estado_s or estado_s.startswith("allowed") else estado)
        out[target] = block
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    pool = None
    verbose = False

    def handle_error(self, request, client_address):
        # The default prints the traceback and carries on; make it explicit
        # so it lands in the log with a timestamp.
        sys.stderr.write(f"[proxy] error sirviendo a {client_address}:\n")
        traceback.print_exc()
        sys.stderr.flush()

    def log_message(self, fmt, *args):
        if self.verbose:
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    # -- panel and controls --------------------------------------------------

    def _envia(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _panel(self, as_json, query=None):
        snap = self.pool.snapshot()
        if as_json:
            self._envia(json.dumps(snap, indent=2).encode(),
                        "application/json; charset=utf-8")
            return

        def page_of(name):
            try:
                return max(0, int((query or {}).get(name, ["0"])[0]))
            except (TypeError, ValueError):
                return 0

        # Which slice to render is the client's to say. The page asks for the
        # one it is showing, so the rows it is not showing are never built.
        body = render_panel(snap, sw_page=page_of("sw"), log_page=page_of("log"))
        self._envia(body.encode(), "text/html; charset=utf-8")

    def _control(self):
        """Panel buttons: pause, resume, raise and lower priority."""
        n = int(self.headers.get("Content-Length") or 0)
        campos = urllib.parse.parse_qs(self.rfile.read(n).decode())
        # These names are the ones the form posts. They used to be read under
        # Spanish keys, so every button silently did nothing but redirect.
        self.pool.apply_control((campos.get("alias") or [""])[0],
                                (campos.get("action") or [""])[0])
        self.send_response(303)
        self.send_header("Location", "/_status")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- proxy ---------------------------------------------------------------

    def _handle(self, method):
        # A bare http://host:port/ is what anyone tries first: send it to the
        # panel instead of forwarding it upstream as a bad request.
        if self.path in ("/", "/_estado", "/_estado.json") and method == "GET":
            target = "/_status.json" if self.path.endswith(".json") else "/_status"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # The browser asks for this the moment you open the panel. Forwarding
        # it upstream earned a 404 per page load and filled the request log.
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        route, _, query = self.path.partition("?")
        if route in ("/_status", "/_status.json"):
            self._panel(route.endswith(".json"),
                        urllib.parse.parse_qs(query))
            return
        if self.path == "/_control" and method == "POST":
            self._control()
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        requested_model = None
        if body:
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict):
                requested_model = payload.get("model")
                if (method == "POST"
                        and route.startswith(SYSTEM_PATHS)
                        and "messages" in payload):
                    before = payload.get("system")
                    if with_cli_system(payload).get("system") is not before:
                        body = json.dumps(payload).encode()

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP
        }

        started_at = time.time()
        tried = []
        # The account the client brought wins, while it can still serve.
        preferred = self.pool.account_for_token(self.headers.get("Authorization"))
        for account in self.pool.order(preferred):
            status, resp_headers, stream, error = self._attempt(
                method, headers, body, account,
            )
            if error is not None:
                tried.append(f"{account.alias}:{error}")
                continue

            account.last_status = status
            limits_ = collect_limits(resp_headers)
            if limits_:
                account.limits = limits_
                # Header quota is fresher than any probe.
                account.quota.update(quota_from_headers(limits_))
                account.quota_error = None

            if status in RETRYABLE and len(tried) < len(self.pool.accounts) - 1:
                # If we know when the window resets, wait for that; otherwise
                # fall back to retry-after.
                body_seen = ""
                try:
                    body_seen = stream.read(2048).decode(errors="replace")
                except Exception:
                    pass

                if not is_quota_429(body_seen):
                    # Not quota: the API is rejecting the caller. Every account
                    # would answer the same, so pass it through without
                    # touching pool state.
                    sys.stderr.write(
                        f"[proxy] {account.alias} dio {status} generico "
                        f"(client not recognised): not quota, not switching\n")
                    with self.pool.lock:
                        self.pool.active = account.alias
                    self._relay(status, resp_headers, stream, account,
                                requested_model, started_at, False, preferred,
                                prefetched=body_seen.encode(errors="replace"))
                    return

                # Store the sentence, not the envelope: the panel shows this.
                account.limit_text = (limit_message(body_seen)[:300]
                                      or account.limit_text)
                _, in_s, _ = account.window("five_hour")
                wait_s = (seconds_until_reset(body_seen)
                          or in_s
                          or parse_retry_after(resp_headers.get("retry-after")))
                account.cool_down(wait_s)
                wait_s = account.cooling_for      # capped by now
                with self.pool.lock:
                    self.pool.failovers += 1
                stream.close()
                tried.append(f"{account.alias}:{status}")
                self.pool.log_request({
                    "time": time.strftime("%H:%M:%S"),
                    "path": self.path,
                    "model": requested_model, "account": account.alias,
                    "status": status, "switched": True, "own": True,
                    "ms": int((time.time() - started_at) * 1000),
                    "tokens": None, "usd": None,
                })
                sys.stderr.write(
                    f"[proxy] {account.alias} dio {status}: en wait_s "
                    f"{int(wait_s)}s -> switching to the next account\n"
                )
                threading.Thread(target=account.probe_quota, daemon=True).start()
                continue

            account.requests += 1
            with self.pool.lock:
                self.pool.active = account.alias
            self._relay(status, resp_headers, stream, account,
                        requested_model, started_at, bool(tried), preferred)
            return

        self.send_error(502, "no account answered: " + ", ".join(tried))

    def _attempt(self, method, headers, body, account):
        """One upstream request. Returns (status, headers, stream, error)."""
        request = urllib.request.Request(
            UPSTREAM + self.path, data=body, method=method,
        )
        for key, value in headers.items():
            request.add_header(key, value)
        request.add_header("Authorization", "Bearer " + account.token)
        request.add_header("Host", "api.anthropic.com")
        try:
            resp = urllib.request.urlopen(request, timeout=900)
            return resp.status, resp.headers, resp, None
        except urllib.error.HTTPError as exc:
            # An HTTPError is still a response with a body: this is how the
            # 429 arrives.
            return exc.code, exc.headers, exc, None
        except Exception as exc:                     # red, DNS, timeout
            return None, None, None, type(exc).__name__

    def _relay(self, status, resp_headers, stream, account,
               requested_model, started_at, switched_, preferred=None, prefetched=b""):
        """Streams the response through, counting tokens on the way."""
        self.send_response(status)
        for key, value in resp_headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        seen = bytearray()
        try:
            if prefetched:
                # Whatever was read to diagnose the 429 still has to be sent:
                # the client expects the whole body.
                self.wfile.write(b"%x\r\n%s\r\n" % (len(prefetched), prefetched))
                seen.extend(prefetched)
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                # The client comes first; accounting trails it and never blocks it.
                if len(seen) < 262144:
                    seen.extend(chunk)
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                                     # el agente corto; normal
        finally:
            stream.close()

        model_, usage = usage_from_body(
            decompress(bytes(seen), resp_headers.get("content-encoding")))
        model_ = model_ or requested_model
        usd = cost_usd(model_, usage)
        account.anota(model_, usage)
        self.pool.log_request({
            "time": time.strftime("%H:%M:%S"),
            # The path is what makes a 404 or a 400 diagnosable at all.
            "path": self.path,
            "model": model_, "account": account.alias,
            "status": status, "switched": switched_,
            "own": preferred is None or account is preferred,
            "ms": int((time.time() - started_at) * 1000),
            "tokens": usage, "usd": round(usd, 5),
        })

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")


def clock(seconds):
    """3h 14m / 14m 02s / --."""
    if seconds is None:
        return "--"
    h, rest = divmod(int(seconds), 3600)
    m, sec = divmod(rest, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"


def thousands(n):
    return f"{n:,}"


def reset_label(window):
    """'· resets at 10:12 PM', in the viewer's own clock.

    The headline above already says how long is left, so repeating the
    countdown here wastes the line. A wall-clock time answers the question you
    actually have: can I still get work in before it resets?

    A window with no usage carries no `resets_at`: there is nothing to reset.
    Printing 'resets at --' there looks like missing data, and it is not.
    """
    if not window.get("in_s"):
        return "· unused" if not window.get("pct") else ""
    when = (datetime.fromtimestamp(window["at"]).astimezone()
            if window.get("at") else
            datetime.now().astimezone() + timedelta(seconds=window["in_s"]))
    # These resets land on 12:59:59, a second short of the round hour.
    # Truncating the seconds away printed "12:59", which reads as the wrong
    # hour; rounding to the nearest minute prints the hour people expect.
    when = (when + timedelta(seconds=30)).replace(second=0, microsecond=0)
    hora = when.strftime("%I:%M %p").lstrip("0")
    if when.date() != datetime.now().astimezone().date():
        return f"· resets {when.strftime('%b %-d')}, {hora}"
    return f"· resets at {hora}"


def empty_label(window, no_telemetry):
    """What to write where the percentage would go, when there is none.

    "Shows once it is used" is only true of an account nobody has touched.
    Saying it about one whose window just rolled over reads as 0%, which is a
    claim the proxy cannot make: the account is being spent, the reading it
    had has expired, and nothing has refreshed it.
    """
    if window.get("stale"):
        return "unknown since it reset"
    return "shows once it is used" if no_telemetry else "no data"


def bar(pct, label, extra="", empty="no data"):
    if pct is None:
        return (f'<div class="bar"><span class="lbl">{label}</span>'
                f'<div class="track empty"><i style="width:0%"></i></div>'
                f'<span class="val">{empty}</span></div>')
    cls = "high" if pct >= 90 else "mid" if pct >= 70 else ""
    return (f'<div class="bar {cls}"><span class="lbl">{label}</span>'
            f'<div class="track"><i style="width:{min(100, pct):.1f}%"></i></div>'
            f'<span class="val">{pct:.0f}% {extra}</span></div>')


def money(usd):
    """A dollar amount a person can read at a glance.

    Totals are money, so they get the two decimals money has: "$5.31", not
    "$5.3123". Single requests cost fractions of a cent, and rounding those to
    two decimals turns the whole log into a column of "$0.02", so small
    amounts keep enough digits to stay distinguishable.
    """
    if usd is None:
        return "—"
    if usd == 0:
        return "$0.00"
    if abs(usd) >= 1:
        return f"${usd:,.2f}"
    if abs(usd) >= 0.01:
        return f"${usd:.3f}"
    return f"${usd:.5f}"


def switch_when(event):
    """The clock for today's switches, the date as well for older ones.

    Entries survive restarts, so "16:14:50" on its own would pass off a switch
    from two days ago as something that just happened.
    """
    stamp = event.get("ts")
    if not stamp:
        return event.get("time", "—")      # saved before `ts` existed
    when = datetime.fromtimestamp(stamp)
    if when.date() == datetime.now().date():
        return when.strftime("%H:%M:%S")
    return when.strftime("%b %-d %H:%M:%S")


def pager_html(name, page, total, per):
    """The ‹ newer / older › control under a table, rendered server side.

    The buttons carry the page they would load; the script turns that into
    the next fetch. Rendering it here is what lets the page ask for one
    slice instead of being handed every row and hiding most of them.
    """
    pages = max(1, math.ceil(total / per))
    if pages <= 1:
        return ""
    first = page * per + 1
    last = min((page + 1) * per, total)
    def button(label, to, disabled):
        flag = " disabled" if disabled else ""
        return (f'<button data-page="{name}:{to}"{flag}>{label}</button>')
    return (f'<div class=pager>'
            + button("‹ newer", page - 1, page == 0)
            + f"<span>{first}-{last} of {total}</span>"
            + button("older ›", page + 1, page >= pages - 1)
            + "</div>")


def render_panel(snap, sw_page=0, log_page=0):
    queue = snap.get("queue") or [c["alias"] for c in snap["accounts"] if c["available_"]]
    # Before the first request there is no "active" account: show the one that
    # will serve, which is what you want to know when you open the panel.
    active = snap["active"] or (queue[0] if queue else None)
    # Rendering the panel picks an account, so `active` alone would claim
    # traffic that never happened. Real requests are the signal.
    started = snap["requests__total"] > 0
    # Who actually takes over, not merely who is next in the list: in
    # sequential mode the queue holds every account, drained ones included.
    free = {c["alias"] for c in snap["accounts"] if c["available_"]}
    nxt = next((a for a in queue if a != active and a in free),
               next((a for a in queue if a != active), "none free"))

    # The panel does not assume three accounts: it adapts to whatever is loaded.
    total = len(snap["accounts"])
    drained = sum(1 for c in snap["accounts"] if c["waiting_s"] or c.get("out_of_quota"))
    paused = sum(1 for c in snap["accounts"] if c["paused_"])
    unknown = sum(1 for c in snap["accounts"]
                  if not c.get("quota_known") and not c["waiting_s"]
                  and not c["paused_"] and not c.get("out_of_quota"))
    parts = [f"{total - drained - paused - unknown} with quota"]
    if unknown:
        parts.append(f"{unknown} unknown")
    if drained:
        parts.append(f"{drained} drained")
    if paused:
        parts.append(f"{paused} paused")
    summary = " · ".join(parts)

    requests_on_active = next((c["requests_"] for c in snap["accounts"]
                               if c["alias"] == active), 0)
    active_alias = active          # the loop below uses `is_active`

    cards = []
    no_telemetry = all(c["quota_error_"] for c in snap["accounts"]) if snap["accounts"] else False
    for i, c in enumerate(snap["accounts"]):
        w5, w7 = c["window_5h"], c["window_7d"]
        is_active = c["alias"] == active_alias

        week_gone = (w7["pct"] or 0) >= 100
        if c["paused_"]:
            state, cls = "PAUSED", "off"
        elif week_gone:
            state, cls = "WEEKLY LIMIT REACHED", "wait"
        elif c["waiting_s"] or c.get("out_of_quota") or (w5["pct"] or 0) >= 100:
            # The header counts `out_of_quota` as drained; a card that says
            # "ready" for the same account contradicts it on the same screen.
            state, cls = "DRAINED", "wait"
        elif not c.get("quota_known") or w5.get("stale") or w7.get("stale"):
            state, cls = "QUOTA UNKNOWN", "idle"
        elif is_active:
            state, cls = "IN USE", "on"
        else:
            state, cls = "ready", "idle"

        # What you look at first: when this account comes back.
        if week_gone:
            headline = ('<div class="reset wait">Weekly limit reached'
                        + (f' · back in <b>{clock(w7["in_s"])}</b>'
                           if w7["in_s"] else "") + "</div>")
        elif c["waiting_s"]:
            headline = (f'<div class="reset wait">Back in '
                        f'<b>{clock(c["waiting_s"])}</b>'
                        + (f'<small>{esc(limit_message(c["limit_text"])[:160])}'
                           '</small>' if c.get("limit_text") else "")
                        + "</div>")
        elif w5["in_s"] and (w5["pct"] or 0) > 0:
            headline = (f'<div class="reset">5-hour window at '
                        f'<b>{w5["pct"]:.0f}%</b>, resets in '
                        f'<b>{clock(w5["in_s"])}</b></div>')
        elif w5.get("stale") or w7.get("stale"):
            headline = ('<div class="reset unknown">Quota reading has expired'
                        '<small>A window reset since the last reading, and '
                        'nothing has refreshed it: the account keeps being '
                        'spent elsewhere. Send a request through clauxy, or '
                        f'run <code>clauxy login {c["alias"]}</code>, to read '
                        'it again.</small></div>')
        elif not c.get("quota_known"):
            headline = ('<div class="reset unknown">Quota not known yet'
                        '<small>No telemetry for this account yet. It stays in '
                        'the rotation, so it will be tried - and may turn out '
                        'to be drained.</small></div>')
        else:
            headline = '<div class="reset ok">Quota available</div>'

        tk = c["tokens"]
        total_tk = tk["input"] + tk["output"] + tk["cache_write"] + tk["cache_read"]
        cards.append(f"""
<article class="card {'active' if is_active else ''}">
  <header>
    <div>
      <h2>#{i + 1} · {c['alias']}{f" <span class='plan'>{c['plan']}</span>" if c['plan'] else ""}</h2>
      <p class="mail">{c['email'] or '— no label: clauxy label ' + c['alias'] + ' you@example.com'}</p>
    </div>
    <span class="state {cls}">{state}</span>
  </header>

  {headline}

  {bar(w5['pct'], '5-hour', reset_label(w5), empty_label(w5, no_telemetry))}
  {bar(w7['pct'], 'Weekly', reset_label(w7), empty_label(w7, no_telemetry))}

  <dl>
    <div><dt>Spend</dt><dd class="big">{money(c['usd'])}</dd></div>
    <div><dt>Requests</dt><dd class="big">{c['requests_']}</dd></div>
    <div><dt>Tokens</dt><dd class="big">{thousands(total_tk)}</dd></div>
    <div><dt>Switches</dt><dd class="big">{c['switches']}</dd></div>
  </dl>
  <p class="detail">in {thousands(tk['input'])} · out {thousands(tk['output'])}
     · cache write {thousands(tk['cache_write'])} · cache read {thousands(tk['cache_read'])}
     {'· <b class="' + ('hintful' if 'clauxy login' in c['quota_error_'] else 'err')
       + '">' + c['quota_error_'] + '</b>' if c['quota_error_'] else ''}</p>

  <form method="post" action="/_control" class="actions">
    <input type="hidden" name="alias" value="{c['alias']}">
    <button name="action" value="{'resume' if c['paused_'] or c['waiting_s'] else 'pause'}">
      {'Resume' if c['paused_'] or c['waiting_s'] else 'Pause'}</button>
    <button name="action" value="up" {'disabled' if i == 0 else ''}>Up</button>
    <button name="action" value="down" {'disabled' if i == len(snap['accounts']) - 1 else ''}>Down</button>
    <button name="action" value="probe">Refresh quota</button>
  </form>
</article>""")

    ev = snap.get("events") or []
    # A startup is not a switch: nothing was left, so FROM is empty and the
    # row looked broken. Say what it actually is.
    def _row(e):
        start = not e["from"]
        frm = '<i class=hint>proxy started</i>' if start else e["from"]
        reason = "now serving this account" if start else e["reason"]
        return (f'<tr class="{"muted" if start else ""}"><td>{switch_when(e)}</td>'
                f'<td>{frm}</td><td><b>{e["to"]}</b></td><td>{reason}</td></tr>')

    real = [e for e in ev if e["from"]]
    note = ("" if real else
            "<p class=none>No switches yet — the rows below are restarts. "
            "A switch is recorded when one account hands over to another.</p>")
    sw_page = min(sw_page, max(0, (len(ev) - 1) // SWITCH_PAGE))
    shown = ev[sw_page * SWITCH_PAGE:(sw_page + 1) * SWITCH_PAGE]
    switches_html = ("<h3>Account switches</h3>" + note + "<table>"
                     "<tr><th>Time</th><th>From</th><th>To</th><th>Reason</th></tr>"
                     + "".join(_row(e) for e in shown)
                     + "</table>"
                     + pager_html("sw", sw_page, len(ev), SWITCH_PAGE)) if ev else ""

    audit = snap["audit"]
    log_page = min(log_page, max(0, (len(audit) - 1) // LOG_PAGE))
    rows = []
    for e in audit[log_page * LOG_PAGE:(log_page + 1) * LOG_PAGE]:
        tk = e["tokens"]
        # Cache writes bill at 1.25x input and are usually most of the cost:
        # hiding them made the price look wrong next to the token counts.
        detail = (f"{thousands(tk['input'])} in · {thousands(tk['cache_read'])} cr · "
                  f"{thousands(tk['cache_write'])} cw / {thousands(tk['output'])} out"
                  if tk else "—")
        cls = "err" if e["status"] != 200 else ""
        mark = ' <b class="sw">switched</b>' if e["switched"] else ""
        if not e.get("own", True):
            mark += ' <b class="sw">borrowed</b>'
        rows.append(
            f'<tr class="{cls}"><td>{e["time"]}</td>'
            f'<td>{e["model"] or e.get("path") or "?"}</td>'
            f'<td><b>{e["account"]}</b>{mark}</td><td>{e["status"]}</td>'
            f'<td>{detail}</td>'
            f'<td>{money(e["usd"])}</td>'
            f'<td>{e["ms"]} ms</td></tr>'
        )

    commands = """<dialog id="cmds">
<form method="dialog"><button class="close" aria-label="Close">&times;</button></form>
<h3>Commands</h3>
<table class="cmds">
<tr><td><code>clauxy status</code></td><td>is it up? which account is serving?</td></tr>
<tr><td><code>clauxy start</code> / <code>stop</code></td><td>run the proxy by hand</td></tr>
<tr><td><code>clauxy restart</code></td><td>pick up new code, service or not</td></tr>
<tr><td><code>clauxy service install</code></td><td>start on login, restart on crash</td></tr>
<tr><td><code>clauxy add &lt;alias&gt;</code></td><td>add an account</td></tr>
<tr><td><code>clauxy label acct-1 you@example.com</code></td><td>set the label shown above</td></tr>
<tr><td><code>clauxy list</code></td><td>accounts and labels</td></tr>
<tr><td><code>clauxy check</code></td><td>try every account with the real CLI</td></tr>
<tr><td><code>tail -f ~/.clauxy/clauxy.log</code></td><td>the log, when something dies</td></tr>
</table>
<p class="foot"><b>Why are the bars empty?</b> Quota is read from the
<code>anthropic-ratelimit-unified-*</code> headers of each response, so an
account that has not served anything yet has no numbers. Run
<code>clauxy login &lt;alias&gt;</code> to let clauxy query quota directly for
idle accounts too.</p>
</dialog>"""

    return f"""<!doctype html><html lang=en><meta charset=utf-8>
<title>clauxy</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#14161a;--mut:#6b7280;--line:#e5e7eb;
--ok:#16a34a;--warn:#d97706;--err:#dc2626;--fill:#2563eb;--act:#2563eb}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0d0f12;--card:#16191e;--fg:#e8eaed;
--mut:#9aa0a6;--line:#2a2e35;--fill:#60a5fa;--act:#60a5fa}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px 16px 48px;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,system-ui,sans-serif;max-width:860px;margin-inline:auto}}
h1{{font-size:19px;margin:0}}
.top{{display:flex;justify-content:space-between;align-items:baseline;
flex-wrap:wrap;gap:8px;margin-bottom:4px}}
.sub{{color:var(--mut);font-size:13px;margin:0 0 20px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px;margin-bottom:14px}}
.card.active{{border-color:var(--act);box-shadow:0 0 0 3px color-mix(in srgb,var(--act) 15%,transparent)}}
.card header{{display:flex;justify-content:space-between;align-items:flex-start;
gap:12px;margin-bottom:14px}}
h2{{font-size:15px;margin:0}}
.plan{{font-size:10px;text-transform:uppercase;letter-spacing:.06em;
background:var(--line);padding:2px 6px;border-radius:4px;vertical-align:2px}}
.mail{{color:var(--mut);font-size:12px;margin:2px 0 0}}
.state{{font-size:11px;font-weight:600;letter-spacing:.04em;padding:4px 9px;
border-radius:999px;white-space:nowrap}}
.state.on{{background:color-mix(in srgb,var(--ok) 15%,transparent);color:var(--ok)}}
.state.wait{{background:color-mix(in srgb,var(--warn) 15%,transparent);color:var(--warn)}}
.state.off{{background:color-mix(in srgb,var(--err) 15%,transparent);color:var(--err)}}
.state.idle{{background:var(--line);color:var(--mut)}}
.bar{{display:grid;grid-template-columns:86px 1fr auto;gap:10px;
align-items:center;margin:7px 0}}
.lbl{{color:var(--mut);font-size:12px}}
.track{{background:var(--line);border-radius:5px;height:9px;overflow:hidden}}
.track i{{display:block;height:100%;background:var(--fill);transition:width .4s}}
.track.empty{{background:repeating-linear-gradient(45deg,var(--line),var(--line) 4px,transparent 4px,transparent 8px)}}
.bar.mid .track i{{background:var(--warn)}}
.bar.high .track i{{background:var(--err)}}
.val{{font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums;white-space:nowrap}}
dl{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0 6px}}
dt{{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em}}
dd{{margin:2px 0 0}}
.big{{font-size:17px;font-variant-numeric:tabular-nums}}
.detail{{color:var(--mut);font-size:11.5px;margin:4px 0 0}}
.err{{color:var(--err)}}
.hintful{{color:var(--mut);font-weight:500}}
.actions{{display:flex;gap:7px;margin-top:14px;flex-wrap:wrap}}
button{{font:inherit;font-size:12px;padding:5px 11px;border:1px solid var(--line);
background:var(--bg);color:var(--fg);border-radius:7px;cursor:pointer}}
button:hover:not(:disabled){{border-color:var(--act);color:var(--act)}}
button:disabled{{opacity:.35;cursor:default}}
h3{{font-size:13px;margin:26px 0 8px;color:var(--mut);text-transform:uppercase;
letter-spacing:.06em}}
table{{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:12.5px}}
th{{text-align:left;color:var(--mut);font-weight:500;font-size:11px;
text-transform:uppercase;letter-spacing:.05em}}
th,td{{padding:7px 11px;border-bottom:1px solid var(--line)}}
tr:last-child td{{border-bottom:0}}
tr.err td{{background:color-mix(in srgb,var(--err) 7%,transparent)}}
.sw{{color:var(--warn);font-size:10.5px}}
td{{font-variant-numeric:tabular-nums}}
.none{{color:var(--mut);padding:18px 11px}}
.note{{color:var(--mut);font-size:11.5px;margin-top:18px;line-height:1.6}}
.pager{{display:flex;gap:10px;align-items:center;justify-content:flex-end;
margin-top:10px;color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}}
.note b{{color:var(--fg)}}
.btn-cmds{{font-size:11px;padding:3px 10px;margin-left:8px;vertical-align:3px;
border:1px solid var(--line);background:var(--card);color:var(--mut);border-radius:999px;cursor:pointer}}
.btn-cmds:hover{{border-color:var(--act);color:var(--act)}}
dialog{{border:1px solid var(--line);border-radius:14px;background:var(--card);color:var(--fg);
padding:22px 24px;max-width:620px;width:calc(100% - 32px);box-shadow:0 18px 50px rgba(0,0,0,.45)}}
dialog::backdrop{{background:rgba(0,0,0,.55)}}
dialog h3{{margin:0 0 12px;font-size:15px;color:var(--fg);text-transform:none;letter-spacing:0}}
.close{{float:right;border:0;background:none;color:var(--mut);font-size:22px;line-height:1;
cursor:pointer;padding:0 2px;margin:-4px -4px 0 0}}
.close:hover{{color:var(--fg)}}
.foot{{color:var(--mut);font-size:11.5px;line-height:1.6;margin:14px 0 0}}
.foot b{{color:var(--fg)}}
.cmds{{border:0;background:none;margin:0;font-size:12.5px;width:100%}}
.cmds td{{border:0;padding:5px 12px 5px 0;vertical-align:top}}
.cmds td:last-child{{color:var(--mut)}}
.cmds td:first-child{{white-space:nowrap}}
.cmds code{{background:var(--bg)}}
code{{font-size:11.5px;background:var(--line);padding:1px 5px;border-radius:4px}}
.now{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--line);
border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:14px 0 6px}}
.now>div{{background:var(--card);padding:12px 14px}}
.now span{{display:block;color:var(--mut);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em}}
.now b{{font-size:19px}}
.hint{{display:block;font-style:normal;color:var(--mut);font-size:10.5px;margin-top:2px}}
.reset{{background:var(--bg);border-radius:8px;padding:9px 12px;margin:0 0 12px;font-size:13px}}
.reset b{{font-variant-numeric:tabular-nums}}
tr.muted td{{color:var(--mut)}}
.reset.wait{{background:color-mix(in srgb,var(--warn) 12%,transparent);color:var(--warn)}}
.reset.wait b{{color:var(--warn)}}
.reset.ok{{color:var(--ok)}}
.reset.unknown{{color:var(--mut)}}
.reset small{{display:block;color:var(--mut);font-size:11px;margin-top:3px}}
</style>
<div id=live data-sw="{sw_page}" data-log="{log_page}">
<div class=top>
  <h1>clauxy <button id="open-cmds" class="btn-cmds">Commands</button></h1>
  <div class=val><b>{money(snap['usd_total'])}</b> · {snap['requests__total']} requests
   · {snap['switches']} switches</div>
</div>
<div class=now>
  <div><span>Accounts</span><b>{total}</b><i class=hint>{summary}</i></div>
  <div><span>Current account</span><b>{active or '—'}</b>
       <i class=hint>{f"{requests_on_active} requests" if started else "no requests yet"}</i></div>
  <div><span>Next if drained</span><b>{nxt}</b><i class=hint>&nbsp;</i></div>
  <div><span>Strategy</span><b>{snap.get('mode','?')}</b>
       <i class=hint>{f"switch at {THRESHOLD_5H:.0f}% · spend perishable first" if snap.get('mode')=='sequential' else 'each client keeps its own'}</i></div>
</div>
<p class=sub>Up for {clock(snap['started_s_ago'])} · live</p>

{commands}

{"".join(cards)}

{switches_html}

<h3>Request log</h3>
<table id="log">
<tr><th>Time</th><th>Model</th><th>Account</th><th>Status</th>
    <th>Tokens (in · cache r · cache w / out)</th><th>Cost</th><th>Took</th></tr>
{"".join(rows) or '<tr><td colspan=7 class=none>No requests yet.</td></tr>'}
</table>
{pager_html("log", log_page, len(audit), LOG_PAGE)}

<p class=note><b>Spend is indicative.</b> On a subscription you do not pay per
token: this is what the same traffic would cost at public API prices, useful to
compare accounts against each other. The quota bars are the real limit.</p>
</div>
<script>
(function () {{
  // The panel used to be a meta-refresh every 10 s, so "IN USE" could name the
  // account you had already left for up to ten seconds. It now re-fetches
  // itself every second and swaps the part that changes, which is also why all
  // the wiring lives out here: markup inserted through innerHTML never runs
  // its own <script>.
  // Which slice each table is showing. The server renders only that slice,
  // so a page holds ten log rows and five switches no matter how much
  // history there is — the rest is fetched when you ask for it.
  var live0 = document.getElementById('live');
  var pages = {{sw: +live0.dataset.sw || 0, log: +live0.dataset.log || 0}};

  function pager() {{
    document.querySelectorAll('[data-page]').forEach(function (b) {{
      b.onclick = function () {{
        var bits = b.dataset.page.split(':');
        pages[bits[0]] = Math.max(0, parseInt(bits[1], 10) || 0);
        b.disabled = true;      // until the rows it asked for arrive
        tick(true);
      }};
    }});
  }}

  function query() {{
    return '/_status?sw=' + pages.sw + '&log=' + pages.log;
  }}

  function dialog(reopen) {{
    var d = document.getElementById('cmds');
    var open_ = document.getElementById('open-cmds');
    if (!d || !open_) {{ return; }}
    open_.onclick = function () {{ d.showModal(); }};
    d.addEventListener('click', function (e) {{ if (e.target === d) d.close(); }});
    if (reopen && !d.open) {{ d.showModal(); }}
  }}

  function wire(reopen) {{ pager(); dialog(reopen); }}

  function tick(asked) {{
    var live = document.getElementById('live');
    if (!asked) {{
      // Never redraw out from under a button the user is about to press, or
      // a dialog they are reading past the first line. A click on the pager
      // is not an interruption, though — it is the whole point.
      if (document.hidden) {{ return; }}
      var focus = document.activeElement;
      if (focus && focus !== document.body && live.contains(focus)) {{ return; }}
    }}
    fetch(query(), {{cache: 'no-store'}})
      .then(function (r) {{ return r.ok ? r.text() : null; }})
      .then(function (html) {{
        if (!html) {{ return; }}
        var fresh = new DOMParser().parseFromString(html, 'text/html')
                        .getElementById('live');
        if (!fresh) {{ return; }}
        var d = document.getElementById('cmds');
        var wasOpen = !!(d && d.open);
        live.innerHTML = fresh.innerHTML;
        live.dataset.sw = fresh.dataset.sw;
        live.dataset.log = fresh.dataset.log;
        // The server clamps a page past the end; follow it rather than
        // keep asking for a slice that is not there.
        pages.sw = +fresh.dataset.sw || 0;
        pages.log = +fresh.dataset.log || 0;
        wire(wasOpen);
      }})
      .catch(function () {{}});   // the proxy restarting is not worth a stack trace
  }}

  wire(false);
  setInterval(tick, 1000);
}})();
</script>
</html>"""


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def save_periodically(pool, every=10):
    """Saves when something changed. Not per request: that would rewrite the
    whole file on every call while the client waits."""
    while True:
        time.sleep(every)
        if pool.dirty:
            pool.dirty = False
            save_state(pool)


def watch_quotas(pool, every=PROBE_EVERY):
    """Probes every account's quota once a minute, in the background.

    Spread out across the minute rather than fired together: three accounts
    asking in the same instant is a burst, and bursts are what the endpoint
    rate-limits on.
    """
    while True:
        accounts = list(pool.accounts) or [None]
        # One full pass per `every`, not `every` plus a pass: the gaps are
        # carved out of the interval, not added to it.
        gap = every / len(accounts)
        for account in accounts:
            time.sleep(gap)
            # Backing off is the whole point: hammering the usage endpoint
            # through its own rate limit is what earned the 429.
            if account is None or account.probe_wait:
                continue
            try:
                account.probe_quota()
            except Exception:
                pass


def probe(pool, port):
    """End-to-end check: the real CLI, through the proxy.

    A token cannot be validated by hand-building a request to
    api.anthropic.com: the API answers 429 without quota headers to any client
    it does not recognise as the CLI, and faking that identity is not an
    option. The real test is the real thing: start the proxy and run
    `claude -p` through it with each account's token.
    """
    Handler.pool = pool
    server = Server(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    failures = 0
    for account in pool.accounts:
        # One account at a time. `only` disables switching: we want to know
        # what THIS account does, not have the pool rescue it — on a switch the
        # CLI would say OK and we would not know which one answered.
        pool.only = account
        for other in pool.accounts:
            other.cooling_until = 0.0

        account.probe_quota()
        pct, in_s, _ = account.window("five_hour")
        quota = (f"5h {pct:.0f}% · resets in {clock(in_s)}"
                 if pct is not None else "no quota data")

        cfg = os.path.join(HOME, "sessions", account.alias)
        if not os.path.isdir(cfg):
            cfg = os.path.join(HOME, "sessions", "_probe")
        os.makedirs(cfg, exist_ok=True)
        run = subprocess.run(
            ["claude", "-p", "di OK", "--model", "claude-haiku-5-5"],
            env={**os.environ, "ANTHROPIC_BASE_URL": base,
                 "CLAUDE_CODE_OAUTH_TOKEN": account.token,
                 "CLAUDE_CONFIG_DIR": cfg},
            capture_output=True, text=True, timeout=180,
        )
        output = (run.stdout or run.stderr).strip().replace("\n", " ")[:90]
        label = f"{account.alias:12} {account.email or '':32}"
        if account.last_status == 200:
            print(f"{label} OK          {quota}")
        elif account.last_status in RETRYABLE:
            # The token works: an account out of quota authenticates fine and
            # is then refused. That is not a failure, it is a wait.
            back = clock(account.cooling_for) if account.cooling_for else "?"
            print(f"{label} NO QUOTA   back in {back}")
        else:
            print(f"{label} FAILED      (http {account.last_status}) {output}")
            failures += 1

    pool.only = None
    for a in pool.accounts:
        a.cooling_until = 0.0
    server.shutdown()
    return failures


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.splitlines()[2:]),
    )
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument(
        "--accounts",
        help="comma-separated aliases in priority order (default: all)",
    )
    parser.add_argument(
        "--mode", choices=["sequential", "sticky"], default="sequential",
        help="sequential: drain one account before moving to the next "
             "(default). sticky: each client keeps the account its own token "
             "maps to, borrowing another only when that one runs out.")
    parser.add_argument("--probe", action="store_true",
                        help="run the real CLI through the proxy, account by account, then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    aliases = (
        [a.strip() for a in args.accounts.split(",") if a.strip()]
        if args.accounts else known_accounts()
    )
    if not aliases:
        sys.exit("no accounts configured — run: clauxy add <alias>")

    accounts = []
    for alias in aliases:
        token = token_for(alias)
        if not token:
            sys.exit(f"no token found for account {alias!r}")
        accounts.append(Account(alias, token))

    LABELS.update(read_labels())

    pool = Pool(accounts)
    pool.mode = args.mode

    if args.probe:
        sys.exit(1 if probe(pool, 0) else 0)

    # Restore before probing. A restart used to throw away a reading taken
    # seconds earlier and ask again, so a handful of restarts in a row was
    # enough to earn a 429 from an endpoint that rate-limits on its own.
    restored = load_state(pool)

    stale = [a for a in accounts
             if time.time() - a.quota_at > PROBE_EVERY and not a.probe_wait]
    if stale:
        print("probing each account's quota...")
    for account in accounts:
        if account in stale:
            account.probe_quota()
        pct, in_s, _ = account.window("five_hour")
        if account in stale:
            fresh = ""
        elif account.probe_wait:
            # Saying nothing here is how you end up staring at figures that
            # are not being refreshed and wondering why.
            fresh = f" (not refreshing for {clock(account.probe_wait)})"
        elif account.quota_at:
            fresh = f" (read {clock(int(time.time() - account.quota_at))} ago)"
        else:
            fresh = ""
        print(f"  {account.alias:12} {account.email or '?':32} "
              + (f"5h {pct:.0f}% · resets en {clock(in_s)}{fresh}"
                 if pct is not None else f"no quota data ({account.quota_error})"))

    if restored:
        done = sum(a.requests for a in accounts)
        waiting = [a.alias for a in accounts if not a.available]
        print(f"  HISTORY   restored: {done} requests"
              + (f" · on hold: {', '.join(waiting)}" if waiting else ""))

    threading.Thread(target=watch_quotas, args=(pool,), daemon=True).start()
    threading.Thread(target=save_periodically, args=(pool,), daemon=True).start()

    Handler.pool = pool
    Handler.verbose = args.verbose
    # Inside a container we must bind 0.0.0.0 for the published port to reach
    # us. Publish it on the host's 127.0.0.1 only — anything else exposes your
    # subscriptions to the network with no credential at all.
    listen = os.environ.get("ESCUCHA", "127.0.0.1")
    server = Server((listen, args.port), Handler)
    print(f"\n  MODO      {args.mode}"
          + ("  (drain one account, then the next)"
             if args.mode == "sequential"
             else "  (each client keeps its own; borrows when drained)"))
    print(f"  ORDER     {' > '.join(a.alias for a in accounts)}")
    print(f"  PANEL     http://127.0.0.1:{args.port}/_status")
    print(f"  ENDPOINT  http://127.0.0.1:{args.port}   <- point ANTHROPIC_BASE_URL here")
    
    def on_exit(reason):
        """Records in the log why the process is ending.

        Clients depend on this port: if it dies they hang talking to nobody.
        Without this trace there is no way to know what happened, and the log
        file is left empty.
        """
        save_state(pool)
        sys.stderr.write(f"\n[proxy] TERMINA: {reason}\n")
        sys.stderr.write(f"[proxy] sirvio {sum(a.requests for a in accounts)} requests_\n")
        sys.stderr.flush()

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda s, _f: (on_exit(f"sig {s}"), sys.exit(0)))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        on_exit("Ctrl+C")
    except BaseException as exc:
        on_exit(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
