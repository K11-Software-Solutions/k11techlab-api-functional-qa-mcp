"""Database checks and fixtures, with guardrails.

URLs (from the environment, never literal in flows):
  sqlite:///path/to/file.db   (relative to the project)   · postgresql://user:pass@host:5432/db
  mysql://user:pass@host:3306/db                         · mssql://user:pass@host:1433/db

Guardrails:
- Queries use named parameters (``:orderId``). ``${...}`` inside SQL text is refused, so values
  can't be pasted into SQL.
- One statement per step. ``db`` checks may only read (SELECT / WITH / SHOW / EXPLAIN / VALUES),
  and run read-only where the driver allows it, inside a transaction that is rolled back.
- Writes (fixtures, cleanup) need ``allow_writes: true`` on the database *and* an environment that
  allows writes.
- At most 1,000 rows are read; masked columns are shown as ••• in evidence.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .values import MASK

READ_ONLY = re.compile(r"^\s*(select|with|show|explain|values|pragma\s+table_info)\b", re.I)
WRITE_VERBS = re.compile(r"\b(insert|update|delete|merge|drop|alter|truncate|create|grant|revoke|replace|call|exec)\b", re.I)
PARAM = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
MAX_ROWS = 1000


class DbError(ValueError):
    pass


def _strip_literals(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S)


def check_sql(sql: str, write: bool) -> None:
    if "${" in sql:
        raise DbError("Put values in params (e.g. WHERE id = :orderId), not ${...} inside the SQL")
    bare = _strip_literals(sql).strip().rstrip(";")
    if ";" in bare:
        raise DbError("Only one SQL statement per step")
    if not write:
        # must start as a read, and contain no write verb anywhere (e.g. WITH x AS (DELETE ...) in PostgreSQL)
        if not READ_ONLY.match(bare) or WRITE_VERBS.search(bare):
            raise DbError("db checks may only read (SELECT/WITH/SHOW/EXPLAIN). Use fixtures or cleanup for writes.")
        if re.search(r"\bfor\s+update\b", bare, re.I):
            raise DbError("SELECT ... FOR UPDATE takes locks; not allowed in a check")


class Connection:
    def __init__(self, url: str, root: Path, read_only: bool):
        self.url, self.read_only = url, read_only
        u = urlsplit(url)
        self.kind = u.scheme.split("+")[0]
        if self.kind == "sqlite":
            raw = unquote(url.split("sqlite:///", 1)[1]) if "sqlite:///" in url else ""
            if not raw:
                raise DbError("sqlite URL must look like sqlite:///path/to/file.db")
            path = (Path(root) / raw).resolve() if not Path(raw).is_absolute() else Path(raw)
            if not path.is_file():
                raise DbError(f"SQLite database not found: {raw}")
            uri = f"{path.as_uri()}?mode={'ro' if read_only else 'rw'}"
            self.conn = sqlite3.connect(uri, uri=True, timeout=10)
            self.style = "named"
        elif self.kind in ("postgresql", "postgres"):
            try:
                import psycopg
            except ImportError:
                raise DbError("PostgreSQL needs: pip install 'k11-api-functional-qa-mcp[postgres]'") from None
            self.conn = psycopg.connect(url.replace("postgres://", "postgresql://"), connect_timeout=10)
            if read_only:
                self.conn.read_only = True
            self.style = "pyformat"
        elif self.kind in ("mysql", "mariadb"):
            try:
                import pymysql
            except ImportError:
                raise DbError("MySQL needs: pip install 'k11-api-functional-qa-mcp[mysql]'") from None
            self.conn = pymysql.connect(host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""),
                                        password=unquote(u.password or ""), database=u.path.lstrip("/"),
                                        connect_timeout=10, autocommit=False)
            if read_only:
                with self.conn.cursor() as c:
                    c.execute("SET SESSION TRANSACTION READ ONLY")
            self.style = "pyformat"
        elif self.kind in ("mssql", "sqlserver"):
            try:
                import pymssql
            except ImportError:
                raise DbError("SQL Server needs: pip install 'k11-api-functional-qa-mcp[mssql]'") from None
            self.conn = pymssql.connect(server=u.hostname, port=str(u.port or 1433), user=unquote(u.username or ""),
                                        password=unquote(u.password or ""), database=u.path.lstrip("/"), login_timeout=10)
            self.style = "pyformat"
        else:
            raise DbError(f"Unsupported database URL scheme '{u.scheme}'. Use sqlite, postgresql, mysql or mssql.")

    def _convert(self, sql: str, params: dict) -> tuple[str, Any]:
        used = PARAM.findall(_strip_literals(sql))
        missing = [p for p in used if p not in params]
        if missing:
            raise DbError(f"Missing parameter(s): {', '.join(missing)}")
        if self.style == "named":
            return sql, {k: params[k] for k in used}
        # replace :name outside literals with %(name)s
        out, last = [], 0
        for m in re.finditer(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|(?<![:\w]):([A-Za-z_]\w*)", sql):
            if m.group(1):
                out.append(sql[last:m.start()].replace("%", "%%"))
                out.append(f"%({m.group(1)})s")
                last = m.end()
        out.append(sql[last:].replace("%", "%%"))
        return "".join(out), {k: params[k] for k in used}

    def execute(self, sql: str, params: dict) -> dict:
        q, p = self._convert(sql, params)
        cur = self.conn.cursor()
        try:
            cur.execute(q, p)
            if cur.description:
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchmany(MAX_ROWS + 1)]
                truncated = len(rows) > MAX_ROWS
                return {"columns": cols, "rows": rows[:MAX_ROWS], "truncated": truncated, "rowcount": len(rows[:MAX_ROWS])}
            return {"columns": [], "rows": [], "truncated": False, "rowcount": cur.rowcount}
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass

    def finish(self, commit: bool) -> None:
        try:
            (self.conn.commit if commit and not self.read_only else self.conn.rollback)()
        finally:
            self.conn.close()


def run(url: str, root: Path, sql: str, params: dict, write: bool) -> dict:
    check_sql(sql, write)
    conn = Connection(url, root, read_only=not write)
    ok = False
    try:
        res = conn.execute(sql, params)
        ok = True
        return res
    finally:
        conn.finish(commit=ok and write)


def mask_rows(rows: list[dict], columns: list[str]) -> list[dict]:
    low = {c.lower() for c in columns}
    return [{k: (MASK if k.lower() in low and v is not None else _jsonable(v)) for k, v in r.items()} for r in rows]


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def insert_sql(table: str, values: dict) -> tuple[str, dict]:
    _ident(table)
    cols = list(values)
    for c in cols:
        _ident(c)
    return (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})", dict(values))


def delete_sql(table: str, where: dict) -> tuple[str, dict]:
    _ident(table)
    if not where:
        raise DbError("delete needs a where clause")
    for c in where:
        _ident(c)
    return f"DELETE FROM {table} WHERE {' AND '.join(f'{c} = :{c}' for c in where)}", dict(where)


def _ident(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)?", name):
        raise DbError(f"Invalid table or column name: {name!r}")
