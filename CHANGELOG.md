# Changelog

## 0.2.1 (26 November 2026)

- **Fix:** the `repeat` question fired on any "already", so "Cancelling is rejected when it already shipped" got a question about duplicate requests, and that hid the `error-contract` question the story needed. "Already" now counts only when it says the request was made before ("already registered", "already used", "already exists"), not when it describes the record's state. Found in the 0.2.0 live check; covered by a regression test.

## 0.2.0 (25 November 2026): ask before you build

- **Ask before you build:** new `review_user_story` tool. Before a flow is drafted, it lists the questions a careful tester would ask, each with why it matters and the words that raised it. If there are any, the agent asks you and waits instead of guessing.
- **Shared rules,** identical to k11 Playwright QA 0.4.1 and k11 Selenium QA 0.3.0, tested against the same `tests/fixtures/review_cases.json`: missing acceptance criteria, outcomes that can't be checked, no role, untestable words, limits without numbers, no failure path, missing preconditions, and (with `context`) undefined terms.
- **API rules:**
  - `error-contract`: a rejection with no status code;
  - `delete-semantics`: hard or soft delete;
  - `access`: no expected result without credentials or for another user's data;
  - `repeat`: a duplicate request with no defined result;
  - `spec-mismatch`: with a spec, an endpoint or field the spec doesn't have.

  Rejections and repeats are judged per When/Then exchange, so a status code elsewhere in the story doesn't count as an answer.
- `/api-story-test` now starts with the review. Open questions the user chooses to skip go into the flow as `# OPEN QUESTION:` comments and into the final report.
- `draft_flow` returns a short `review` summary, so open questions are visible even when the review is skipped.

## 0.1.1 (13 November 2026)

- The MCP server no longer logs request URLs to stderr. URLs can contain IDs or query-string credentials that bypass masking; all evidence is reported, masked, through the tools.

## 0.1.0 (13 November 2026)

- First release: YAML API test flows with chained calls, value/schema/timing checks, read-only database checks, fixtures and cleanup; negative and boundary tests generated from OpenAPI/Swagger (including nested fields); guardrails for production, database writes, SQL parameters, secrets and hosts; Markdown/JSON/JUnit reports; pytest export; 8 MCP tools and a CLI.
