"""Run API test flows: HTTP calls, database checks, fixtures and cleanup, with evidence.

A flow (``tests/api/*.yaml``):

    name: Place an order and find it in the database
    auth: default                    # profile, or none
    fixtures: [customer]
    steps:
      - name: create the order
        call: POST /orders
        body: { customerId: "${fixture.customer.id}", items: [{ sku: BAG-1, qty: 2 }] }
        expect:
          status: 201
          json: { "$.status": pending, "$.total": { gt: 0 } }
        save: { orderId: "$.id" }
      - name: the order is stored
        db: SELECT status FROM orders WHERE id = :orderId
        params: { orderId: "${orderId}" }
        expect: { rows: 1, row: { status: pending } }
    cleanup:
      - call: DELETE /orders/${orderId}

Rules: any 5xx fails a step unless ``expect.status`` allows it; responses are checked against the
spec's schema when a spec is configured (``expect.schema: false`` turns it off); data-changing calls
are refused on environments that don't allow writes; cleanup always runs.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
import yaml
from jsonschema import Draft202012Validator

from . import auth as authmod
from . import db
from .config import Config, writes_allowed
from .values import MASK, Vars, check, json_path, status_matches

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
STEP_KEYS = {"name", "call", "db", "database", "path", "query", "headers", "body", "form", "auth", "expect", "save",
             "params", "fixture"}
FLOW_KEYS = {"name", "description", "auth", "fixtures", "vars", "steps", "cleanup", "tags", "environment",
             "continue_on_failure"}
EXPECT_HTTP = {"status", "json", "headers", "schema", "max_ms", "body_contains"}
EXPECT_DB = {"rows", "row", "rows_include"}
BODY_LIMIT = 2000


class FlowError(ValueError):
    pass


def validate_flow(flow: Any, label: str = "flow") -> dict:
    if not isinstance(flow, dict):
        raise FlowError(f"{label}: a flow must be a mapping with 'name' and 'steps'")
    unknown = set(flow) - FLOW_KEYS
    if unknown:
        raise FlowError(f"{label}: unknown key(s) {sorted(unknown)}")
    if not flow.get("name"):
        raise FlowError(f"{label}: missing 'name'")
    for section in ("steps", "cleanup"):
        steps = flow.get(section) or []
        if section == "steps" and not steps:
            raise FlowError(f"{label}: needs at least one step")
        for i, s in enumerate(steps, 1):
            where = f"{label} {section}[{i}]"
            if not isinstance(s, dict):
                raise FlowError(f"{where}: must be a mapping")
            bad = set(s) - STEP_KEYS
            if bad:
                raise FlowError(f"{where}: unknown key(s) {sorted(bad)}")
            if bool(s.get("call")) == bool(s.get("db")):
                raise FlowError(f"{where}: needs exactly one of 'call' (e.g. 'GET /orders') or 'db' (SQL)")
            if s.get("call"):
                method = str(s["call"]).split(" ", 1)[0].upper()
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} or " " not in str(s["call"]).strip():
                    raise FlowError(f"{where}: call must look like 'POST /orders'")
                bad = set(s.get("expect") or {}) - EXPECT_HTTP
            else:
                bad = set(s.get("expect") or {}) - EXPECT_DB
            if bad:
                raise FlowError(f"{where}: unknown expect key(s) {sorted(bad)}")
    return flow


def load_flow(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text("utf8"))
    except yaml.YAMLError as e:
        raise FlowError(f"{path.name}: invalid YAML: {str(e).splitlines()[0]}") from None
    return validate_flow(data, path.name)


@dataclass
class StepResult:
    name: str
    kind: str  # call | db | fixture
    status: str = "passed"  # passed | failed | blocked | error | skipped
    request: dict | None = None
    response: dict | None = None
    query: dict | None = None
    assertions: list[dict] = field(default_factory=list)
    saved: dict = field(default_factory=dict)
    error: str | None = None
    ms: int | None = None
    cleanup: bool = False


@dataclass
class FlowResult:
    name: str
    file: str
    environment: str
    status: str = "passed"
    steps: list[StepResult] = field(default_factory=list)
    ms: int = 0
    error: str | None = None


class Runner:
    def __init__(self, cfg: Config, environment: str | None = None, spec=None):
        self.cfg = cfg
        self.env_name, self.env = cfg.environment(environment or None)
        self.base_url = str(Vars().sub(self.env.get("base_url", "")) if "${" in str(self.env.get("base_url", "")) else self.env.get("base_url", ""))
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(f"Environment '{self.env_name}' needs base_url (http:// or https://)")
        self.writes, self.writes_why = writes_allowed(self.env_name, self.env, self.base_url)
        self.spec = spec
        self.client = httpx.Client(timeout=cfg.timeout_s, follow_redirects=False)

    def close(self):
        self.client.close()

    # ---- flows -----------------------------------------------------------------------------------
    def run_flow(self, flow: dict, file: str = "") -> FlowResult:
        res = FlowResult(flow["name"], file, self.env_name)
        v = Vars({"env": {"name": self.env_name, "base_url": self.base_url}})
        started = time.perf_counter()
        pending_cleanup: list[tuple[str, dict]] = []
        try:
            v.values.update(v.sub(flow.get("vars") or {}))
            auth_name = flow.get("auth", self.env.get("auth", "default"))
            for fx in flow.get("fixtures") or []:
                sr = self._fixture(fx, v, auth_name, pending_cleanup)
                res.steps.append(sr)
                if sr.status != "passed":
                    res.status = "failed" if sr.status == "failed" else sr.status
                    return res
            for i, step in enumerate(flow["steps"], 1):
                sr = self._step(step, v, auth_name, i)
                res.steps.append(sr)
                if sr.status == "passed":
                    continue
                if res.status == "passed":
                    res.status = sr.status if sr.status in ("blocked", "error") else "failed"
                if _fatal(sr) and not flow.get("continue_on_failure"):
                    break  # later steps depend on this one; value-only failures let the flow continue for more evidence
        except (ValueError, db.DbError) as e:
            res.status, res.error = "error", v.mask(str(e))
        finally:
            for i, step in enumerate(flow.get("cleanup") or [], 1):
                sr = self._step(step, v, flow.get("auth", self.env.get("auth", "default")), i, cleanup=True)
                res.steps.append(sr)
            for kind, spec in reversed(pending_cleanup):
                sr = self._step(spec, v, flow.get("auth", self.env.get("auth", "default")), 0, cleanup=True)
                sr.name = f"fixture cleanup: {kind}"
                res.steps.append(sr)
            res.ms = round((time.perf_counter() - started) * 1000)
        return res

    # ---- fixtures --------------------------------------------------------------------------------
    def _fixture(self, name: str, v: Vars, auth_name: str, pending: list) -> StepResult:
        spec = self.cfg.fixtures.get(name)
        sr = StepResult(f"fixture: {name}", "fixture")
        if spec is None:
            sr.status, sr.error = "error", f"Unknown fixture '{name}'. Known: {sorted(self.cfg.fixtures) or ['(none)']}"
            return sr
        fv: dict[str, Any] = {}
        if "insert" in spec:
            dbname = spec.get("db") or next(iter(self.cfg.databases), None)
            values = v.sub(spec["insert"].get("values") or {})
            sql, params = db.insert_sql(spec["insert"]["table"], values)
            ok, why = self._db_write_ok(dbname)
            if not ok:
                sr.status, sr.error = "blocked", why
                return sr
            try:
                db.run(self._db_url(dbname, v), self.cfg.root, sql, params, write=True)
            except db.DbError as e:
                sr.status, sr.error = "error", v.mask(str(e))
                return sr
            except Exception as e:  # noqa: BLE001 - driver errors
                sr.status, sr.error = "failed", v.mask(f"{type(e).__name__}: {e}")[:300]
                return sr
            fv = dict(values)
            sr.query = {"database": dbname, "sql": sql, "params": v.mask(params)}
            if spec.get("delete"):
                where = Vars(fv).sub(spec["delete"].get("where") or {})
                dsql, dparams = db.delete_sql(spec["delete"]["table"], where)
                pending.append((name, {"db": dsql, "params": dparams, "database": dbname}))
        elif "call" in spec:
            sub = self._step({k: spec[k] for k in spec if k in STEP_KEYS}, v, auth_name, 0)
            sr.request, sr.response, sr.assertions, sr.status, sr.error = sub.request, sub.response, sub.assertions, sub.status, sub.error
            if sub.status != "passed":
                return sr
            fv = dict(sub.saved)
            if spec.get("cleanup"):
                pending.append((name, Vars({**v.values, **fv}).sub(spec["cleanup"])))
        else:
            sr.status, sr.error = "error", f"Fixture '{name}' needs 'insert' (database) or 'call' (API)"
            return sr
        v.values.setdefault("fixture", {})[name] = fv
        sr.saved = v.mask(fv)
        return sr

    # ---- steps -----------------------------------------------------------------------------------
    def _db_url(self, name: str | None, v: Vars) -> str:
        if not name or name not in self.cfg.databases:
            raise db.DbError(f"Unknown database '{name}'. Known: {sorted(self.cfg.databases) or ['(none)']}")
        return str(v.sub(self.cfg.databases[name]["url"]))

    def _db_write_ok(self, name: str | None) -> tuple[bool, str]:
        if not self.writes:
            return False, f"Writes refused on environment '{self.env_name}': {self.writes_why}"
        if not (self.cfg.databases.get(name or "", {}) or {}).get("allow_writes"):
            return False, f"Database '{name}' does not allow writes (set allow_writes: true for a test database)"
        return True, ""

    def _step(self, step: dict, v: Vars, auth_name: str, index: int, cleanup: bool = False) -> StepResult:
        try:
            if step.get("db"):
                return self._db_step(step, v, index, cleanup)
            return self._call_step(step, v, auth_name, index, cleanup)
        except (ValueError, db.DbError) as e:
            label = step.get("name") or step.get("call") or "db check"
            if cleanup and str(e).startswith("Unknown variable"):
                # nothing to clean up: the step that would have created it didn't run or didn't create it
                return StepResult(str(label), "call" if step.get("call") else "db", "skipped",
                                  error=f"skipped: {e}", cleanup=True)
            return StepResult(str(label), "call" if step.get("call") else "db", "error", error=v.mask(str(e)), cleanup=cleanup)

    def _call_step(self, step: dict, v: Vars, auth_name: str, index: int, cleanup: bool) -> StepResult:
        method, _, template = str(step["call"]).strip().partition(" ")
        method = method.upper()
        template = template.strip()
        sr = StepResult(str(step.get("name") or f"{method} {template}"), "call", cleanup=cleanup)
        if method not in SAFE_METHODS and not self.writes:
            sr.status, sr.error = "blocked", f"{method} refused on '{self.env_name}': {self.writes_why}"
            return sr
        path = str(v.sub(template))
        for k, val in (v.sub(step.get("path") or {})).items():
            path = path.replace("{" + k + "}", quote(str(val), safe=""))
        url = urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/"))
        if urlsplit(url).netloc.lower() != urlsplit(self.base_url).netloc.lower() or urlsplit(url).scheme not in ("http", "https"):
            # never send credentials to a host other than the environment's
            sr.status, sr.error = "error", f"Request must stay on {urlsplit(self.base_url).netloc}; got {v.mask(url)}"
            return sr
        hdrs = authmod.headers(step.get("auth", auth_name), self.cfg.auth, v, self.cfg.timeout_s)
        hdrs.update({k: str(x) for k, x in v.sub(step.get("headers") or {}).items()})
        kwargs: dict[str, Any] = {"headers": hdrs}
        if step.get("query"):
            kwargs["params"] = v.sub(step["query"])
        if "body" in step:
            kwargs["json"] = v.sub(step["body"])
        elif "form" in step:
            kwargs["data"] = v.sub(step["form"])
        sr.request = {"method": method, "url": v.mask(url), "headers": _redact(hdrs, v),
                      **({"query": v.mask(kwargs["params"])} if "params" in kwargs else {}),
                      **({"body": v.mask(kwargs.get("json", kwargs.get("data")))} if ("json" in kwargs or "data" in kwargs) else {})}
        started = time.perf_counter()
        try:
            r = self.client.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            sr.status, sr.error = "failed", f"Request failed: {type(e).__name__}: {str(e)[:200]}"
            return sr
        sr.ms = round((time.perf_counter() - started) * 1000)
        body_text = r.text
        try:
            body: Any = r.json()
            is_json = True
        except ValueError:
            body, is_json = body_text, False
        sr.response = {"status": r.status_code, "ms": sr.ms, "content_type": r.headers.get("content-type", ""),
                       "body": v.mask(body if is_json and len(body_text) <= BODY_LIMIT else body_text[:BODY_LIMIT])}
        exp = step.get("expect") or {}
        # status: explicit expectation, else any 2xx/3xx; a 5xx always fails unless explicitly expected
        if "status" in exp:
            ok = status_matches(r.status_code, v.sub(exp["status"]))
            sr.assertions.append({"check": f"status {exp['status']}", "ok": ok, "detail": "" if ok else f"got {r.status_code}"})
        else:
            ok = r.status_code < 400
            sr.assertions.append({"check": "status 2xx/3xx", "ok": ok, "detail": "" if ok else f"got {r.status_code}"})
        if r.status_code >= 500 and not ("status" in exp and status_matches(r.status_code, v.sub(exp["status"]))):
            if all(a["check"] != "no server error" for a in sr.assertions):
                sr.assertions.append({"check": "no server error", "ok": False, "detail": f"HTTP {r.status_code}: a 5xx is a bug"})
        for p, matcher in (exp.get("json") or {}).items():
            found, val = json_path(body, p) if is_json else (False, None)
            ok, why = check(val, v.sub(matcher), found)
            sr.assertions.append({"check": f"{p} {_short(matcher)}", "ok": ok, "detail": v.mask(why)})
        for h, matcher in (exp.get("headers") or {}).items():
            val = r.headers.get(h)
            ok, why = check(val, v.sub(matcher), val is not None)
            sr.assertions.append({"check": f"header {h} {_short(matcher)}", "ok": ok, "detail": why})
        if "body_contains" in exp:
            needle = str(v.sub(exp["body_contains"]))
            sr.assertions.append({"check": f"body contains {needle!r}", "ok": needle in body_text, "detail": ""})
        if "max_ms" in exp:
            ok = sr.ms <= float(exp["max_ms"])
            sr.assertions.append({"check": f"responds within {exp['max_ms']} ms", "ok": ok, "detail": "" if ok else f"took {sr.ms} ms"})
        if exp.get("schema", True) and self.spec is not None and is_json:
            sr.assertions.extend(self._schema_check(method, template, r.status_code, body))
        for var, src in (step.get("save") or {}).items():
            src = str(src)
            if src == "status":
                val, found = r.status_code, True
            elif src.lower().startswith("header:"):
                val = r.headers.get(src.split(":", 1)[1].strip())
                found = val is not None
            else:
                found, val = json_path(body, src) if is_json else (False, None)
            if not found:
                sr.assertions.append({"check": f"save {var} from {src}", "ok": False, "detail": "not in the response"})
            else:
                v.values[var] = val
                sr.saved[var] = v.mask(val)
        sr.status = "passed" if all(a["ok"] for a in sr.assertions) else "failed"
        return sr

    def _schema_check(self, method: str, template: str, status: int, body: Any) -> list[dict]:
        from k11_contract_qa.conformance import _documented, to_json_schema

        op = self.spec.find(f"{method} {template.split('?')[0]}")
        if op is None:
            return []
        key, resp = _documented(op, status)
        if key is None:
            # an undocumented status is worth knowing but not a failure on its own (the status check decides)
            return [{"check": "status documented in the spec", "ok": True, "warn": True,
                     "detail": f"{status} is not documented for {op.key}"}]
        schema = None
        for media, mt in ((resp or {}).get("content") or {}).items():
            if "json" in media and isinstance(mt, dict):
                schema = mt.get("schema")
        if schema is None:
            return []
        errs = list(Draft202012Validator(to_json_schema(schema)).iter_errors(body))[:5]
        if not errs:
            return [{"check": f"matches the {key} response schema", "ok": True, "detail": ""}]
        detail = "; ".join(
            f"{''.join(f'[{x}]' if isinstance(x, int) else f'.{x}' for x in e.absolute_path).lstrip('.') or '(body)'}: {e.message[:120]}"
            for e in errs)
        return [{"check": f"matches the {key} response schema", "ok": False, "detail": detail}]

    def _db_step(self, step: dict, v: Vars, index: int, cleanup: bool) -> StepResult:
        sql = str(step["db"]).strip()
        dbname = step.get("database") or next(iter(self.cfg.databases), None)
        sr = StepResult(str(step.get("name") or ("cleanup: " if cleanup else "") + sql.split("\n")[0][:60]), "db", cleanup=cleanup)
        params = v.sub(step.get("params") or {})
        write = not db.READ_ONLY.match(db._strip_literals(sql))
        if write:
            if not cleanup:
                sr.status, sr.error = "error", "Steps may only read from the database; put writes in fixtures or cleanup"
                return sr
            ok, why = self._db_write_ok(dbname)
            if not ok:
                sr.status, sr.error = "blocked", why
                return sr
        mask_cols = (self.cfg.databases.get(dbname or "", {}) or {}).get("mask", [])
        sr.query = {"database": dbname, "sql": sql, "params": v.mask(params)}
        started = time.perf_counter()
        try:
            res = db.run(self._db_url(dbname, v), self.cfg.root, sql, params, write=write)
        except db.DbError as e:
            sr.status, sr.error = "error", v.mask(str(e))
            return sr
        except Exception as e:  # noqa: BLE001 - driver errors
            sr.status, sr.error = "failed", v.mask(f"{type(e).__name__}: {e}")[:300]
            return sr
        sr.ms = round((time.perf_counter() - started) * 1000)
        rows = res["rows"]
        sr.response = {"rowcount": res["rowcount"], "rows": db.mask_rows(rows[:20], mask_cols), "truncated": res["truncated"]}
        exp = step.get("expect") or {}
        if "rows" in exp:
            ok, why = check(len(rows) if not write else res["rowcount"], v.sub(exp["rows"]))
            sr.assertions.append({"check": f"rows {_short(exp['rows'])}", "ok": ok, "detail": why})
        if "row" in exp:
            first = rows[0] if rows else None
            for col, matcher in (exp["row"] or {}).items():
                found = first is not None and col in first
                ok, why = check(first.get(col) if found else None, v.sub(matcher), found)
                shown = why if col.lower() not in {c.lower() for c in mask_cols} else ("" if ok else "value differs (masked)")
                sr.assertions.append({"check": f"{col} {_short(matcher) if col.lower() not in {c.lower() for c in mask_cols} else '(masked)'}",
                                      "ok": ok, "detail": shown})
        for want in exp.get("rows_include") or []:
            want = v.sub(want)
            ok = any(all(check(r.get(k), m, k in r)[0] for k, m in want.items()) for r in rows)
            sr.assertions.append({"check": f"a row matches {_short(want)}", "ok": ok, "detail": "" if ok else "no matching row"})
        for var, src in (step.get("save") or {}).items():
            src = str(src)
            if src == "rowcount":
                v.values[var] = len(rows)
            elif src.startswith("row."):
                col = src[4:]
                if not rows or col not in rows[0]:
                    sr.assertions.append({"check": f"save {var} from {src}", "ok": False, "detail": "no such column/row"})
                    continue
                v.values[var] = rows[0][col]
            sr.saved[var] = MASK if src[4:].lower() in {c.lower() for c in mask_cols} else v.values.get(var)
        sr.status = "passed" if all(a["ok"] for a in sr.assertions) else "failed"
        return sr


def _fatal(sr: StepResult) -> bool:
    """Stop the flow after a blocked/error step, a wrong status, a server error or a value that couldn't be saved."""
    if sr.status in ("blocked", "error"):
        return True
    return any(not a["ok"] and (a["check"].startswith(("status", "save ", "rows")) or a["check"] == "no server error")
               for a in sr.assertions)


