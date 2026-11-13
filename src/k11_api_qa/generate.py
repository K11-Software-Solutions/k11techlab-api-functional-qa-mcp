"""Generate flows from the spec: negative and boundary tests, and story drafts.

Negative tests for one operation, each expecting a 4xx and never a 5xx:
  missing each required body field · wrong type for each field · value outside an enum ·
  just outside min/max/minLength/maxLength (plus a passing case exactly on the boundary) ·
  missing required query/header parameter · no credentials (when the operation is secured) ·
  an ID that doesn't exist (404 for GET/PUT/PATCH/DELETE with a path parameter)
"""

from __future__ import annotations

import copy
import re
from typing import Any

import yaml

from k11_contract_qa.spec import Operation, Spec, json_schema_of, success_response


def example(schema: dict | None, depth: int = 0) -> Any:
    if not isinstance(schema, dict) or depth > 6:
        return None
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    if schema.get("enum"):
        return schema["enum"][0]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if t == "object" or "properties" in schema:
        req = set(schema.get("required") or [])
        props = schema.get("properties") or {}
        return {k: example(v, depth + 1) for k, v in props.items() if k in req or not req}
    if t == "array":
        n = max(1, int(schema.get("minItems", 1)))
        return [example(schema.get("items") or {}, depth + 1) for _ in range(n)]
    fmt = schema.get("format", "")
    if t == "integer":
        return int(schema.get("minimum", 1)) if "minimum" in schema else 1
    if t == "number":
        return float(schema.get("minimum", 1.5)) if "minimum" in schema else 1.5
    if t == "boolean":
        return True
    s = {"date": "2026-01-01", "date-time": "2026-01-01T00:00:00Z", "email": "qa@example.com",
         "uuid": "00000000-0000-0000-0000-000000000000", "uri": "https://example.com"}.get(fmt, "test")
    if "minLength" in schema and len(s) < schema["minLength"]:
        s = s + "x" * (schema["minLength"] - len(s))
    if "maxLength" in schema:
        s = s[: schema["maxLength"]]
    return s


def _wrong(schema: dict) -> Any:
    t = schema.get("type")
    if t in ("integer", "number"):
        return "not-a-number"
    if t == "boolean":
        return "not-a-boolean"
    if t == "array":
        return {"not": "an array"}
    if t == "object" or "properties" in schema:
        return "not-an-object"
    return 12345  # strings get a number


def _call_template(op: Operation) -> tuple[str, dict]:
    path_params = {p["name"]: example(p.get("schema")) or "1" for p in op.parameters if p.get("in") == "path"}
    return f"{op.method.upper()} {op.path}", path_params


def _base_step(op: Operation) -> dict:
    call, path_params = _call_template(op)
    step: dict[str, Any] = {"call": call}
    if path_params:
        step["path"] = {k: f"${{{k}}}" for k in path_params}
    q = {p["name"]: example(p.get("schema")) for p in op.parameters if p.get("in") == "query" and p.get("required")}
    if q:
        step["query"] = q
    body_schema = json_schema_of((op.request_body or {}).get("content"))
    if body_schema:
        step["body"] = example(body_schema)
    return step


def _expect_4xx(op: Operation, preferred: tuple[str, ...] = ("400", "422")) -> Any:
    documented = [c for c in map(str, op.responses) if c.startswith("4")]
    chosen = [int(c) for c in preferred if c in documented]
    return chosen if chosen else "4xx"


_DELETE = object()


def _fields(schema: dict, prefix: tuple = (), depth: int = 0):
    """(path, schema, required) for every field, including nested objects and the first array item."""
    if depth > 5 or not isinstance(schema, dict):
        return
    req = set(schema.get("required") or [])
    for name, fs in (schema.get("properties") or {}).items():
        if not isinstance(fs, dict) or "x-recursive-ref" in fs:
            continue
        path = prefix + (name,)
        yield path, fs, name in req
        if fs.get("type") == "object" or "properties" in fs:
            yield from _fields(fs, path, depth + 1)
        elif fs.get("type") == "array" and isinstance(fs.get("items"), dict):
            yield from _fields(fs["items"], path + (0,), depth + 1)


def _label(path: tuple) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out


def _set(doc: Any, path: tuple, value: Any) -> bool:
    """Set (or delete, with _DELETE) a nested value; False if the path isn't in the example body."""
    cur = doc
    for p in path[:-1]:
        if isinstance(p, int):
            if not isinstance(cur, list) or len(cur) <= p:
                return False
        elif not isinstance(cur, dict) or p not in cur:
            return False
        cur = cur[p]
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(cur, list) or len(cur) <= last:
            return False
    elif not isinstance(cur, dict):
        return False
    if value is _DELETE:
        if isinstance(cur, dict):
            cur.pop(last, None)
        else:
            return False
    else:
        cur[last] = value
    return True


