"""Demo Shop API backed by SQLite, with two deliberate bugs for the tests to find:

1. POST /orders returns a wrong `total` when a quantity is above 1 (it ignores qty);
   the database stores the right total.
2. POST /orders crashes with a 500 when `qty` is sent as text instead of a number.

  python demo/shop/server.py --port 8080      (token: test-token)
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "test-token"
PRICES = {"BAG-1": 42.5, "HAT-2": 15.0}
CHANNELS = {"web", "mobile", "pos"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def init_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS customers (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS products (sku TEXT PRIMARY KEY, price REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, status TEXT NOT NULL,
            total REAL NOT NULL, channel TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT);
        CREATE TABLE IF NOT EXISTS order_items (order_id TEXT NOT NULL, sku TEXT NOT NULL, qty INTEGER NOT NULL,
            price REAL NOT NULL);
    """)
    con.executemany("INSERT OR IGNORE INTO products VALUES (?, ?)", PRICES.items())
    con.commit()
    con.close()


class Shop:
    def __init__(self, db_path: Path, port: int = 0):
        self.db_path = Path(db_path)
        init_db(self.db_path)
        shop = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._send(401, {"error": "unauthorized"})
                    return False
                return True

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return None

            def do_GET(self):  # noqa: N802
                if not self._authed():
                    return
                path, _, qs = self.path.partition("?")
                con = sqlite3.connect(shop.db_path)
                con.row_factory = sqlite3.Row
                try:
                    if m := re.fullmatch(r"/orders/([^/]+)", path):
                        o = shop.order(con, m.group(1))
                        return self._send(200, o) if o else self._send(404, {"error": "order not found"})
                    if path == "/orders":
                        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
                        if "customerId" not in params:
                            return self._send(400, {"error": "customerId is required"})
                        ids = [r["id"] for r in con.execute(
                            "SELECT id FROM orders WHERE customer_id = ? ORDER BY created_at", (params["customerId"],))]
                        return self._send(200, [shop.order(con, i) for i in ids])
                    return self._send(404, {"error": "not found"})
                finally:
                    con.close()

            def do_POST(self):  # noqa: N802
                if not self._authed():
                    return
                body = self._body()
                if not isinstance(body, dict):
                    return self._send(400, {"error": "body must be a JSON object"})
                con = sqlite3.connect(shop.db_path)
                con.row_factory = sqlite3.Row
                try:
                    if self.path == "/customers":
                        email, name = body.get("email"), body.get("name")
                        if not isinstance(email, str) or not EMAIL.match(email):
                            return self._send(400, {"error": "email is invalid"})
                        if not isinstance(name, str) or not 2 <= len(name) <= 50:
                            return self._send(400, {"error": "name must be 2-50 characters"})
                        cid = str(uuid.uuid4())
                        try:
                            con.execute("INSERT INTO customers VALUES (?, ?, ?)", (cid, email, name))
                        except sqlite3.IntegrityError:
                            return self._send(409, {"error": "email already registered"})
                        con.commit()
                        return self._send(201, {"id": cid, "email": email, "name": name})
                    if self.path == "/orders":
                        cid, items, channel = body.get("customerId"), body.get("items"), body.get("channel")
                        if not isinstance(cid, str):
                            return self._send(400, {"error": "customerId is required"})
                        if channel not in CHANNELS:
                            return self._send(400, {"error": "channel must be web, mobile or pos"})
                        if not isinstance(items, list) or not items:
                            return self._send(400, {"error": "items must be a non-empty array"})
                        if not con.execute("SELECT 1 FROM customers WHERE id = ?", (cid,)).fetchone():
                            return self._send(404, {"error": "customer not found"})
                        total, priced = 0.0, []
                        for it in items:
                            if not isinstance(it, dict) or it.get("sku") not in PRICES:
                                return self._send(400, {"error": "unknown sku"})
                            qty = it.get("qty")
                            if qty < 1 or qty > 10:  # BUG 2: a string qty raises TypeError -> 500
                                return self._send(400, {"error": "qty must be 1-10"})
                            price = PRICES[it["sku"]]
                            total += price * qty
                            priced.append((it["sku"], qty, price))
                        oid = str(uuid.uuid4())
                        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        con.execute("INSERT INTO orders VALUES (?, ?, 'pending', ?, ?, ?, NULL)", (oid, cid, total, channel, now))
                        con.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?)", [(oid, *p) for p in priced])
                        con.commit()
                        o = shop.order(con, oid)
                        o["total"] = round(sum(p for _, _, p in priced), 2)  # BUG 1: ignores qty in the response
                        return self._send(201, o)
                    return self._send(404, {"error": "not found"})
                except TypeError:
                    return self._send(500, {"error": "internal server error"})
                finally:
                    con.close()

            def do_DELETE(self):  # noqa: N802
                if not self._authed():
                    return
                m = re.fullmatch(r"/orders/([^/]+)", self.path)
                if not m:
                    return self._send(404, {"error": "not found"})
                con = sqlite3.connect(shop.db_path)
                try:
                    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    cur = con.execute("UPDATE orders SET status = 'cancelled', deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                                      (now, m.group(1)))
                    con.commit()
                    return self._send(204) if cur.rowcount else self._send(404, {"error": "order not found"})
                finally:
                    con.close()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @staticmethod
    def order(con, oid):
        r = con.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
        if not r:
            return None
        items = [{"sku": i["sku"], "qty": i["qty"], "price": i["price"]}
                 for i in con.execute("SELECT * FROM order_items WHERE order_id = ?", (oid,))]
        return {"id": r["id"], "customerId": r["customer_id"], "status": r["status"], "total": r["total"],
                "channel": r["channel"], "items": items}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=str(Path(__file__).parent / "shop.db"))
    a = ap.parse_args()
    s = Shop(Path(a.db), a.port)
    print(f"Demo Shop API on {s.url} (Bearer test-token), database {a.db}  (Ctrl-C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        s.close()
