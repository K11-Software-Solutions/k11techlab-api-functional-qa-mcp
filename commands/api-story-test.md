---
description: Turn a user story into a passing multi-step API test flow, with database checks
argument-hint: <user story or acceptance criteria> [environment]
---

Write and run a functional API test for the user story below, using the api-functional-qa MCP tools.

Input: $ARGUMENTS

1. Call `list_environments`. Use the default environment unless one is named. If it doesn't allow writes and the story needs them, stop and say so; never try to get around it.
2. Call `draft_flow` with the story. Read the candidate operations, and use `call_api` (read-only calls first) and `query_db` to learn real response shapes and table names.
3. Write the flow:
   - chain calls by saving IDs (`save: { orderId: "$.id" }`) and reusing them (`${orderId}`);
   - check values, not just status codes (amounts, states, counts);
   - add a `db` check after each data-changing call to prove the change was stored (read-only SELECT with `:params`);
   - use fixtures for test data, and add cleanup for anything the flow creates;
   - secrets only as `${ENV:NAME}`.
4. Save it with `write_flow` (ask before overwriting) and run it with `run_flows`.
5. If it fails, decide from the evidence whether the test or the API is wrong. Fix test mistakes. If the API is wrong, report the bug with the request, response and database evidence; never weaken the check to make it pass.

Finish with the flow path, what each step proves, and any bug found.
