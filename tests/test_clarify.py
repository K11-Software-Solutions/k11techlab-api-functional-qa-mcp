"""review_user_story: the questions a careful tester asks before any API flow is written.

The shared cases live in tests/fixtures/review_cases.json, identical to k11 Playwright QA and k11 Selenium QA:
all three implementations must produce exactly the same questions for the same story, so the shared rules
can't drift apart. The API rules are tested below, in both directions: each gap must be caught, and a clear
story must get no questions.
"""
import json
from pathlib import Path

import pytest
from k11_contract_qa.spec import load_spec

from k11_api_qa.clarify import parse_story, review, review_api, review_story

CASES = json.loads((Path(__file__).parent / "fixtures" / "review_cases.json").read_text(encoding="utf-8"))["cases"]
SHOP = Path(__file__).parent.parent / "demo" / "shop"


@pytest.fixture(scope="module")
def spec():
    return load_spec(SHOP.resolve(), "openapi.yaml")


def kinds(raw, spec=None, context=None):
    return [q["kind"] for q in review(raw, context, spec)["questions"]]


# ---- shared rules ---------------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_matches_the_shared_cases(case):
    r = review_story(parse_story(case["story"]), case["story"], case.get("context"))
    got = [{k: q[k] for k in ("kind", "question", "quote") if k in q} for q in r["questions"]]
    assert {"ready": r["ready"], "questions": got} == case["expect"]


def test_the_shared_cases_cover_every_kind_and_both_directions():
    covered = {q["kind"] for c in CASES for q in c["expect"]["questions"]}
    assert covered == {"criteria", "role", "vague-term", "outcome", "precondition", "limit", "failure-path", "undefined-term"}
    assert sum(c["expect"]["ready"] for c in CASES) >= 3


# ---- API rules ------------------------------------------------------------------------------------

CLEAR_CANCEL = """As a signed-in customer, I want to cancel an order so that I am not charged.
Given I have a pending order
When I send DELETE /orders/{id}
Then I get 204 and the order is kept with status "cancelled" and a deleted_at timestamp
When I cancel an unknown order
Then I get 404
When I call without a token
Then I get 401"""

CLEAR_REGISTER = """As a visitor, I want to register so that I can place orders.
Given no customer exists with my email
When I send POST /customers with `email` and `name`
Then I get 201 and the response has an id
When I register the same email twice
Then the second call gets 409 with "Email already registered"
When I send it without an email
Then I get 400"""


@pytest.mark.parametrize("raw", [CLEAR_CANCEL, CLEAR_REGISTER], ids=["cancel", "register"])
def test_clear_api_stories_get_no_questions(raw, spec):
    r = review(raw, None, spec)
    assert r["ready"] and r["questions"] == [], r["questions"]


def test_rejection_without_a_status_asks_for_the_error_contract():
    raw = "As a visitor, I want to register.\n- Registering with a missing email is rejected"
    qs = [q for q in review(raw)["questions"] if q["kind"] == "error-contract"]
    assert len(qs) == 1 and qs[0]["quote"] == "Registering with a missing email is rejected"
    assert "error-contract" not in kinds("As a visitor, I want to register.\n- Registering with a missing email is rejected with 400")


def test_a_status_in_another_exchange_does_not_answer_a_rejection():
    raw = ("As a visitor, I want to register.\nGiven no account exists\nWhen I register\nThen I get 201\n"
           "When I register with an invalid email\nThen it is rejected")
    qs = [q for q in review(raw)["questions"] if q["kind"] == "error-contract"]
    assert [q["quote"] for q in qs] == ["When I register with an invalid email; Then it is rejected"]


def test_delete_without_saying_whether_the_record_is_kept():
    raw = "As a visitor, I want to delete my draft.\nGiven I have a draft\nWhen I delete it\nThen it is removed and I get 204"
    assert "delete-semantics" in kinds(raw)
    assert "delete-semantics" not in kinds(raw.replace("it is removed", "the row is gone"))
    assert "delete-semantics" not in kinds(raw.replace("it is removed", 'it is kept with status "deleted"'))


def test_signed_in_role_without_access_expectations():
    raw = "As a customer, I want to see my orders.\nGiven I have 2 orders\nWhen I list my orders\nThen I get 200 with 2 orders"
    assert "access" in kinds(raw)
    assert "access" not in kinds(raw + "\nWhen I call without a token\nThen I get 401")
    assert "access" not in kinds(raw + "\nWhen I ask for another customer's orders\nThen I get 404")
    assert "access" not in kinds(raw.replace("As a customer", "As a visitor"))


def test_repeat_without_a_defined_result_is_asked_once():
    raw = "As a visitor, I want to register.\nGiven no account exists\nWhen I register the same email again\nThen it is refused"
    ks = kinds(raw)
    assert ks.count("repeat") == 1 and "error-contract" not in ks      # one question, not two about the same status
    assert "repeat" not in kinds(raw.replace("it is refused", "I get 409"))


def test_spec_mismatch_for_unknown_endpoints_and_fields(spec):
    raw = ("As a visitor, I want to register.\nGiven no account exists\n"
           "When I send POST /signup with `nickname` and customerId\nThen I get 201")
    qs = [q for q in review(raw, None, spec)["questions"] if q["kind"] == "spec-mismatch"]
    assert [q["quote"] for q in qs] == ["POST /signup", "nickname"]   # customerId is a real field in the spec
    assert not any(q["kind"] == "spec-mismatch" for q in review(raw)["questions"])   # no spec, no spec questions


def test_parameterised_paths_match_the_spec(spec):
    raw = "As a visitor, I want an order.\nGiven order 7 exists\nWhen I send GET /orders/{id}\nThen I get 200"
    assert "spec-mismatch" not in kinds(raw, spec)


def test_api_rules_only_add_questions():
    raw = CASES[0]["story"]
    story = parse_story(raw)
    shared = review_story(story, raw)["questions"]
    assert review(raw)["questions"] == shared + review_api(story, raw)


def test_every_question_says_why_and_is_deterministic(spec):
    raw = ("As a customer, I want checkout to be fast.\nGiven a cart\nWhen I cancel it\nThen it is removed\n"
           "When I send it again\nThen it is rejected\nWhen I send POST /nope\nThen I get 200")
    a, b = review(raw, None, spec), review(raw, None, spec)
    assert a == b and len(a["questions"]) >= 5
    assert all(len(q["why"]) > 20 for q in a["questions"])
