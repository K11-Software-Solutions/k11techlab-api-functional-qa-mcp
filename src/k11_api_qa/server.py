"""MCP server: functional API testing with database checks and evidence."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from mcp.server.fastmcp import FastMCP

from . import __version__, config, db, export, generate, report
from .runner import FlowError, Runner, flow_files, run_files, validate_flow


def project_root() -> Path:
    return Path(os.environ.get("QA_MCP_ROOT") or os.getcwd()).resolve()


def _spec(cfg: config.Config, spec: str | None):
    from k11_contract_qa.spec import load_spec

    src = spec or cfg.spec
    if not src:
        raise ValueError("No spec given and none configured (set 'spec:' in .qa-mcp/api-test.yaml)")
    return load_spec(cfg.root, src)


def _brief(results) -> list[dict]:
    out = []
    for r in results:
        failed = [{"step": s.name, "status": s.status, "error": s.error,
                   "failed_checks": [f"{a['check']}: {a.get('detail', '')}" for a in s.assertions if not a["ok"]],
                   "request": s.request, "response": s.response, "query": s.query}
                  for s in r.steps if s.status != "passed"]
        out.append({"flow": r.name, "file": r.file, "status": r.status, "ms": r.ms, "error": r.error,
                    "steps": len([s for s in r.steps if not s.cleanup]), "problems": failed})
    return out


def create_server() -> FastMCP:
    mcp = FastMCP(
        "k11-api-functional-qa",
        instructions=(
            "Functional API testing. Tests are YAML flows in tests/api/: chained HTTP calls ('call: POST /orders'), "
            "values saved with 'save' and reused as ${name}, checks under 'expect' (status, json paths, headers, "
            "schema, time), read-only database checks ('db: SELECT ...' with :params), fixtures and cleanup. "
            "Use list_environments first, draft_flow for a user story, generate_negative_tests for rejection cases, "
            "write_flow to save, run_flows to run. Data-changing calls are refused on environments that don't allow "
            "writes; never put secrets in flows, use ${ENV:NAME}."
        ),
    )

    @mcp.tool(title="List environments")
    def list_environments() -> dict:
        """Environments (base URL, whether writes are allowed and why), auth profiles, databases and fixtures from
        .qa-mcp/api-test.yaml, plus the flows in tests/api/."""
        cfg = config.load(project_root())
        out = config.summary(cfg)
        out["flows"] = [p.relative_to(cfg.root).as_posix() for p in flow_files(cfg.root, None)]
        return out

    @mcp.tool(title="Draft flow from a user story")
    def draft_flow(story: str, spec: Optional[str] = None) -> dict:
        """Turn a user story or acceptance criteria into scenarios, the spec operations most likely involved, and a
        flow skeleton to complete (chain IDs with save/${...}, add value and db checks)."""
        cfg = config.load(project_root())
        s = None
        if spec or cfg.spec:
            s = _spec(cfg, spec)
        return generate.draft(story, s)

    @mcp.tool(title="Generate negative tests")
    def generate_negative_tests(endpoint: str, spec: Optional[str] = None, base_values: Optional[dict] = None,
                                fixtures: Optional[list[str]] = None) -> dict:
        """Negative and boundary tests for one operation (e.g. 'POST /orders'), derived from the spec, including nested
        fields and array items: missing required fields, wrong types, values outside enums and min/max/length/item
        limits, missing parameters, no credentials, unknown IDs. Every case must get a 4xx, never a 5xx; boundary cases
        that must be accepted clean up after themselves. base_values supplies real values for the valid parts
        (e.g. {"customerId": "${fixture.customer.id}"}) with fixtures=["customer"]. Save the result with write_flow."""
        cfg = config.load(project_root())
        s = _spec(cfg, spec)
        if not s.http:
            raise ValueError(f"Negative tests are generated for REST APIs (OpenAPI/Swagger); this spec is {s.format}")
        op = s.find(endpoint)
        if op is None:
            raise ValueError(f"{endpoint} is not in the spec")
        unknown = [f for f in (fixtures or []) if f not in cfg.fixtures]
        if unknown:
            raise ValueError(f"Unknown fixture(s) {unknown}. Known: {sorted(cfg.fixtures) or ['(none)']}")
        flow = generate.negative_flows(s, op, base_values, fixtures)
        return {"path": generate.flow_file_name(op), "cases": len(flow["steps"]), "flow": generate.to_yaml(flow)}

    @mcp.tool(title="Write flow")
    def write_flow(path: str, content: str, overwrite: bool = False) -> dict:
        """Save a YAML flow under tests/api/ after validating it. Won't overwrite unless asked."""
        from k11_contract_qa.spec import inside

        root = project_root()
        rel = path.replace("\\", "/")
        if not rel.startswith("tests/api/"):
            rel = "tests/api/" + rel.lstrip("/")
        if not re.search(r"\.ya?ml$", rel):
            raise ValueError("Flows are .yaml files")
        target = inside(root, rel)
        api_dir = (root / "tests" / "api").resolve()
        if api_dir not in target.parents:
            raise ValueError("Flows can only be written inside tests/api/")
        try:
            validate_flow(yaml.safe_load(content), rel)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML: {str(e).splitlines()[0]}") from None
        if target.exists() and not overwrite:
            raise ValueError(f"{rel} exists. Pass overwrite=true to replace it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, "utf8")
        return {"written": rel}

    @mcp.tool(title="Run flows")
    def run_flows(paths: Optional[list[str]] = None, environment: Optional[str] = None,
                  tags: Optional[list[str]] = None, write_report: bool = True) -> dict:
        """Run flows (default: everything in tests/api/) against an environment. Returns per-flow results with the
        failing checks and the request/response or query evidence; writes Markdown, JSON and JUnit reports."""
        cfg = config.load(project_root())
        files = flow_files(cfg.root, paths)
        if not files:
            raise ValueError("No flows found. Write one with write_flow (tests/api/*.yaml).")
        results = run_files(cfg, files, environment, tags)
        env = results[0].environment if results else (environment or cfg.default_environment)
        out: dict[str, Any] = {"environment": env, "summary": report.counts(results), "flows": _brief(results)}
        if write_report:
            paths_out = report.write(results, env, cfg.root / ".qa-mcp" / "api-test-report")
            out["report"] = {k: p.relative_to(cfg.root).as_posix() for k, p in paths_out.items()}
        return out

    @mcp.tool(title="Call API")
    def call_api(call: str, environment: Optional[str] = None, path_params: Optional[dict] = None,
                 query: Optional[dict] = None, body: Optional[Any] = None, headers: Optional[dict] = None,
                 auth: Optional[str] = None) -> dict:
        """Make one request (e.g. call='GET /orders/{id}', path_params={'id': '1001'}) to explore an API while writing
        tests. Same guardrails as flows: writes only where allowed, secrets masked, schema checked when a spec is set."""
        cfg = config.load(project_root())
        step: dict[str, Any] = {"name": call, "call": call, "expect": {"status": {"not": "5xx"}}}
        for k, val in (("path", path_params), ("query", query), ("headers", headers)):
            if val:
                step[k] = val
        if body is not None:
            step["body"] = body
        if auth:
            step["auth"] = auth
        flow = validate_flow({"name": f"ad hoc: {call}", "steps": [step]})
        spec = _spec(cfg, None) if cfg.spec else None
        runner = Runner(cfg, environment, spec)
        try:
            res = runner.run_flow(flow)
        finally:
            runner.close()
        s = res.steps[0] if res.steps else None
        return {"environment": res.environment, "status": res.status, "error": res.error or (s.error if s else None),
                "request": s.request if s else None, "response": s.response if s else None,
                "checks": s.assertions if s else []}

    @mcp.tool(title="Query test database")
    def query_db(sql: str, params: Optional[dict] = None, database: Optional[str] = None) -> dict:
        """Run one read-only query (SELECT/WITH/SHOW/EXPLAIN) against a configured test database, with :named params.
        Masked columns are shown as •••. Use it to find test data and write db checks."""
        from .values import Vars

        cfg = config.load(project_root())
        name = database or next(iter(cfg.databases), None)
        if not name or name not in cfg.databases:
            raise ValueError(f"Unknown database '{name}'. Known: {sorted(cfg.databases) or ['(none)']}")
        v = Vars()
        res = db.run(str(v.sub(cfg.databases[name]["url"])), cfg.root, sql, params or {}, write=False)
        return {"database": name, "columns": res["columns"], "rowcount": res["rowcount"], "truncated": res["truncated"],
                "rows": db.mask_rows(res["rows"][:50], cfg.databases[name].get("mask", []))}

    @mcp.tool(title="Export flow to pytest")
    def export_pytest(path: str, write: bool = False) -> dict:
        """Turn a flow into a plain pytest + httpx test (database checks included), so the suite can live without this
        tool. Returns the code; with write=true saves it under tests/ (never overwriting)."""
        from k11_contract_qa.spec import inside

        from .runner import load_flow

        cfg = config.load(project_root())
        src = inside(cfg.root, path)
        flow = load_flow(src)
        _, env = cfg.environment(None)
        code = export.to_pytest(flow, src.relative_to(cfg.root).as_posix(), str(env.get("base_url", "http://localhost:8080")))
        out_rel = "tests/test_" + re.sub(r"[^a-z0-9]+", "_", src.stem.lower()).strip("_") + "_exported.py"
        if write:
            target = inside(cfg.root, out_rel)
            if target.exists():
                raise ValueError(f"{out_rel} exists; not overwriting")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, "utf8")
        return {"file": out_rel, "written": write, "code": code,
                "dependencies": "pip install pytest httpx (and psycopg[binary] for PostgreSQL checks)"}

    mcp._mcp_server.version = __version__  # FastMCP (mcp 1.x) would report the SDK's version
    return mcp


def serve() -> None:
    import logging

    # httpx logs every request URL at INFO; URLs can carry IDs or query-string secrets that our masking
    # never sees, so keep it quiet. Evidence is reported, masked, through the tools instead.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    create_server().run()


__all__ = ["create_server", "serve", "FlowError"]
