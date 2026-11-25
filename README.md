# k11 API Functional QA — MCP server & Claude Code plugin

<!-- mcp-name: io.github.K11-Software-Solutions/api-functional-qa -->

**Functional API tests that prove the API did the right thing, down to the database row.**

Contract checks tell you an API still has the right *shape*. They don't tell you whether placing an order charges the right amount, whether cancelling really soft-deletes the row, or whether a malformed request crashes the server. This MCP server lets Claude (or any MCP client) write and run those tests with you:

- **Ask before you build.** Before drafting a flow, `review_user_story` lists what a careful tester would ask: a rejection with no status code, a delete that doesn't say whether the row is kept, no expected result without credentials, an endpoint or field the spec doesn't have. The agent stops and asks instead of guessing.
- **User story → multi-step test flow.** Chain calls (`POST /orders` → save the `id` → `GET /orders/{id}` → `DELETE`) with readable YAML and value checks.
- **Database checks in the same flow.** After the API call, check the row really changed: `SELECT status, deleted_at FROM orders WHERE id = :orderId`. Read-only by default, with masked columns.
- **Negative and boundary tests from the spec,** including nested fields and array items: missing required fields, wrong types, values outside enums and min/max/length limits, no credentials, unknown IDs. Every case must get a 4xx, **never a 5xx**.
- **Evidence for every step:** the request, response, query, rows and each check, in Markdown (PR comment), JSON and JUnit XML for CI dashboards.
- **Guardrails:** data-changing calls are refused on production-like environments, database writes only in fixtures and cleanup on allowlisted test databases, secrets only from environment variables and masked everywhere, and requests never leave the environment's host.
- **No lock-in:** export any flow as plain pytest + httpx code.

