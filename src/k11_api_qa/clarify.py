"""Review a user story before drafting an API flow from it: what would a careful tester ask first?

Drafting a flow from an unclear story doesn't remove the ambiguity; it hides it inside the flow, as
guessed status codes, guessed database checks and guessed error bodies that nobody agreed to. This
review finds the gaps and turns each into a question for a person to answer, with the reason it
matters and, where it applies, the exact words in the story that raised it.

The rules are deterministic, so the same story always gets the same questions, and they can be tested.

Shared rules (review_story). These match k11 Playwright QA and k11 Selenium QA exactly; all three
repositories test against the same cases in tests/fixtures/review_cases.json:

  outcome         a scenario has no observable result, or the result is vague ("works", "correctly")
  criteria        no acceptance criteria at all; the only "scenario" is the goal itself
  role            no user role (permissions and test accounts depend on it)
  vague-term      words a test can't check: fast, easy, many, appropriate, user-friendly...
  limit           a limit or threshold without a number ("up to", "maximum", "expires")
  failure-path    no scenario says what happens when things go wrong
  precondition    scenarios with actions but no starting state (who is signed in, what data exists)
  undefined-term  with context: a term the story relies on that the context never explains

API rules (review_api), for things an API test has to pin down that a UI test often doesn't:

  error-contract    a rejection with no status code ("invalid input is rejected": 400 or 422? what body?)
  delete-semantics  something is deleted, cancelled or removed, but not whether the record is kept
  access            a signed-in role, but nothing about calls without credentials or on someone else's data
  repeat            a repeated or duplicate request with no defined result (409? the same result again?)
  spec-mismatch     with a spec: an endpoint or field name in the story that the spec doesn't have
"""

from __future__ import annotations

import re
from typing import Any, Optional

# ---- story parsing (same formats as the other k11 QA tools) ---------------------------------------

STEP_RE = re.compile(r"^(given|when|then|and|but)\b[:\s]*(.*)$", re.I)


def parse_story(raw: str) -> dict[str, Any]:
    """As a <role>, I want <goal> so that <benefit>; Scenario/Given/When/Then; or acceptance-criteria bullets."""
    text = raw.replace("\r", "")
    story: dict[str, Any] = {"title": "", "scenarios": []}
    m = re.search(r"as an?\s+(.+?),?\s+i (?:want|need|would like)(?: to)?\s+(.+?)(?:,?\s+so that\s+(.+?))?(?:\.|\n|$)", text, re.I)
    if m:
        story["role"], story["goal"] = m.group(1).strip(), m.group(2).strip()
        if m.group(3):
            story["benefit"] = m.group(3).strip()
    t = re.search(r"^(?:title|story|feature)\s*:\s*(.+)$", text, re.I | re.M)
    first = next((ln for ln in text.split("\n") if ln.strip()), "User story")
    title = (t.group(1) if t else story.get("goal") or first).strip()
    story["title"] = title[:1].upper() + title[1:]

    current = None
    bullets: list[str] = []
    for line in (ln.strip() for ln in text.split("\n")):
        if not line:
            continue
        sc = re.match(r"^scenario(?: outline)?\s*:\s*(.+)$", line, re.I)
        if sc:
            current = {"name": sc.group(1).strip(), "steps": []}
            story["scenarios"].append(current)
            continue
        st = STEP_RE.match(re.sub(r"^[-*•]\s*", "", line))
        if st:
            if current is None:
                current = {"name": f"Scenario {len(story['scenarios']) + 1}", "steps": []}
                story["scenarios"].append(current)
            current["steps"].append({"keyword": st.group(1).capitalize(), "text": st.group(2).strip()})
            continue
        b = re.match(r"^(?:[-*•]|\d+[.)]|ac\s*\d+\s*[:.-])\s*(.+)$", line, re.I)
        if b and not re.match(r"^as an?\s", b.group(1), re.I):
            bullets.append(b.group(1).strip())
    if not story["scenarios"]:
        story["scenarios"] = [{"name": b, "steps": [{"keyword": "Check", "text": b}]} for b in bullets]
    if not story["scenarios"] and story.get("goal"):
        story["scenarios"] = [{"name": story["goal"], "steps": [{"keyword": "Check", "text": story["goal"]}]}]
    return story


