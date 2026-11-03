"""Byte-exact ``JSON.stringify(obj, null, 2)`` in Python.

The session ZIP is written by the backend now, but its golden snapshots — and any
browser-era archive people compare against — were produced by the dashboard's
``JSON.stringify``.  ``json.dumps`` is *close* but not identical: it prints ``1.0``
for integral floats, uses ``1e+21``-style Python exponent formatting (``repr``), and
escapes non-ASCII by default.  ``data/manifest.json``, ``cameras.json`` and
``video_report.json`` must round-trip byte-for-byte, so this module implements the
ECMA-262 ``Number::toString`` / ``JSON.stringify`` rules directly:

* layout: 2-space indent, ``": "`` after keys, ``", "`` → newline-separated items,
  empty containers stay inline (``[]`` / ``{}``), no trailing newline;
* numbers: integral floats print without ``.0``; the fixed/exponential switch
  follows the JS rules on the shortest round-trip digits (``repr`` in Python gives
  the same digits as JS): ``k ≤ n ≤ 21`` → digits plus zeros, ``0 < n ≤ 21`` →
  decimal point inside the digits, ``−6 < n ≤ 0`` → ``0.000…``, otherwise
  ``d[.d…]e±X`` with an explicit sign and no zero padding (so ``1e+21`` *with* the
  plus, but ``1e-7`` — that asymmetry is what JavaScript does);
* ``NaN`` / ``Infinity`` → ``null``, ``-0.0`` → ``0`` (JS prints negative zero as
  ``0`` through ``JSON.stringify``);
* strings: raw UTF-8 (``ensure_ascii=False``), the same short escapes and lowercase
  ``\\u00xx`` for other control characters that JS emits; U+2028/U+2029 stay raw;
* objects: insertion order preserved, keys coerced with ``str()``.

Only plain data (dict/list/tuple/str/int/float/bool/None) is accepted; anything else
raises ``TypeError`` rather than silently diverging from the browser.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

__all__ = ["js_json_dumps"]

_INDENT = "  "


def js_json_dumps(obj: Any) -> str:
    """Serialise *obj* exactly like ``JSON.stringify(obj, null, 2)``."""
    return _dump(obj, 0)


def _dump(obj: Any, level: int) -> str:
    if obj is None:
        return "null"
    if isinstance(obj, bool):  # before int: bool is an int subclass
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return _format_float(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, (list, tuple)):
        if not obj:
            return "[]"
        pad = _INDENT * (level + 1)
        items = ",\n".join(pad + _dump(v, level + 1) for v in obj)
        return "[\n" + items + "\n" + _INDENT * level + "]"
    if isinstance(obj, Mapping):
        if not obj:
            return "{}"
        pad = _INDENT * (level + 1)
        items = ",\n".join(
            pad + json.dumps(str(k), ensure_ascii=False) + ": " + _dump(v, level + 1)
            for k, v in obj.items()
        )
        return "{\n" + items + "\n" + _INDENT * level + "}"
    raise TypeError(f"Object of type {type(obj).__name__} is not JS-JSON serialisable")


def _format_float(v: float) -> str:
    if not math.isfinite(v):
        return "null"  # JSON.stringify(NaN) === "null", same for ±Infinity
    if v == 0.0:
        return "0"  # covers -0.0: JSON.stringify(-0) === "0"
    sign = "-" if v < 0 else ""
    m, n = _digits_and_exponent(abs(v))
    k = len(m)
    if k <= n <= 21:
        # integer with trailing zeros: 1e20 → 100000000000000000000
        body = m + "0" * (n - k)
    elif 0 < n <= 21:
        # decimal point inside the digits: 123.456, 0.1 → n=0 handled below
        body = m[:n] + "." + m[n:]
    elif -6 < n <= 0:
        # leading zeros: 1e-6 → 0.000001
        body = "0." + "0" * (-n) + m
    else:
        # exponential: signed exponent, no zero padding, no point for one digit
        e = n - 1
        exp = ("+" if e >= 0 else "-") + str(abs(e))
        mant = m if k == 1 else m[0] + "." + m[1:]
        body = mant + "e" + exp
    return sign + body


def _digits_and_exponent(v: float) -> tuple[str, int]:
    """Shortest digits ``m`` and power ``n`` with ``v == 0.<m> × 10**n``.

    Python's ``repr`` produces the same shortest round-trip digit string that
    JavaScript's number printer starts from, so parsing it gives exactly the
    ``m``/``k``/``n`` triple ECMA-262 §6.1.6.1.13 defines.  ``m`` has no leading
    or trailing zeros.
    """
    s = repr(v)
    if "e" in s:
        mant, _, exp = s.partition("e")
        e = int(exp)
    else:
        mant, e = s, 0
    int_part, _, frac_part = mant.partition(".")
    digits = int_part + frac_part
    # v == int(digits) × 10**(point - len(digits)) with point = len(int_part) + e
    point = len(int_part) + e
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)  # leading zeros shift the point left
    digits = stripped
    if not digits:  # unreachable: v == 0 handled by caller
        return "0", 0
    digits = digits.rstrip("0")  # trailing zeros fold into k, point (= n) unchanged
    return digits, point
