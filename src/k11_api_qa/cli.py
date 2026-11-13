"""Command line: start the MCP server (default) or run flows in CI."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__

USAGE = f"""k11-api-functional-qa-mcp {__version__}

Usage:
  k11-api-functional-qa-mcp                      start the MCP server on stdio
  k11-api-functional-qa-mcp run [paths] [opts]   run API test flows and write reports
  k11-api-functional-qa-mcp --version

run options:
  --env NAME           environment from .qa-mcp/api-test.yaml (default: default_environment)
  --tag TAG            only flows with this tag (repeatable)
  --report-dir PATH    default .qa-mcp/api-test-report
  --fail-on LEVEL      any (default: fail on failed, blocked or error) | failed | never

The project is QA_MCP_ROOT or the current directory.
"""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from .server import serve

        serve()
        return 0
    if argv[0] in ("--version", "-v"):
        print(__version__)
        return 0
    if argv[0] in ("--help", "-h", "help"):
        print(USAGE)
        return 0
    if argv[0] != "run":
        print(f"Unknown command: {argv[0]}\n\n{USAGE}", file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(prog="k11-api-functional-qa-mcp run")
    p.add_argument("paths", nargs="*")
    p.add_argument("--env")
    p.add_argument("--tag", action="append")
    p.add_argument("--report-dir", default=".qa-mcp/api-test-report")
    p.add_argument("--fail-on", default="any", choices=["any", "failed", "never"])
    a = p.parse_args(argv[1:])

    from k11_contract_qa.spec import SpecError, inside

    from . import config, report
    from .runner import flow_files, run_files

    root = Path(os.environ.get("QA_MCP_ROOT") or os.getcwd()).resolve()
    try:
        cfg = config.load(root)
        files = flow_files(root, a.paths or None)
        if not files:
            print("No flows found in tests/api/", file=sys.stderr)
            return 1
        results = run_files(cfg, files, a.env, a.tag)
        env = results[0].environment if results else (a.env or cfg.default_environment)
        paths = report.write(results, env, inside(root, a.report_dir))
    except (SpecError, ValueError, OSError) as e:
        print(f"run failed: {e}", file=sys.stderr)
        return 1
    c = report.counts(results)
    print(f"{c['passed']}/{c['flows']} flows passed ({c['failed']} failed, {c['blocked']} blocked, {c['error']} errors, "
          f"{c['server_errors']} server errors). Report: {paths['markdown']}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf8") as fh:
            fh.write(f"passed={c['passed']}\nfailed={c['failed']}\nblocked={c['blocked']}\nerrors={c['error']}\n"
                     f"report={paths['markdown']}\njunit={paths['junit']}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf8") as fh:
            fh.write(paths["markdown"].read_text("utf8") + "\n")
    bad = {"any": c["failed"] + c["blocked"] + c["error"], "failed": c["failed"], "never": 0}[a.fail_on]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
