"""Variables, JSON paths, assertions, guardrails (no network)."""

import sqlite3

import pytest

from k11_api_qa import db
from k11_api_qa.config import writes_allowed
from k11_api_qa.runner import FlowError, validate_flow
from k11_api_qa.values import MASK, Vars, check, json_path, status_matches


def test_substitution_keeps_types_and_masks_env_secrets(monkeypatch):
    monkeypatch.setenv("TOKEN_X", "s3cr3t-value")
    v = Vars({"order": {"id": 7, "items": [{"sku": "A"}]}})
    assert v.sub("${order.id}") == 7
    assert v.sub("/orders/${order.id}") == "/orders/7"
    assert v.sub({"x": ["${order.items.0.sku}"]}) == {"x": ["A"]}
    assert v.sub("Bearer ${ENV:TOKEN_X}") == "Bearer s3cr3t-value"
    assert v.mask({"h": "Bearer s3cr3t-value"}) == {"h": f"Bearer {MASK}"}
    assert len(v.sub("${uuid}")) == 36 and v.sub("${random_email}").endswith("@example.com")
    with pytest.raises(ValueError, match="Unknown variable"):
        v.sub("${nope}")
    with pytest.raises(ValueError, match="NOT_SET_X is not set"):
        v.sub("${ENV:NOT_SET_X}")


def test_json_path():
    doc = {"items": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}], "a": {"b": None}}
    assert json_path(doc, "$.items[1].sku") == (True, "B")
    assert json_path(doc, "$.items[*].sku") == (True, ["A", "B"])
    assert json_path(doc, "$.items.length") == (True, 2)
    assert json_path(doc, "$.a.b") == (True, None)
    assert json_path(doc, "$.missing")[0] is False
    assert json_path([{"t": 1}], "$[0].t") == (True, 1)


@pytest.mark.parametrize("actual,expected,ok", [
    (85.0, 85, True), ("85.00", 85, True), (42.5, 85.0, False),
    (5, {"gt": 3, "lt": 10}, True), ("abc-123", {"matches": r"^abc-\d+$"}, True),
    ([1, 2], {"length": 2}, True), ("x", {"in": ["x", "y"]}, True), (None, {"eq": None}, True),
    (3, {"type": "integer"}, True), (True, {"type": "number"}, False), (84.999, {"approx": [85, 0.01]}, True),
])
def test_check(actual, expected, ok):
    assert check(actual, expected)[0] is ok


def test_exists_and_status():
    assert check(None, {"exists": False}, found=False)[0]
    assert not check(None, {"exists": True}, found=False)[0]
    assert status_matches(201, "2xx") and status_matches(404, [400, 404]) and not status_matches(500, {"not": "5xx"})


def test_writes_allowed():
    assert writes_allowed("local", {}, "http://localhost:8080")[0]
    assert writes_allowed("qa-eu", {}, "https://qa.example.com")[0]
    assert not writes_allowed("prod", {"allow_writes": True}, "https://api.example.com")[0]
    assert writes_allowed("prod", {"allow_writes": True, "production_writes_acknowledged": True}, "https://x")[0]
    assert not writes_allowed("partner", {}, "https://partner.example.com")[0]
    assert writes_allowed("partner", {"allow_writes": True}, "https://partner.example.com")[0]


def test_sql_guardrails(tmp_path):
    with pytest.raises(db.DbError, match="params"):
        db.check_sql("SELECT * FROM t WHERE id = '${id}'", write=False)
    with pytest.raises(db.DbError, match="one SQL statement"):
        db.check_sql("SELECT 1; DROP TABLE t", write=False)
    with pytest.raises(db.DbError, match="may only read"):
        db.check_sql("DELETE FROM t", write=False)
    with pytest.raises(db.DbError, match="may only read"):
        db.check_sql("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", write=False)
    db.check_sql("SELECT ';' AS semi FROM t WHERE a = :a", write=False)  # literals don't count
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER, email TEXT)")
    con.execute("INSERT INTO t VALUES (1, 'a@x.io')")
    con.commit()
    con.close()
    res = db.run("sqlite:///t.db", tmp_path, "SELECT * FROM t WHERE id = :id", {"id": 1}, write=False)
    assert res["rows"] == [{"id": 1, "email": "a@x.io"}]
    assert db.mask_rows(res["rows"], ["email"]) == [{"id": 1, "email": MASK}]
    with pytest.raises(db.DbError, match="Missing parameter"):
        db.run("sqlite:///t.db", tmp_path, "SELECT * FROM t WHERE id = :id", {}, write=False)
    with pytest.raises(db.DbError, match="Invalid table"):
        db.insert_sql("t; DROP TABLE x", {"a": 1})


def test_flow_validation():
    with pytest.raises(FlowError, match="exactly one of 'call'"):
        validate_flow({"name": "x", "steps": [{"call": "GET /a", "db": "SELECT 1"}]})
    with pytest.raises(FlowError, match="unknown expect"):
        validate_flow({"name": "x", "steps": [{"call": "GET /a", "expect": {"stauts": 200}}]})
    with pytest.raises(FlowError, match="POST /orders"):
        validate_flow({"name": "x", "steps": [{"call": "FETCH /a"}]})