def negative_flows(spec: Spec, op: Operation, base_values: dict | None = None,
                   fixtures: list[str] | None = None) -> dict:
    """One flow per operation with a step for each negative/boundary case.

    ``base_values`` replaces example values in the request body (e.g. a real customerId from a
    fixture: ``{"customerId": "${fixture.customer.id}"}``) in every case except the one that
    tests that field, so each case is valid apart from the single thing it breaks."""
    base = _base_step(op)
    if base_values and isinstance(base.get("body"), dict):
        base["body"].update(base_values)
    for k, val in (base_values or {}).items():  # path/query overrides too
        if k in (base.get("path") or {}):
            base["path"][k] = val
        if k in (base.get("query") or {}):
            base["query"][k] = val
    _, path_params = _call_template(op)
    steps: list[dict] = []
    body_schema = json_schema_of((op.request_body or {}).get("content")) or {}
    if isinstance(base.get("body"), dict):
        rejected = {"status": _expect_4xx(op)}
        for path, fs, required in _fields(body_schema):
            label = _label(path)
            if required:
                s = copy.deepcopy(base)
                if _set(s["body"], path, _DELETE):
                    steps.append({"name": f"missing required field '{label}' is rejected", **s, "expect": rejected})
            s = copy.deepcopy(base)
            if _set(s["body"], path, _wrong(fs)):
                steps.append({"name": f"wrong type for '{label}' is rejected", **s, "expect": rejected})
            if fs.get("enum"):
                s = copy.deepcopy(base)
                if _set(s["body"], path, "__not_an_allowed_value__"):
                    steps.append({"name": f"value outside the allowed list for '{label}' is rejected", **s, "expect": rejected})
            for lo_key, hi_key, kind in (("minimum", "maximum", "number"), ("minLength", "maxLength", "length"),
                                         ("minItems", "maxItems", "items")):
                for key, delta, side in ((lo_key, -1, "below"), (hi_key, +1, "above")):
                    if key not in fs:
                        continue
                    bound = fs[key]
                    if kind == "number":
                        bad = bound + delta if fs.get("type") == "integer" else bound + delta * 0.01
                        good = bound
                    elif kind == "length":
                        if bound + delta < 0:
                            continue
                        bad, good = "x" * (bound + delta), "x" * bound
                    else:
                        if bound + delta < 0 or bound + delta > 1000:
                            continue
                        item = example(fs.get("items") or {})
                        bad, good = [copy.deepcopy(item) for _ in range(bound + delta)], [copy.deepcopy(item) for _ in range(bound)]
                    s = copy.deepcopy(base)
                    if _set(s["body"], path, bad):
                        steps.append({"name": f"'{label}' just {side} its {key} ({bound}) is rejected", **s, "expect": rejected})
                    s = copy.deepcopy(base)
                    if _set(s["body"], path, good) and not (kind == "items" and bound == 0):
                        steps.append({"name": f"'{label}' exactly at its {key} ({bound}) is accepted", **s,
                                      "expect": {"status": "2xx"}})
    for p in op.parameters:
        if p.get("in") in ("query", "header") and p.get("required"):
            s = copy.deepcopy(base)
            if p["in"] == "query":
                s.get("query", {}).pop(p["name"], None)
            steps.append({"name": f"missing required {p['in']} parameter '{p['name']}' is rejected", **s,
                          "expect": {"status": _expect_4xx(op)}})
    if op.security:
        steps.append({"name": "request without credentials is rejected", **copy.deepcopy(base), "auth": "none",
                      "expect": {"status": [401, 403]}})
    if path_params and op.method.upper() in ("GET", "PUT", "PATCH", "DELETE"):
        s = copy.deepcopy(base)
        s["path"] = {k: "does-not-exist-000" for k in path_params}
        steps.append({"name": "an ID that doesn't exist returns 404", **s, "expect": {"status": 404}})

    # Cases that must be accepted on a POST create real records: save their IDs and delete them afterwards
    # with the spec's own DELETE operation, when there is one (e.g. POST /orders -> DELETE /orders/{id}).
    cleanup: list[dict] = []
    if op.method.upper() == "POST":
        delete_op = next((o for o in spec.operations.values() if o.method.upper() == "DELETE"
                          and re.fullmatch(re.escape(op.path.rstrip("/")) + r"/\{[^}/]+\}", o.path)), None)
        succ = success_response(op)
        resp_schema = json_schema_of(((succ[1] if succ else {}) or {}).get("content")) or {}
        if delete_op and "id" in (resp_schema.get("properties") or {}):
            param = re.search(r"\{([^}]+)\}$", delete_op.path).group(1)
            n = 0
            for st in steps:
                if st.get("expect", {}).get("status") == "2xx":
                    n += 1
                    st["save"] = {f"created_{n}": "$.id"}
                    cleanup.append({"name": f"delete record created by an accepted case ({n})",
                                    "call": f"DELETE {delete_op.path}", "path": {param: f"${{created_{n}}}"},
                                    "expect": {"status": "2xx"}})
    return {
        "name": f"Negative and boundary tests: {op.key}",
        "description": f"Generated from {spec.label}. Each case must be rejected with a 4xx (never a 5xx) "
                       "unless it is a boundary that must be accepted.",
        "tags": ["negative", "generated"],
        "continue_on_failure": True,
        **({"fixtures": list(fixtures)} if fixtures else {}),
        "vars": {k: v for k, v in path_params.items()},
        "steps": steps or [{"name": "no negative cases could be derived", **base, "expect": {"status": "2xx"}}],
        **({"cleanup": cleanup} if cleanup else {}),
    }


