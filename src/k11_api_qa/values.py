"""Variables, generators, JSON paths and assertions used by test flows.

Substitution: ``${name}``, ``${fixture.customer.id}``, ``${ENV:API_TOKEN}`` and generators
``${uuid}``, ``${now}``, ``${today}``, ``${random_int}``, ``${random_email}``, ``${random_string}``.
A string that is exactly one ``${...}`` keeps the value's type (numbers stay numbers).
Every value read from the environment is remembered as a secret and masked in evidence.
"""

from __future__ import annotations

import os
import random
import re
import string
import uuid
from datetime import date, datetime, timezone
from typing import Any

VAR = re.compile(r"\$\{([^}]+)\}")
MASK = "•••"


class Vars:
    def __init__(self, initial: dict | None = None):
        self.values: dict[str, Any] = dict(initial or {})
        self.secrets: set[str] = set()

    def _lookup(self, name: str) -> Any:
        name = name.strip()
        if name.startswith("ENV:"):
            key = name[4:]
            if key not in os.environ:
                raise ValueError(f"Environment variable {key} is not set")
            value = os.environ[key]
            if value:
                self.secrets.add(value)
            return value
        gen = {
            "uuid": lambda: str(uuid.uuid4()),
            "now": lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "today": lambda: date.today().isoformat(),
            "random_int": lambda: random.randint(1000, 999999),
            "random_email": lambda: f"qa+{uuid.uuid4().hex[:10]}@example.com",
            "random_string": lambda: "".join(random.choices(string.ascii_lowercase, k=10)),
        }.get(name)
        if gen:
            return gen()
        cur: Any = self.values
        for part in name.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                raise ValueError(f"Unknown variable ${{{name}}}")
        return cur

    def sub(self, value: Any) -> Any:
        if isinstance(value, str):
            m = VAR.fullmatch(value.strip())
            if m:
                return self._lookup(m.group(1))
            return VAR.sub(lambda mm: str(self._lookup(mm.group(1))), value)
        if isinstance(value, dict):
            return {self.sub(k): self.sub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.sub(v) for v in value]
        return value

    def mask(self, value: Any) -> Any:
        """Replace secret values anywhere in a string/structure."""
        if isinstance(value, str):
            for s in sorted(self.secrets, key=len, reverse=True):
                if len(s) >= 4:
                    value = value.replace(s, MASK)
            return value
        if isinstance(value, dict):
            return {k: self.mask(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.mask(v) for v in value]
        return value


# ---- JSON paths: $.a.b[0].c, $.items[*].sku, $.items.length --------------------------------------

_SEG = re.compile(r"\.([A-Za-z_@$][\w@$-]*)|\[(\d+|\*|'[^']*'|\"[^\"]*\")\]")


def json_path(doc: Any, path: str) -> tuple[bool, Any]:
    """(found, value). '*' collects a list; '.length' gives a list/str length."""
    if path in ("$", ""):
        return True, doc
    if not path.startswith("$"):
        path = "$." + path
    rest = path[1:]
    nodes, multi = [doc], False
    pos = 0
    while pos < len(rest):
        m = _SEG.match(rest, pos)
        if not m:
            raise ValueError(f"Unsupported JSON path: {path}")
        pos = m.end()
        key, idx = m.group(1), m.group(2)
        nxt = []
        for n in nodes:
            if key is not None:
                if key == "length" and isinstance(n, (list, str)) and pos == len(rest):
                    nxt.append(len(n))
                elif isinstance(n, dict) and key in n:
                    nxt.append(n[key])
            elif idx == "*":
                multi = True
                if isinstance(n, list):
                    nxt.extend(n)
                elif isinstance(n, dict):
                    nxt.extend(n.values())
            elif idx[0] in "'\"":
                k = idx[1:-1]
                if isinstance(n, dict) and k in n:
                    nxt.append(n[k])
            elif isinstance(n, list) and int(idx) < len(n):
                nxt.append(n[int(idx)])
        nodes = nxt
        if not nodes and not multi:
            return False, None
    if multi:
        return True, nodes
    return (True, nodes[0]) if nodes else (False, None)


# ---- assertions ---------------------------------------------------------------------------------

OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "not_contains", "matches", "length", "type", "exists", "in",
       "not_in", "empty", "approx"}
