---
description: Turn a user story into a passing multi-step API test flow, with database checks, asking about anything unclear first
argument-hint: <user story or acceptance criteria> [environment]
---

Write and run a functional API test for the user story below, using the api-functional-qa MCP tools.

Input: $ARGUMENTS

1. **Ask before you build.** Call `review_user_story` with the story. It checks the story against the configured OpenAPI spec. If the project has related docs (a glossary, product notes, the feature's README), pass them as `context`.
   - If `ready` is false, show the user every question, with its reason and the words in the story that raised it, then **stop and wait for answers**. Don't guess them: not a status code, not hard versus soft delete, not an error body.
   - Add the answers to the story as acceptance criteria or Given/When/Then lines, and call `review_user_story` again.
   - If the user says to go ahead anyway, continue, but put each open question in the flow as a `# OPEN QUESTION:` YAML comment next to the step it affects, and list them in your final report.
2. Call `list_environments`. Use the default environment unless one is named. If it doesn't allow writes and the story needs them, stop and say so; never try to get around it.
3. Call `draft_flow` with the story. Read the candidate operations, and use `call_api` (read-only calls first) and `query_db` to learn real response shapes and table names.
4. Write the flow:
   - chain calls by saving IDs (`save: { orderId: "$.id" }`) and reusing them (`${orderId}`);
   - check values, not just status codes (amounts, states, counts);
   - add a `db` check after each data-changing call to prove the change was stored (read-only SELECT with `:params`);
   - use fixtures for test data, and add cleanup for anything the flow creates;
   - secrets only as `${ENV:NAME}`.
5. Save it with `write_flow` (ask before overwriting) and run it with `run_flows`.
6. If it fails, decide from the evidence whether the test or the API is wrong. Fix test mistakes. If the API is wrong, report the bug with the request, response and database evidence; never weaken the check to make it pass.

Finish with the flow path, what each step proves, the questions you asked and the answers you got (and any still open), and any bug found.