def flow_file_name(op: Operation, prefix: str = "negative") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", f"{op.method}_{op.path}".lower()).strip("_")
    return f"tests/api/{prefix}_{slug}.yaml"


def to_yaml(flow: dict) -> str:
    return yaml.safe_dump(flow, sort_keys=False, allow_unicode=True, width=120)


# ---- story drafts -------------------------------------------------------------------------------

STOP = {"a", "an", "the", "i", "to", "so", "that", "as", "and", "or", "of", "in", "on", "my", "me", "can", "be",
        "is", "are", "want", "with", "for", "it", "see", "get", "should", "when", "then", "given", "user", "customer"}
VERBS = {"create": "POST", "add": "POST", "place": "POST", "submit": "POST", "register": "POST", "book": "POST",
         "update": "PUT", "change": "PATCH", "edit": "PATCH", "rename": "PATCH", "delete": "DELETE", "remove": "DELETE",
         "cancel": "DELETE", "list": "GET", "view": "GET", "see": "GET", "find": "GET", "search": "GET", "fetch": "GET",
         "show": "GET", "read": "GET", "history": "GET"}


def _tokens(s: str) -> list[str]:
    words = re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", s).lower())
    return [w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words if w not in STOP]


def draft(story: str, spec: Spec | None) -> dict:
    """Scenarios from the story, the spec operations most likely involved, and a flow skeleton."""
    lines = [ln.strip(" -*•\t") for ln in story.replace("\r", "").split("\n") if ln.strip()]
    scenarios = [ln for ln in lines if not re.match(r"^as an?\s", ln, re.I)] or lines
    ranked = []
    if spec is not None and spec.http:
        story_tokens = set(_tokens(story))
        wanted_methods = {VERBS[t] for t in story_tokens if t in VERBS}
        for op in spec.operations.values():
            op_tokens = set(_tokens(f"{op.path} {op.summary} {op.operation_id}"))
            score = len(story_tokens & op_tokens) + (1.5 if op.method.upper() in wanted_methods else 0)
            if score > 0:
                ranked.append((score, op))
        ranked.sort(key=lambda x: (-x[0], x[1].path, x[1].method))
    candidates = [{"endpoint": op.key, "summary": op.summary, "score": round(s, 1)} for s, op in ranked[:8]]
    steps = []
    for s, op in ranked[:4]:
        st = _base_step(op)
        succ = success_response(op)
        st = {"name": f"TODO: {op.summary or op.key}", **st, "expect": {"status": int(succ[0]) if succ and succ[0].isdigit() else "2xx",
                                                                        "json": {"$": {"exists": True}}}}
        steps.append(st)
    skeleton = {"name": scenarios[0][:80] if scenarios else "New flow", "description": story.strip()[:500],
                "tags": ["story"], "steps": steps or [{"name": "TODO: first call", "call": "GET /", "expect": {"status": 200}}]}
    return {"scenarios": scenarios, "candidate_operations": candidates, "skeleton": to_yaml(skeleton),
            "next": "Chain the calls (save IDs with 'save', reuse them as ${name}), add value checks under expect.json, "
                    "add db checks for side effects, then write_flow and run_flows."}