# ---- shared rules (keep identical to k11 Playwright QA and k11 Selenium QA) -----------------------

VAGUE = [
    "fast", "quickly", "quick", "slow", "easy", "easily", "simple", "simply", "intuitive", "user-friendly", "user friendly",
    "seamless", "seamlessly", "many", "several", "some", "few", "large", "small", "appropriate", "appropriately",
    "properly", "correctly", "as needed", "as expected", "efficient", "efficiently", "reasonable", "secure", "robust",
    "and/or", "etc", "usually", "normally", "relevant", "nice", "better",
]
VAGUE_OUTCOME = re.compile(r"\b(works?|working|is successful|succeeds?|correctly|properly|as expected|is handled|handles it|is fine|is ok(ay)?)\b", re.I)
OBSERVABLE = re.compile(r'\b(see|sees|shown?|shows|display(s|ed)?|visible|message|redirect(s|ed)?|page|url|receives?|email|download(s|ed)?|disabled|enabled|error|text|title|heading|count|list|button|link|status|toast|banner|notification|"[^"]+")\b', re.I)
FAILURE = re.compile(r"\b(invalid|wrong|incorrect|error|fail(s|ed|ure)?|denied|forbidden|unauthori[sz]ed|expired?|empty|missing|blank|not\b|no\b|cannot|can't|unknown|locked|timeout|offline|duplicate|too (long|many|short))\b", re.I)
LIMIT = re.compile(r"\b(up to|at most|at least|maximum|minimum|max|min|limit(ed)?|expires?|timeout|within|no more than|no less than|threshold|retries|attempts)\b", re.I)
HAS_NUMBER = re.compile(r"\d")
STOP_TERMS = {"As", "I", "Given", "When", "Then", "And", "But", "Scenario", "Title", "Story", "Feature",
              "Acceptance", "AC", "The", "A", "An", "If", "So", "On", "In", "My", "Our", "This", "That", "It"}
TRAILING = re.compile(r"[,.;:!?)]+$")
CAPITALISED = re.compile(r"^[A-Z][a-zA-Z0-9-]+$")


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.replace("\r", ""))
    out = [re.sub(r"^[-*•]\s*|^\d+[.)]\s*", "", p, count=1).strip() for p in parts]
    return [s for s in out if s]


def _word_re(term: str) -> re.Pattern[str]:
    return re.compile(r"(^|[^a-z])" + re.escape(term) + r"([^a-z]|$)", re.I)


def _named_terms(raw: str) -> list[str]:
    """Terms a story relies on: quoted phrases and Capitalised Multi Word names that aren't sentence starts."""
    out: dict[str, None] = {}
    for m in re.finditer(r'"([^"]{2,40})"', raw):
        out[m.group(1).strip()] = None
    for s in _sentences(raw):
        words = re.split(r"\s+", s)
        i = 1
        while i < len(words):
            run: list[str] = []
            while i < len(words) and CAPITALISED.match(TRAILING.sub("", words[i])) and words[i] not in STOP_TERMS:
                run.append(TRAILING.sub("", words[i]))
                i += 1
            if run:
                out[" ".join(run)] = None
            i += 1
    return list(out)


