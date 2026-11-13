"""Export a flow as plain pytest code (httpx + DB-API), so teams can keep the suite without this tool."""

from __future__ import annotations

import json
import re
from typing import Any

HEADER = '''"""{title}

Exported from {source} by k11-api-functional-qa-mcp. Plain pytest + httpx; edit freely.
Environment: API_BASE_URL, API_TOKEN (optional), TEST_DB_URL (sqlite:///file.db or postgresql://...).
"""
import os
import re
import sqlite3

import httpx
import pytest

BASE_URL = os.environ.get("API_BASE_URL", "{base_url}")
HEADERS = {{"Authorization": f"Bearer {{os.environ['API_TOKEN']}}"}} if os.environ.get("API_TOKEN") else {{}}


def db_query(sql, params):
    """Read-only query helper for sqlite (stdlib) or PostgreSQL (psycopg)."""
    url = os.environ["TEST_DB_URL"]
    if url.startswith("sqlite:///"):
        conn = sqlite3.connect(f"file:{{url[len('sqlite:///'):]}}?mode=ro", uri=True)
        try:
            cur = conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()
    import psycopg  # pip install "psycopg[binary]"
    with psycopg.connect(url) as conn:
        conn.read_only = True
        cur = conn.execute(re.sub(r"(?<![:\\w]):([A-Za-z_]\\w*)", r"%(\\1)s", sql), params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def jpath(doc, path):
    for part in re.findall(r"\\.([\\w$@-]+)|\\[(\\d+)\\]", path[1:]):
        key, idx = part
        doc = doc[int(idx)] if idx else doc[key]
    return doc
'''


def _py(v: Any) -> str:
    """A Python expression for a value, turning ${var} into variables and ${ENV:X} into os.environ."""
    if isinstance(v, str):
        m = re.fullmatch(r"\$\{([^}]+)\}", v.strip())
        if m:
            return _var(m.group(1))
        if "${" in v:
            parts = re.split(r"(\$\{[^}]+\})", v)
            return "f" + json.dumps("".join("{" + _var(p[2:-1]) + "}" if p.startswith("${") else p.replace("{", "{{").replace("}", "}}")
                                            for p in parts))
        return json.dumps(v)
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))}: {_py(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_py(x) for x in v) + "]"
    return repr(v)


def _var(name: str) -> str:
    name = name.strip()
    if name.startswith("ENV:"):
        return f"os.environ[{name[4:]!r}]"
    gens = {"uuid": "str(__import__('uuid').uuid4())", "random_email": "f\"qa+{__import__('uuid').uuid4().hex[:10]}@example.com\"",
            "random_int": "__import__('random').randint(1000, 999999)"}
    if name in gens:
        return gens[name]
    parts = name.split(".")
    expr = f"v[{parts[0]!r}]"
    for p in parts[1:]:
        expr += f"[{p!r}]"
    return expr


def _status_expr(exp: Any) -> str:
    if isinstance(exp, list):
        return " or ".join(f"({_status_expr(e)})" for e in exp)
    s = str(exp).lower()
    if s.endswith("xx"):
        return f"str(r.status_code)[0] == {s[0]!r}"
    return f"r.status_code == {int(s)}"


def _assert_value(target: str, matcher: Any) -> list[str]:
    ops = {"eq": "==", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    if isinstance(matcher, dict):
        out = []
        for op, e in matcher.items():
            if op in ops:
                cast = "float" if op in ("gt", "gte", "lt", "lte") else ""
                out.append(f"    assert {cast}({target}) {ops[op]} {cast}({_py(e)})" if cast else f"    assert {target} {ops[op]} {_py(e)}")
            elif op == "contains":
                out.append(f"    assert {_py(e)} in {target}")
            elif op == "matches":
                out.append(f"    assert re.search({_py(e)}, str({target}))")
            elif op == "length":
                out.append(f"    assert len({target}) == {_py(e)}")
            elif op == "in":
                out.append(f"    assert {target} in {_py(e)}")
            elif op == "exists":
                out.append(f"    {target}  # exists")
            else:
                out.append(f"    # TODO: '{op}' check on {target}")
        return out
    return [f"    assert str({target}) == str({_py(matcher)})"]


def to_pytest(flow: dict, source: str, base_url: str = "http://localhost:8080") -> str:
    title = flow["name"]
    name = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:60] or "flow"
    L = [HEADER.format(title=title.replace('"""', "'"), source=source, base_url=base_url), "",
         f"def test_{name}():", f"    v = {_py(flow.get('vars') or {})}"]
    if flow.get("fixtures"):
        L.append(f"    # TODO: fixtures {flow['fixtures']} were set up by the runner; create that test data here.")
    L.append("    try:")
    body_lines: list[str] = []
    for i, s in enumerate(flow["steps"], 1):
        body_lines.append(f"    # step {i}: {s.get('name') or s.get('call') or 'db check'}")
        if s.get("call"):
            method, _, path = str(s["call"]).partition(" ")
            path_expr = _py(path.strip())
            for k, val in (s.get("path") or {}).items():
                path_expr = f"{path_expr}.replace({('{' + k + '}')!r}, str({_py(val)}))"
            kw = ["headers={**HEADERS" + (f", **{_py(s['headers'])}" if s.get("headers") else "") + "}"
                  if s.get("auth") != "none" else (f"headers={_py(s.get('headers') or {})}")]
            if s.get("query"):
                kw.append(f"params={_py(s['query'])}")
            if "body" in s:
                kw.append(f"json={_py(s['body'])}")
            body_lines.append(f"    r = httpx.request({method.upper()!r}, BASE_URL + {path_expr}, {', '.join(kw)}, timeout=30)")
            exp = s.get("expect") or {}
            body_lines.append(f"    assert {_status_expr(exp.get('status', '2xx'))}, r.text[:300]")
            if exp.get("json") or s.get("save"):
                body_lines.append("    data = r.json()")
            for p, m in (exp.get("json") or {}).items():
                body_lines += _assert_value(f"jpath(data, {p!r})", m)
            for var, src in (s.get("save") or {}).items():
                body_lines.append(f"    v[{var!r}] = jpath(data, {str(src)!r})" if str(src).startswith("$")
                                  else f"    v[{var!r}] = r.status_code" if src == "status" else f"    # TODO: save {var} from {src}")
        else:
            body_lines.append(f"    rows = db_query({json.dumps(' '.join(str(s['db']).split()))}, {_py(s.get('params') or {})})")
            exp = s.get("expect") or {}
            if "rows" in exp:
                body_lines += _assert_value("len(rows)", exp["rows"])
            for col, m in (exp.get("row") or {}).items():
                body_lines += _assert_value(f"rows[0][{col!r}]", m)
    L += ["    " + ln if ln else ln for ln in body_lines]
    L.append("    finally:")
    cleanup = flow.get("cleanup") or []
    if not cleanup:
        L.append("        pass")
    for s in cleanup:
        if s.get("call"):
            method, _, path = str(s["call"]).partition(" ")
            L.append(f"        httpx.request({method.upper()!r}, BASE_URL + {_py(path.strip())}, headers=HEADERS, timeout=30)")
        else:
            L.append(f"        pass  # TODO cleanup query: {' '.join(str(s['db']).split())[:100]}")
    return "\n".join(L) + "\n"
