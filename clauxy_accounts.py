"""User-supplied subscription dates, separate from tokens and measured quota."""

import json
import os
import re
import tempfile
from datetime import date


HOME = os.path.expanduser(os.environ.get("CLAUXY_HOME", "~/.clauxy"))
RENEWALS_FILE = os.path.join(HOME, "subscriptions.json")


def parse_renewal(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("use a valid renewal date in YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("use a valid renewal date in YYYY-MM-DD format") from exc


def format_renewal(value):
    renewal = parse_renewal(value)
    months = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
              "agosto", "septiembre", "octubre", "noviembre", "diciembre")
    return f"{renewal.day} de {months[renewal.month - 1]}"


def read_renewals():
    try:
        with open(RENEWALS_FILE) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("subscriptions.json must contain an object")
    return data


def set_renewal(alias, value):
    """None clears the date. Replace atomically so live readers see complete JSON."""
    if value is not None:
        parse_renewal(value)
    data = read_renewals()
    if value is None:
        if alias not in data:
            return
        del data[alias]
    else:
        data[alias] = {"renewal_date": value, "source": "manual"}
    folder = os.path.dirname(RENEWALS_FILE)
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".subscriptions-", dir=folder)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(temporary, RENEWALS_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def subscription_snapshot(alias, today=None):
    empty = {"renewal_date": None, "days_remaining": None, "source": None}
    try:
        record = read_renewals().get(alias, {})
        renewal = parse_renewal(record.get("renewal_date"))
    except (OSError, ValueError, AttributeError):
        return empty
    return {"renewal_date": renewal.isoformat(),
            "days_remaining": (renewal - (today or date.today())).days,
            "source": "manual"}