TYPE_NAMES = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict,
              "null": type(None)}


def _num(x):
    if isinstance(x, bool):
        raise TypeError
    return float(x)


def check(actual: Any, expected: Any, found: bool = True) -> tuple[bool, str]:
    """Compare one value against an expectation: a literal, or a dict of operators."""
    if isinstance(expected, dict) and expected and set(expected) <= OPS:
        for op, exp in expected.items():
            ok, why = _op(op, actual, exp, found)
            if not ok:
                return False, why
        return True, ""
    if not found:
        return False, "missing"
    if _loose_equal(actual, expected):
        return True, ""
    return False, f"expected {expected!r}, got {actual!r}"


def _loose_equal(a: Any, e: Any) -> bool:
    if a == e:
        return True
    if isinstance(a, (int, float)) and isinstance(e, str) and not isinstance(a, bool):
        try:
            return float(e) == float(a)
        except ValueError:
            return False
    if isinstance(e, (int, float)) and isinstance(a, str) and not isinstance(e, bool):
        try:
            return float(a) == float(e)
        except ValueError:
            return False
    return str(a) == str(e) if isinstance(a, (str, int, float)) and isinstance(e, (str, int, float)) else False


def _op(op: str, a: Any, e: Any, found: bool) -> tuple[bool, str]:
    if op == "exists":
        return (found == bool(e)), ("" if found == bool(e) else ("missing" if e else f"should not exist, got {a!r}"))
    if not found:
        return False, "missing"
    try:
        if op == "eq":
            return _loose_equal(a, e), f"expected {e!r}, got {a!r}"
        if op == "ne":
            return not _loose_equal(a, e), f"expected anything but {e!r}"
        if op in ("gt", "gte", "lt", "lte"):
            av, ev = _num(a), _num(e)
            ok = {"gt": av > ev, "gte": av >= ev, "lt": av < ev, "lte": av <= ev}[op]
            return ok, f"expected {op} {e!r}, got {a!r}"
        if op == "approx":
            target, tol = (e[0], e[1]) if isinstance(e, list) else (e, 0.01)
            return abs(_num(a) - _num(target)) <= _num(tol), f"expected ≈{target} (±{tol}), got {a!r}"
        if op == "contains":
            return (e in a) if isinstance(a, (str, list, dict)) else False, f"expected to contain {e!r}, got {a!r}"
        if op == "not_contains":
            return (e not in a) if isinstance(a, (str, list, dict)) else True, f"expected not to contain {e!r}"
        if op == "matches":
            return bool(re.search(str(e), str(a))), f"expected to match /{e}/, got {a!r}"
        if op == "length":
            n = len(a)
            ok, why = check(n, e) if isinstance(e, dict) else (n == e, f"expected length {e}, got {n}")
            return ok, why if not ok else ""
        if op == "type":
            t = TYPE_NAMES.get(str(e))
            ok = t is not None and isinstance(a, t) and not (e in ("number", "integer") and isinstance(a, bool))
            return ok, f"expected type {e}, got {type(a).__name__}"
        if op == "in":
            return any(_loose_equal(a, x) for x in e), f"expected one of {e!r}, got {a!r}"
        if op == "not_in":
            return not any(_loose_equal(a, x) for x in e), f"expected none of {e!r}, got {a!r}"
        if op == "empty":
            return (len(a) == 0) == bool(e), f"expected {'empty' if e else 'not empty'}, got {a!r}"
    except (TypeError, ValueError):
        return False, f"cannot apply {op} to {a!r}"
    return False, f"unknown operator {op}"


def status_matches(status: int, expected: Any) -> bool:
    """201, "2xx", "4xx", [400, 422], {in: [...]} or {not: "5xx"}."""
    if isinstance(expected, list):
        return any(status_matches(status, e) for e in expected)
    if isinstance(expected, dict):
        if "not" in expected:
            return not status_matches(status, expected["not"])
        if "in" in expected:
            return status_matches(status, list(expected["in"]))
        return False
    s = str(expected).lower()
    if len(s) == 3 and s.endswith("xx") and s[0].isdigit():
        return str(status)[0] == s[0]
    try:
        return status == int(s)
    except ValueError:
        return False
