---
description: Generate and run negative and boundary tests for an API operation from its spec
argument-hint: <operation, e.g. "POST /orders"> [environment]
---

Generate and run negative and boundary tests for the operation below, using the api-functional-qa MCP tools.

Input: $ARGUMENTS

1. Call `list_environments`. The tests send invalid requests and a few valid boundary ones that create records, so use an environment that allows writes. Never run them against production.
2. If valid requests need real data (for example an existing customer), find a matching fixture in the config and pass `base_values` (e.g. `{"customerId": "${fixture.customer.id}"}`) and `fixtures`.
3. Call `generate_negative_tests`, save the flow with `write_flow`, and run it with `run_flows`.
4. Report the results in three groups:
   - **server errors (5xx):** always bugs; give the request that caused each;
   - **accepted invalid input** (a 2xx where a 4xx was expected): validation gaps;
   - **wrong rejection** (a boundary value that should be accepted but got a 4xx).
5. Note any status codes the API returns that the spec doesn't document.

Don't change expectations to make cases pass. Finish with a short bug list, most severe first.