def _short(m: Any) -> str:
    s = json.dumps(m, default=str) if not isinstance(m, str) else m
    return s if len(s) <= 60 else s[:57] + "..."


def _redact(headers: dict, v: Vars) -> dict:
    out = {}
    for k, val in headers.items():
        out[k] = MASK if k.lower() in ("authorization", "cookie", "x-api-key", "proxy-authorization") else v.mask(val)
    return out


def flow_files(root: Path, targets: list[str] | None) -> list[Path]:
    from k11_contract_qa.spec import inside

    base = root / "tests" / "api"
    if not targets:
        return sorted(p for p in base.rglob("*.y*ml") if p.suffix in (".yaml", ".yml")) if base.is_dir() else []
    out = []
    for t in targets:
        p = inside(root, t)
        out += sorted(x for x in p.rglob("*.y*ml") if x.suffix in (".yaml", ".yml")) if p.is_dir() else [p]
    return out


def run_files(cfg: Config, files: list[Path], environment: str | None = None, tags: list[str] | None = None) -> list[FlowResult]:
    from k11_contract_qa.spec import load_spec

    spec = load_spec(cfg.root, cfg.spec) if cfg.spec else None
    runner = Runner(cfg, environment, spec)
    results = []
    try:
        for f in files:
            rel = f.relative_to(cfg.root).as_posix() if f.is_relative_to(cfg.root) else f.name
            try:
                flow = load_flow(f)
            except FlowError as e:
                results.append(FlowResult(f.stem, rel, runner.env_name, "error", error=str(e)))
                continue
            if tags and not set(tags) & set(flow.get("tags") or []):
                continue
            results.append(runner.run_flow(flow, rel))
    finally:
        runner.close()
    return results
