# Changelog

## 0.1.1 (13 November 2026)

- The MCP server no longer logs request URLs to stderr. URLs can contain IDs or query-string credentials that bypass masking; all evidence is reported, masked, through the tools.

## 0.1.0 (13 November 2026)

- First release: YAML API test flows with chained calls, value/schema/timing checks, read-only database checks, fixtures and cleanup; negative and boundary tests generated from OpenAPI/Swagger (including nested fields); guardrails for production, database writes, SQL parameters, secrets and hosts; Markdown/JSON/JUnit reports; pytest export; 8 MCP tools and a CLI.