def review_story(story: dict[str, Any], raw: str, context: Optional[str] = None) -> dict[str, Any]:
    """story: the dict parse_story returns. Returns {ready, questions, assumptions}."""
    questions: list[dict[str, str]] = []
    scenarios = story.get("scenarios", [])
    every = _sentences(raw)
    only_goal = (len(scenarios) == 1 and len(scenarios[0]["steps"]) == 1
                 and scenarios[0]["steps"][0]["keyword"] == "Check" and scenarios[0]["steps"][0]["text"] == story.get("goal"))

    if not scenarios or only_goal:
        questions.append({
            "kind": "criteria",
            "question": "What are the acceptance criteria? List what must be true for this story to be done.",
            "why": "Without criteria, tests can only check that the page loads, and anything else they assert is a guess.",
        })
    else:
        for sc in scenarios:
            outcomes = [s for s in sc["steps"] if s["keyword"] in ("Then", "Check")]
            text = " ".join(s["text"] for s in outcomes)
            if not outcomes:
                questions.append({
                    "kind": "outcome",
                    "question": f'In "{sc["name"]}", what should the user see or what should change at the end?',
                    "why": "A scenario with no Then has nothing to assert.",
                })
            elif VAGUE_OUTCOME.search(text) and not OBSERVABLE.search(text):
                questions.append({
                    "kind": "outcome",
                    "question": f'In "{sc["name"]}", what exactly shows that it worked: a message, a page, a value, an email?',
                    "why": "\"Works\" or \"correctly\" can't be asserted; a test needs something observable.",
                    "quote": "; ".join(f'{s["keyword"]} {s["text"]}' for s in outcomes),
                })
            acts = any(s["keyword"] == "When" for s in sc["steps"])
            if acts and not any(s["keyword"] == "Given" for s in sc["steps"]):
                questions.append({
                    "kind": "precondition",
                    "question": f'In "{sc["name"]}", what is the starting state: who is signed in, and what data already exists?',
                    "why": "Tests need set-up; guessing it makes them pass for the wrong reason or fail on a clean environment.",
                })

    if not story.get("role"):
        questions.append({
            "kind": "role",
            "question": "Who is the user: a visitor, a signed-in customer, an admin? Does anyone else get different behaviour?",
            "why": "Permissions, test accounts and what the page shows all depend on the role.",
        })

    seen: set[str] = set()
    outcome_quotes = {part for q in questions if q["kind"] == "outcome" and q.get("quote") for part in q["quote"].split("; ")}
    for s in every:
        # Quoted text is what the page shows ("This code has expired"), not a requirement to question.
        req = re.sub(r'"[^"]*"', '""', s)
        for term in VAGUE:
            if s in outcome_quotes:
                continue                                   # already asked about as a vague outcome
            if _word_re(term).search(req) and term not in seen:
                seen.add(term)
                questions.append({
                    "kind": "vague-term",
                    "question": f'"{term}" can\'t be tested as written. What is the measurable version (a number, a time, a specific behaviour)?',
                    "why": "A test needs a threshold or a concrete expectation.",
                    "quote": s,
                })
        if LIMIT.search(req) and not HAS_NUMBER.search(s):
            questions.append({
                "kind": "limit",
                "question": "What is the actual number for this limit, and what happens exactly at it and one past it?",
                "why": "Boundary tests need the boundary.",
                "quote": s,
            })

    scenario_text = " ".join(f'{sc["name"]} {" ".join(s["text"] for s in sc["steps"])}' for sc in scenarios)
    if scenarios and not only_goal and not FAILURE.search(scenario_text):
        questions.append({
            "kind": "failure-path",
            "question": "What should happen when it goes wrong: invalid input, no permission, an expired session, a server error?",
            "why": "Every scenario is a success path, so failures are untested; this is where most production bugs are.",
        })

    if context:
        for term in _named_terms(raw):
            if not _word_re(term).search(context):
                questions.append({
                    "kind": "undefined-term",
                    "question": f'What does "{term}" mean here? The context provided doesn\'t mention it.',
                    "why": "The story depends on it, and a test built on a wrong reading checks the wrong thing.",
                    "quote": term,
                })

    role = story.get("role")
    assumptions = [
        f"The tests will use a {role} account." if role else "The tests will use a standard signed-in user.",
        "Anything a question above asks about will be left as a TODO, not guessed.",
    ]
    return {"ready": not questions, "questions": questions, "assumptions": assumptions}


# ---- API rules ------------------------------------------------------------------------------------