Built on the parsers of [k11 API Contract QA](https://github.com/K11-Software-Solutions/k11techlab-api-contract-qa-mcp) (OpenAPI 3.x and Swagger 2.0 for test generation and response-schema checks).

---

## See it find real bugs

The demo shop API (`demo/shop/`) has two planted bugs. Two hand-written flows and one generated negative suite found them, plus one I didn't plant.

**1. The order total is wrong only in the API response.** `order_total.yaml` orders two bags at 42.50:

> ❌ **Order total is quantity times price**
> - **place an order for two bags**: failed. Response: HTTP 201. ✗ `$.total 85.0`: expected 85.0, got 42.5
> - **the database stores 85.00**: passed
> - **order history shows 85.00**: passed

The database check shows exactly where the bug is: the stored total is right, and only the `POST /orders` response is wrong.

**2. Malformed input crashes the server.** `generate_negative_tests("POST /orders")` produced 18 cases from the spec. 16 behaved correctly, and two returned **HTTP 500**:

> - ✗ wrong type for `items[0].qty` is rejected: got 500. *A 5xx is a bug.*
> - ✗ missing required field `items[0].qty` is rejected: got 500. *A 5xx is a bug.*

The second crash wasn't planted. It's the same code path, and the generator found it because it tests nested array items, not just top-level fields.

**3. The soft delete works, proven in the database.** `cancel_order.yaml` passes all 7 steps, including checks that the row is kept with `status = cancelled` and a `deleted_at` timestamp.

## A flow

```yaml
name: Customer cancels an order and it is soft-deleted
fixtures: [customer]                       # seeded in the test database, removed afterwards
steps:
  - name: place an order
    call: POST /orders
    body: { customerId: "${fixture.customer.id}", channel: web, items: [{ sku: HAT-2, qty: 1 }] }
    expect:
      status: 201
      json: { "$.status": pending, "$.total": 15.0, "$.items": { length: 1 } }
    save: { orderId: "$.id" }

  - name: the row is kept and marked deleted
    db: SELECT status, deleted_at FROM orders WHERE id = :orderId
    params: { orderId: "${orderId}" }
    expect: { rows: 1, row: { status: cancelled, deleted_at: { ne: null } } }

cleanup:
  - db: DELETE FROM orders WHERE id = :orderId
    params: { orderId: "${orderId}" }
```

- **Checks** (`expect`): `status` (`201`, `"2xx"`, `[400, 422]`), `json` paths (`$.items[0].qty`, `$.items[*].sku`, `$.items.length`), `headers`, `body_contains`, `max_ms`, and the response schema from the spec (on by default). Values compare as literals or with operators: `eq ne gt gte lt lte approx contains matches length type in exists empty`.
- **Variables:** `save` from JSON paths, headers (`header:Location`) or `status`; use them as `${orderId}`. Generators: `${uuid}`, `${random_email}`, `${random_int}`, `${now}`, `${today}`. Secrets: `${ENV:NAME}`.
- **Failures:** a step that only fails value checks lets the flow continue, to gather more evidence. A wrong status, a 5xx, a blocked write or a missing saved value stops it. `continue_on_failure: true` runs every step (generated negative suites use it). Cleanup always runs.

## Ask before you build

Generating a flow from an unclear story doesn't remove the ambiguity; it hides it in the YAML, as guessed status codes,
guessed database checks and guessed error bodies. So `/api-functional-qa:api-story-test` starts with `review_user_story`,
and if the story has gaps the agent asks you and waits:

```text
As a customer, I want to cancel an order.
Given I have an order
When I send DELETE /refunds/{id}
Then it is removed
```

> - **failure-path:** What should happen when it goes wrong: invalid input, no permission, an expired session, a server error?
> - **delete-semantics:** When it's deleted or cancelled, is the record removed (hard delete) or kept and marked (soft delete: which status or column)? And what does GET return for it afterwards?
> - **access:** The story is for a customer. What should happen without credentials, and when one user calls with another user's IDs: 401, 403 or 404?
> - **spec-mismatch:** The story mentions DELETE /refunds/{id}, but the spec has no such operation. Is the spec out of date, or should the flow use a different endpoint?

Each question says why it matters and quotes the words that raised it. The rules are deterministic, and there are two sets:

- **Shared with [k11 Playwright QA](https://github.com/K11-Software-Solutions/k11techlab-playwright-qa-mcp) and [k11 Selenium QA](https://github.com/K11-Software-Solutions/k11techlab-selenium-qa-mcp):** missing acceptance criteria, outcomes that can't be checked, no role, untestable words, limits without numbers, no failure path, missing preconditions, and (with `context`) terms the context never explains. All three repositories test against the same `tests/fixtures/review_cases.json`.
- **API-specific:**

  | Kind | Asked when |
  |---|---|
  | `error-contract` | Something is rejected, but the outcome gives no 4xx or 5xx status. |
  | `delete-semantics` | Something is deleted or cancelled, but nothing says whether the record is kept. |
  | `access` | A signed-in role, but nothing about calls without credentials or on another user's data. |
  | `repeat` | A repeated or duplicate request, but no defined result. |
  | `spec-mismatch` | With a spec: an endpoint or field name the spec doesn't have. |

A clear story gets no questions; that's tested too. If you tell the agent to go ahead anyway, each open question goes into
the flow as a `# OPEN QUESTION:` comment and into the final report.

## Tools

| Tool | What it does |
|---|---|
| `list_environments` | Environments and whether each allows writes (and why), auth profiles, databases, fixtures, flows. |
| `review_user_story` | Questions to answer before writing a flow, checked against the spec; `ready: false` means ask, don't guess. |
| `draft_flow` | User story → scenarios, the spec operations most likely involved, a flow skeleton, and a review summary. |
| `generate_negative_tests` | Negative and boundary cases for one operation, with real base values from fixtures. |
| `write_flow` | Validates and saves a flow under `tests/api/`. |
| `run_flows` | Runs flows (all, by path or by tag) with full evidence and reports. |
| `call_api` | One request with the same guardrails, to explore an API while writing tests. |
| `query_db` | One read-only query against a test database, with masking. |
| `export_pytest` | A flow as plain pytest + httpx code. |

## Configure: `.qa-mcp/api-test.yaml`

> All K11 QA tools keep their settings, baselines and reports in `.qa-mcp/`. [What each file is for, and what to commit](https://github.com/K11-Software-Solutions/k11techlab-release-gate-mcp/blob/main/docs/qa-mcp-folder.md).

```yaml
spec: openapi.yaml
default_environment: local
environments:
  local:   { base_url: http://localhost:8080 }
  staging: { base_url: https://staging.example.com, allow_writes: true }
  prod:    { base_url: https://api.example.com }        # read-only: writes refused
auth:
  default: { type: bearer, token: "${ENV:API_TOKEN}" }
  # basic: {username, password} · api_key: {header, value} · oauth2: {token_url, client_id, client_secret, scope}
databases:
  main: { url: "${ENV:TEST_DB_URL}", allow_writes: true, mask: [email, card_number] }
fixtures:
  customer:
    db: main
    insert: { table: customers, values: { id: "${uuid}", email: "${random_email}", name: Test Customer } }
    delete: { table: customers, where: { id: "${id}" } }
```

**Databases:** SQLite (built in), PostgreSQL (`pip install "k11-api-functional-qa-mcp[postgres]"`), MySQL/MariaDB (`[mysql]`) and SQL Server (`[mssql]`).

## Guardrails

| Risk | What happens |
|---|---|
| Changing data in production | `POST/PUT/PATCH/DELETE` are refused on environments named like production (`prod`, `production`, `live`) unless they set both `allow_writes: true` and `production_writes_acknowledged: true`. Other environments must be local, named like a test environment, or set `allow_writes: true`. |
| Writing to a database | `db` steps may only read (SELECT/WITH/SHOW/EXPLAIN; no write verbs anywhere, no `FOR UPDATE`), on a read-only connection inside a rolled-back transaction. Writes happen only in fixtures and cleanup, on databases with `allow_writes: true`. |
| SQL injection | Values go in `:named` parameters; `${...}` inside SQL text is refused, as is more than one statement. Table and column names in fixtures are validated. |
| Leaking secrets | Credentials come from `${ENV:NAME}`. Authorization headers show as `•••`, and any secret value is masked in requests, responses, errors and reports, as are database columns listed under `mask`. |
| Sending credentials elsewhere | Requests must stay on the environment's host; a flow can't send your token to another URL. |
| Runaway queries | At most 1,000 rows are read per query, with timeouts on every connection and request. |

## In CI

```bash
k11-api-functional-qa-mcp run --env staging
```

It writes `api-test-report.md` (PR comment), `api-test-report.json` (evidence) and `api-test-junit.xml`, sets GitHub Actions outputs and exits non-zero when a flow fails (`--fail-on any|failed|never`). See [`docs/api-tests.yml`](docs/api-tests.yml).

## Install

Requires Python 3.10+.

```text
/plugin marketplace add K11-Software-Solutions/k11techlab-api-functional-qa-mcp
/plugin install api-functional-qa@k11-api-functional-qa
```

The plugin adds `/api-story-test` and `/api-negative-tests`. To add only the MCP server:

```bash
claude mcp add api-functional-qa -- uvx k11-api-functional-qa-mcp
```

Claude Desktop and other clients: run `uvx k11-api-functional-qa-mcp` with `QA_MCP_ROOT` set to your project.

## Try the demo

```bash
python demo/shop/server.py --port 8080
SHOP_URL=http://127.0.0.1:8080 SHOP_TOKEN=test-token QA_MCP_ROOT=demo/shop k11-api-functional-qa-mcp run
```

## Scope

This server is for functional REST testing with database checks. Related K11 tools:
- **Contract and breaking-change checks** (REST, GraphQL, gRPC, AsyncAPI, SOAP): [k11 API Contract QA](https://github.com/K11-Software-Solutions/k11techlab-api-contract-qa-mcp)
- **UI tests:** [Playwright QA](https://github.com/K11-Software-Solutions/k11techlab-playwright-qa-mcp) and [Selenium QA](https://github.com/K11-Software-Solutions/k11techlab-selenium-qa-mcp)
- **LLM red-teaming:** [LLM Assure](https://github.com/K11-Software-Solutions/k11techlab-llm-assurance-toolkit)

Not covered: load testing (use k6 or Gatling) and deep fuzzing (use Schemathesis).

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # bin/pip on macOS/Linux
pytest   # values, guardrails, flows against the demo shop, negative tests, export, CLI, MCP, story review (51)
```

## Roadmap

- GraphQL, gRPC and SOAP calls as flow steps
- Java export (REST Assured)
- MongoDB checks
- Schema-driven test data (realistic values per field format)

---

Built by [K11 Software Solutions](https://k11softwaresolutions.com). Part of the K11 Tech Lab agentic QA series. MIT licensed.
