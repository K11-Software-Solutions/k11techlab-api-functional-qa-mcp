"""Flows against the demo shop: functional bugs, database checks, negative tests, guardrails, reports, MCP, CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from k11_api_qa import __version__, config, export, generate, report
from k11_api_qa.cli import main as cli
from k11_api_qa.runner import Runner, flow_files, run_files
from k11_api_qa.server import create_server
from k11_contract_qa.spec import load_spec


def run(project, env=None):
    cfg = config.load(project)
    return {r.name: r for r in run_files(cfg, flow_files(project, None), env)}


def test_cancel_flow_passes_with_db_checks(project, shop):
    r = run(project)["Customer cancels an order and it is soft-deleted"]
    assert r.status == "passed", [(s.name, s.status, s.error, s.assertions) for s in r.steps]
    db_steps = [s for s in r.steps if s.kind == "db" and not s.cleanup]
    assert [s.response["rows"][0]["status"] for s in db_steps] == ["pending", "cancelled"]
    assert all(s.status == "passed" for s in r.steps if s.cleanup)


def test_total_bug_found_and_database_shows_where(project, shop):
    r = run(project)["Order total is quantity times price"]
    assert r.status == "failed"
    post, db_check, history = [s for s in r.steps if s.kind in ("call", "db") and not s.cleanup]
    assert post.status == "failed"
    assert any("expected 85.0, got 42.5" in a["detail"] for a in post.assertions if not a["ok"])
    assert db_check.status == "passed" and history.status == "passed"  # only the POST response is wrong


def test_negative_tests_find_server_errors_and_clean_up(project, shop):
    cfg = config.load(project)
    spec = load_spec(project, "openapi.yaml")
    flow = generate.negative_flows(spec, spec.find("POST /orders"), {"customerId": "${fixture.customer.id}"}, ["customer"])
    runner = Runner(cfg, None, spec)
    try:
        res = runner.run_flow(flow)
    finally:
        runner.close()
    main = [s for s in res.steps if not s.cleanup and s.kind == "call"]
    five_xx = sorted(s.name for s in main if s.response and s.response["status"] >= 500)
    assert five_xx == ["missing required field 'items[0].qty' is rejected", "wrong type for 'items[0].qty' is rejected"]
    assert len(main) == 18 and sum(s.status == "passed" for s in main) == 16
    cleanup = [s for s in res.steps if s.cleanup and s.kind == "call"]
    assert len(cleanup) == 3 and all(s.status == "passed" for s in cleanup)


def test_production_blocks_writes_but_allows_reads(project, shop):
    r = run(project, "prod")
    assert r["Order total is quantity times price"].status == "blocked"
    fixture = r["Order total is quantity times price"].steps[0]
    assert fixture.status == "blocked" and "production" in fixture.error


def test_reports_and_junit(project, shop):
    results = list(run(project).values())
    paths = report.write(results, "local", project / ".qa-mcp" / "out")
    md = paths["markdown"].read_text("utf8")
    assert "**1 passed**, 1 failed" in md and "expected 85.0, got 42.5" in md
    assert "test-token" not in paths["json"].read_text("utf8")
    junit = paths["junit"].read_text("utf8")
    assert 'failures="1"' in junit and "expected 85.0, got 42.5" in junit


def test_export_pytest_compiles_and_runs(project, shop, tmp_path):
    flow = yaml.safe_load((project / "tests/api/order_total.yaml").read_text())
    flow.pop("fixtures")
    flow["vars"] = {"fixture": {"customer": {"id": "c-export"}}}
    import sqlite3
    con = sqlite3.connect(project / "shop.db")
    con.execute("INSERT INTO customers VALUES ('c-export', 'e@x.io', 'Export')")
    con.commit()
    con.close()
    code = export.to_pytest(flow, "tests/api/order_total.yaml", shop.url)
    compile(code, "exported.py", "exec")
    f = tmp_path / "test_exported.py"
    f.write_text(code, "utf8")
    env = {**os.environ, "API_BASE_URL": shop.url, "API_TOKEN": "test-token", "TEST_DB_URL": f"sqlite:///{project / 'shop.db'}"}
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(f)], env=env,
                         capture_output=True, text=True)
    assert "1 failed" in out.stdout and "42.5" in out.stdout, out.stdout[-1500:]  # the exported test finds the same bug


def test_cli_run(project, shop, tmp_path, monkeypatch):
    out = tmp_path / "gh_out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert cli(["run"]) == 1
    o = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert o["passed"] == "1" and o["failed"] == "1"
    assert cli(["run", "tests/api/cancel_order.yaml"]) == 0
    assert cli(["run", "--fail-on", "never"]) == 0


def test_mcp_tools(project, shop):
    async def go():
        async with create_connected_server_and_client_session(create_server()._mcp_server) as c:
            info = await c.initialize()
            assert (info.serverInfo.name, info.serverInfo.version) == ("k11-api-functional-qa", __version__)
            names = sorted(t.name for t in (await c.list_tools()).tools)
            assert names == ["call_api", "draft_flow", "export_pytest", "generate_negative_tests", "list_environments",
                             "query_db", "run_flows", "write_flow"]

            def j(r):
                assert not r.isError, r.content[0].text
                return json.loads(r.content[0].text)

            envs = j(await c.call_tool("list_environments", {}))
            assert envs["environments"]["prod"]["writes"] is False and len(envs["flows"]) == 2
            d = j(await c.call_tool("draft_flow", {"story": "As a customer I want to place an order and see it in my order history"}))
            assert d["candidate_operations"][0]["endpoint"] in ("POST /orders", "GET /orders")
            neg = j(await c.call_tool("generate_negative_tests", {"endpoint": "POST /orders",
                                                                   "base_values": {"customerId": "${fixture.customer.id}"},
                                                                   "fixtures": ["customer"]}))
            assert neg["cases"] == 18
            w = j(await c.call_tool("write_flow", {"path": neg["path"], "content": neg["flow"]}))
            assert w["written"].startswith("tests/api/negative_post_orders")
            assert (await c.call_tool("write_flow", {"path": "../evil.yaml", "content": neg["flow"]})).isError
            assert (await c.call_tool("write_flow", {"path": "bad.yaml", "content": "name: x\nsteps: []"})).isError
            ran = j(await c.call_tool("run_flows", {"tags": ["negative"]}))
            assert ran["summary"]["server_errors"] == 2 and ran["report"]["junit"].endswith(".xml")
            blocked = j(await c.call_tool("call_api", {"call": "POST /customers", "environment": "prod",
                                                       "body": {"email": "a@b.io", "name": "Al"}}))
            assert blocked["status"] == "blocked"
            got = j(await c.call_tool("call_api", {"call": "POST /customers", "body": {"email": "z@b.io", "name": "Zed"}}))
            assert got["response"]["status"] == 201 and got["request"]["headers"]["Authorization"] == "•••"
            rows = j(await c.call_tool("query_db", {"sql": "SELECT email, name FROM customers WHERE name = :n", "params": {"n": "Zed"}}))
            assert rows["rows"] == [{"email": "•••", "name": "Zed"}]
            assert (await c.call_tool("query_db", {"sql": "DELETE FROM customers"})).isError
            for evil in ("GET https://attacker.example/steal", "GET http://attacker.example"):
                other = j(await c.call_tool("call_api", {"call": evil}))
                assert other["status"] == "error" and "must stay on 127.0.0.1" in other["error"], other
                assert other["request"] is None  # nothing was sent
            same_host = j(await c.call_tool("call_api", {"call": "GET //attacker.example/steal"}))
            assert same_host["request"]["url"].startswith(shop.url + "/")  # treated as a path on the same host
            ex = j(await c.call_tool("export_pytest", {"path": "tests/api/cancel_order.yaml"}))
            assert "def test_customer_cancels_an_order" in ex["code"]

    anyio.run(go)