STATUS_4XX_5XX = re.compile(r"\b[45]\d\d\b")
REJECTION = re.compile(r"\b(reject(s|ed)?|refuse(s|d)?|invalid|not allowed|disallowed|denied|forbidden|unauthori[sz]ed|fails?|failed|error)\b", re.I)
DELETION = re.compile(r"\b(delete[sd]?|deletion|remove[sd]?|removal|cancel(s|led|ed|lation)?|archive[sd]?|deactivate[sd]?)\b", re.I)
DELETION_DEFINED = re.compile(r"\b(soft|hard|permanent(ly)?|status|deleted_at|kept|keeps|marked|flag(ged)?|row|rows|database|db|purge[sd]?|gone)\b", re.I)
PUBLIC_ROLES = re.compile(r"\b(visitor|guest|anonymous|public|anyone)\b", re.I)
ACCESS_DEFINED = re.compile(r"\b(401|403|unauthori[sz]ed|forbidden|without (a |an |any )?(token|credentials|api key|key|auth\w*|signing in|logging in|login)|not (signed|logged) in|signed out|logged out|another (customer|user|account|tenant)|other (customer|user|account)s?'s|someone else'?s?)\b", re.I)
REPEAT = re.compile(r"\b(twice|again|duplicates?|duplicated|already|more than once|only once|a second time|same (email|request|order|code|payment|key|id|name))\b", re.I)
ENDPOINT = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[\w\-./{}:]*)")
BACKTICKED = re.compile(r"`([A-Za-z_][\w.\[\]]*)`")
CAMEL = re.compile(r"\b([a-z]+(?:[A-Z][a-z0-9]*)+)\b")


def _spec_fields(spec: Any) -> set[str]:
    """Every parameter, request-body and response property name in the spec (any depth), lower-cased."""
    from k11_contract_qa.spec import json_schema_of

    names: set[str] = set()

    def walk(schema: Any, depth: int = 0) -> None:
        if not isinstance(schema, dict) or depth > 8:
            return
        for k, v in (schema.get("properties") or {}).items():
            names.add(k.lower())
            walk(v, depth + 1)
        for key in ("items", "additionalProperties"):
            walk(schema.get(key), depth + 1)
        for key in ("allOf", "anyOf", "oneOf"):
            for sub in schema.get(key) or []:
                walk(sub, depth + 1)

    for op in spec.operations.values():
        for p in op.parameters or []:
            if p.get("name"):
                names.add(str(p["name"]).lower())
        walk(json_schema_of((op.request_body or {}).get("content")))
        for resp in (op.responses or {}).values():
            walk(json_schema_of((resp or {}).get("content")))
    return names


def _exchanges(story: dict[str, Any]) -> list[tuple[list[str], str]]:
    """Split scenarios into exchanges: each When (with the Givens before it) and the Then/And/But steps after it,
    or a single acceptance criterion. Returns (step lines, outcome text) pairs."""
    out: list[tuple[list[str], str]] = []
    for sc in story.get("scenarios", []):
        lines: list[str] = []
        outcome: list[str] = []
        for s in sc["steps"]:
            kw = s["keyword"]
            if kw == "Check":
                out.append(([s["text"]], s["text"]))
                continue
            if kw in ("When", "Given") and outcome:          # the previous exchange is complete
                out.append((lines, " ".join(outcome)))
                lines, outcome = [], []
            lines.append(f"{kw} {s['text']}")
            if kw in ("Then", "And", "But") and (outcome or kw == "Then"):
                outcome.append(s["text"])
        if lines:
            out.append((lines, " ".join(outcome)))
    return out


