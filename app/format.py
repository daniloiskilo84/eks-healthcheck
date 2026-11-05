"""Small display helpers shared by the pages and the API."""

from __future__ import annotations

_BINARY = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
_DECIMAL = {"k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}


def humanize_duration(seconds: float) -> str:
    """12d 4h · 3h 20m · 7m · 30s (two units at most, like kubectl AGE)."""
    s = int(max(seconds, 0))
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def memory_gib(quantity: str) -> str:
    """Kubernetes memory quantity ("16073824Ki") to "15.3 GiB"."""
    if not quantity:
        return "-"
    try:
        for suffix, mult in _BINARY.items():
            if quantity.endswith(suffix):
                return f"{float(quantity[:-2]) * mult / 2**30:.1f} GiB"
        for suffix, mult in _DECIMAL.items():
            if quantity.endswith(suffix):
                return f"{float(quantity[:-1]) * mult / 2**30:.1f} GiB"
        return f"{float(quantity) / 2**30:.1f} GiB"
    except ValueError:
        return quantity


def cpu_cores(quantity: str) -> str:
    """Kubernetes CPU quantity ("3920m") to cores ("3.92")."""
    if not quantity:
        return "-"
    try:
        if quantity.endswith("m"):
            return f"{float(quantity[:-1]) / 1000:g}"
        return f"{float(quantity):g}"
    except ValueError:
        return quantity