def review_api(story: dict[str, Any], raw: str, spec: Any = None) -> list[dict[str, str]]:
    """The extra questions an API flow needs answered. spec: a parsed OpenAPI/Swagger Spec, or None."""
    questions: list[dict[str, str]] = []
    every = _sentences(raw)
    whole = " ".join(every)

    def unquoted(text: str) -> str:
        return re.sub(r'"[^"]*"', '""', text)

    # Rejections and repeats are judged per exchange (a When and the Thens after it, or one criterion):
    # "When I send it twice / Then I get 409" is answered even though the status isn't on the same line,
    # and a 201 in another exchange doesn't answer it.
    for steps, outcome in _exchanges(story):
        if not outcome:
            continue                                      # the shared "outcome" rule already asks
        repeated = [s for s in steps if REPEAT.search(unquoted(s))]
        if repeated and not re.search(r"\b[1-5]\d\d\b", outcome):
            # Asked once: the repeat question already covers "which status", so no error-contract question too.
            questions.append({
                "kind": "repeat",
                "question": "What should a repeated or duplicate request return: an error (which status, such as 409), the original result unchanged, or a second record?",
                "why": "APIs differ most here (409, an idempotent 200, or a silent duplicate), so a test has to know which one is correct.",
                "quote": repeated[0],
            })
        elif any(REJECTION.search(unquoted(s)) for s in steps) and not STATUS_4XX_5XX.search(outcome):
            questions.append({
                "kind": "error-contract",
                "question": "Which status code and error body should this return (for example 400 or 422, and which message or error code)?",
                "why": "A rejection test needs the exact status and body; \"any 4xx\" also passes when the request fails for the wrong reason, such as a typo in the URL.",
                "quote": "; ".join(steps),
            })

    deletes = [s for s in every if DELETION.search(unquoted(s))]
    if deletes and not DELETION_DEFINED.search(whole):
        questions.append({
            "kind": "delete-semantics",
            "question": "When it's deleted or cancelled, is the record removed (hard delete) or kept and marked (soft delete: which status or column)? And what does GET return for it afterwards?",
            "why": "The database check depends on it: a hard delete expects no row, a soft delete expects the row with a status or timestamp. Guessing makes the check prove the wrong thing.",
            "quote": deletes[0],
        })

    role = story.get("role")
    if role and not PUBLIC_ROLES.search(role) and not ACCESS_DEFINED.search(whole):
        questions.append({
            "kind": "access",
            "question": f"The story is for a {role}. What should happen without credentials, and when one user calls with another user's IDs: 401, 403 or 404?",
            "why": "Access control is the API's job, not the UI's; without an expected answer a test can't tell a missing check from a correct one.",
        })

    if spec is not None and getattr(spec, "http", False):
        for m in ENDPOINT.finditer(raw):
            ep = f"{m.group(1)} {m.group(2).rstrip('.,;:')}"
            if spec.find(ep) is None:
                questions.append({
                    "kind": "spec-mismatch",
                    "question": f"The story mentions {ep}, but the spec has no such operation. Is the spec out of date, or should the flow use a different endpoint?",
                    "why": "A flow can only call what the API documents; testing an undocumented endpoint, or the wrong one, checks nothing the story asked for.",
                    "quote": ep,
                })
        fields = _spec_fields(spec)
        asked: set[str] = set()
        for name in [*BACKTICKED.findall(raw), *CAMEL.findall(re.sub(r"`[^`]*`", "", raw))]:
            leaf = re.split(r"[.\[\]]", name.strip("."))
            leaf = next((p for p in reversed(leaf) if p), name)
            if leaf.lower() in fields or leaf.lower() in asked:
                continue
            asked.add(leaf.lower())
            questions.append({
                "kind": "spec-mismatch",
                "question": f'The story uses "{leaf}", but no parameter, request field or response field in the spec has that name. Which field does it mean?',
                "why": "Checks are written against real field names; a field the spec doesn't have is either a spec gap or a misread story.",
                "quote": leaf,
            })
    return questions


def review(raw: str, context: Optional[str] = None, spec: Any = None) -> dict[str, Any]:
    """Shared rules plus API rules: {ready, questions, assumptions}."""
    story = parse_story(raw)
    out = review_story(story, raw, context)
    out["questions"] += review_api(story, raw, spec)
    out["ready"] = not out["questions"]
    return out
