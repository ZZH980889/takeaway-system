"""Executable acceptance scenarios for transactions, roles, and deadlines."""

from concurrent.futures import ThreadPoolExecutor
import uuid
import pytest
from backend.app import create_app
from backend.db import transaction
from backend.jobs import scan


class Client:
    """Simulate a browser with its own signed session and CSRF token."""

    def __init__(self, app, account="customer"):
        self.client = app.test_client()
        self.token = self.client.get("/api/session").json["csrf"]
        response = self.post(
            "/api/login", {"account": account, "password": "Demo12345!", "admin": account == "admin"}
        )
        assert response.status_code == 200, response.json
        self.user = response.json["user"]
        self.token = response.json["csrf"]

    def request(self, method, url, data, key=None):
        return self.client.open(
            url,
            method=method,
            json=data,
            headers={"X-CSRF-Token": self.token, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    def post(self, url, data, key=None):
        return self.request("POST", url, data, key)

    def put(self, url, data, key=None):
        return self.request("PUT", url, data, key)

    def get(self, url):
        return self.client.get(url)


def create_order(client, key=None, dish=1, shop=1):
    response = client.put("/api/cart", {"dish_id": dish, "quantity": 1})
    assert response.status_code == 200, response.json
    address = client.get("/api/addresses").json[0]["id"]
    return client.post("/api/orders", {"shop_id": shop, "address_id": address}, key)


def order_action(client, identifier, action, **kwargs):
    detail = client.get(f"/api/orders/{identifier}")
    assert detail.status_code == 200, detail.json
    return client.post(
        f"/api/orders/{identifier}/actions", {"action": action, "version": detail.json["version"], **kwargs}
    )


def ready_order(app):
    customer = Client(app)
    merchant = Client(app, "merchant")
    order = create_order(customer).json
    assert order_action(customer, order["id"], "pay").status_code == 200
    assert order_action(merchant, order["id"], "accept").status_code == 200
    assert order_action(merchant, order["id"], "ready").status_code == 200
    with transaction(app.config["DATABASE"]) as db:
        task = dict(db.execute("SELECT * FROM tasks WHERE order_id=?", (order["id"],)).fetchone())
    return customer, merchant, order, task


def test_normal_lifecycle(env):
    app, _ = env
    customer, _, order, task = ready_order(app)
    rider = Client(app, "rider")
    for action in ("claim", "pickup", "deliver"):
        assert rider.post(f"/api/tasks/{task['id']}/actions", {"action": action}).status_code == 200
    assert customer.get(f"/api/orders/{order['id']}/position").status_code == 409
    assert order_action(customer, order["id"], "complete").json["status"] == "completed"


def test_new_app_reopens_same_data(env):
    app, clock = env
    client = Client(app)
    order = create_order(client).json
    reopened = create_app(
        {
            "TESTING": True,
            "DATABASE": app.config["DATABASE"],
            "SECRET_KEY": "testing-only",
            "CLOCK": lambda: clock[0],
        }
    )
    assert Client(reopened).get(f"/api/orders/{order['id']}").status_code == 200


def test_password_not_plaintext(env):
    app, _ = env
    with transaction(app.config["DATABASE"]) as db:
        rows = db.execute("SELECT password_hash FROM users").fetchall()
        assert all(row[0] != "Demo12345!" for row in rows)
        assert all(len(row[0]) > 50 for row in rows)


@pytest.mark.parametrize("url", ["/api/orders", "/api/users", "/api/cart", "/api/tasks"])
def test_anonymous_denied(env, url):
    assert env[0].test_client().get(url).status_code == 401


@pytest.mark.parametrize(
    "account,url",
    [
        ("customer", "/api/users"),
        ("customer", "/api/logs"),
        ("merchant", "/api/tasks"),
        ("rider", "/api/statistics"),
    ],
)
def test_role_access(env, account, url):
    assert Client(env[0], account).get(url).status_code == 403


def test_order_ownership(env):
    app, _ = env
    order = create_order(Client(app)).json
    other = Client(app, "customer2")
    assert other.get(f"/api/orders/{order['id']}").status_code == 403
    assert other.get("/api/orders").json == []


def test_csrf(env):
    client = Client(env[0])
    assert client.client.post("/api/logout", json={}).status_code == 403


def test_idempotent_create(env):
    app, _ = env
    customer = Client(app)
    first = create_order(customer, key="same-order-request")
    address = customer.get("/api/addresses").json[0]["id"]
    second = customer.post("/api/orders", {"shop_id": 1, "address_id": address}, "same-order-request")
    assert first.json["id"] == second.json["id"]
    with transaction(app.config["DATABASE"]) as db:
        assert db.execute("SELECT stock FROM dishes WHERE id=1").fetchone()[0] == 11


def test_same_food_new_intent_allowed(env):
    customer = Client(env[0])
    assert create_order(customer).json["id"] != create_order(customer).json["id"]


def test_request_key_conflict(env):
    customer = Client(env[0])
    first = customer.put("/api/cart", {"dish_id": 1, "quantity": 1}, "fixed-key-0001")
    assert first.status_code == 200
    assert customer.put("/api/cart", {"dish_id": 1, "quantity": 2}, "fixed-key-0001").status_code == 409


def test_concurrent_last_stock(env):
    app, _ = env
    with transaction(app.config["DATABASE"], True) as db:
        db.execute("UPDATE dishes SET stock=1 WHERE id=1")
    clients = [Client(app), Client(app, "customer2")]
    for client in clients:
        assert client.put("/api/cart", {"dish_id": 1, "quantity": 1}).status_code == 200

    def place(client):
        address = client.get("/api/addresses").json[0]["id"]
        return client.post("/api/orders", {"shop_id": 1, "address_id": address}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(place, clients))
    assert sorted(codes) == [200, 409]
    with transaction(app.config["DATABASE"]) as db:
        assert db.execute("SELECT stock FROM dishes WHERE id=1").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_concurrent_claim(env):
    app, _ = env
    _, _, _, task = ready_order(app)
    riders = [Client(app, "rider"), Client(app, "rider2")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(
            pool.map(
                lambda rider: rider.post(f"/api/tasks/{task['id']}/actions", {"action": "claim"}).status_code,
                riders,
            )
        )
    assert sorted(codes) == [200, 409]


def test_payment_failure_does_not_pay(env):
    client = Client(env[0])
    order = create_order(client).json
    result = order_action(client, order["id"], "pay", outcome="failure")
    assert result.json["paid"] is False
    assert client.get(f"/api/orders/{order['id']}").json["status"] == "pending_payment"


def test_payment_timeout_releases_once(env):
    app, clock = env
    client = Client(app)
    order = create_order(client).json
    clock[0] += 300
    with transaction(app.config["DATABASE"], True) as db:
        scan(db, clock[0])
        scan(db, clock[0])
        assert db.execute("SELECT stock FROM dishes WHERE id=1").fetchone()[0] == 12
    assert client.get(f"/api/orders/{order['id']}").json["status"] == "closed"
    assert order_action(client, order["id"], "pay").status_code == 409


def test_acceptance_timeout_refund_is_independent(env):
    app, clock = env
    client = Client(app)
    order = create_order(client).json
    order_action(client, order["id"], "pay")
    clock[0] += 900
    with transaction(app.config["DATABASE"], True) as db:
        scan(db, clock[0])
    detail = client.get(f"/api/orders/{order['id']}").json
    assert detail["status"] == "cancelled"
    assert detail["refunds"][0]["status"] == "processing"


def test_price_confirmation_and_snapshot(env):
    app, _ = env
    client = Client(app)
    client.put("/api/cart", {"dish_id": 1, "quantity": 1})
    with transaction(app.config["DATABASE"], True) as db:
        db.execute("UPDATE dishes SET price=1900 WHERE id=1")
    address = client.get("/api/addresses").json[0]["id"]
    payload = {"shop_id": 1, "address_id": address}
    assert client.post("/api/orders", payload).json["code"] == "price_changed"
    payload["confirmed_prices"] = {"1": 1900}
    order = client.post("/api/orders", payload).json
    with transaction(app.config["DATABASE"], True) as db:
        db.execute("UPDATE dishes SET name='new',price=3000 WHERE id=1")
    detail = client.get(f"/api/orders/{order['id']}").json
    assert detail["items"][0]["unit_price"] == 1900
    assert detail["items"][0]["name"] == "宫保鸡丁饭"


def test_stale_order_page(env):
    app, _ = env
    client = Client(app)
    order = create_order(client).json
    order_action(client, order["id"], "pay")
    result = client.post(
        f"/api/orders/{order['id']}/actions", {"action": "cancel", "version": order["version"]}
    )
    assert result.status_code == 409


def test_refund_failure_retry_keeps_order_cancelled(env):
    app, _ = env
    customer, _, order, task = ready_order(app)
    rider = Client(app, "rider")
    rider.post(f"/api/tasks/{task['id']}/actions", {"action": "claim"})
    rider.post(f"/api/tasks/{task['id']}/actions", {"action": "pickup"})
    issue = rider.post(f"/api/orders/{order['id']}/exceptions", {"reason": "无法联系顾客"}).json
    admin = Client(app, "admin")
    result = admin.post(
        f"/api/exceptions/{issue['id']}/resolve",
        {
            "decision": "terminate",
            "reason": "核实无法配送",
            "food_amount": "18.00",
            "delivery_amount": "5.00",
        },
    ).json
    refund_id = result["refund"]["id"]
    failed = admin.post(f"/api/refunds/{refund_id}/process", {"outcome": "failure"})
    assert failed.json["status"] == "failed"
    assert customer.get(f"/api/orders/{order['id']}").json["status"] == "cancelled"
    assert rider.get("/api/tasks").json["occupied"] == 0
    success = admin.post(f"/api/refunds/{refund_id}/process", {"outcome": "success"})
    assert success.json["status"] == "refunded"
    assert admin.post(f"/api/refunds/{refund_id}/process", {"outcome": "success"}).status_code == 409
    assert (
        admin.post(
            f"/api/orders/{order['id']}/refunds", {"food_amount": "0.01", "reason": "再次退款"}
        ).status_code
        == 409
    )
    with transaction(app.config["DATABASE"]) as db:
        assert db.execute("SELECT stock FROM dishes WHERE id=1").fetchone()[0] == 11


def test_dispatch_timing_and_capacity(env):
    app, clock = env
    _, _, order, task = ready_order(app)
    with transaction(app.config["DATABASE"], True) as db:
        assert scan(db, clock[0] + 479)["offered"] == 0
        assert scan(db, clock[0] + 480)["offered"] == 1
        assigned = dict(db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone())
        assert assigned["status"] == "offered"
        assert scan(db, clock[0] + 540)["expired_offers"] == 1
        assert scan(db, clock[0] + 1200)["escalated"] == 1
        assert db.execute("SELECT ready_at FROM tasks WHERE id=?", (task["id"],)).fetchone()[0] == clock[0]
        assert (
            db.execute("SELECT COUNT(*) FROM exceptions WHERE order_id=?", (order["id"],)).fetchone()[0] == 1
        )


@pytest.mark.parametrize("quantity", [-1, 1.5, True, "2"])
def test_invalid_cart_quantity(env, quantity):
    assert Client(env[0]).put("/api/cart", {"dish_id": 1, "quantity": quantity}).status_code == 400


@pytest.mark.parametrize("price", [0, -1, "1.001", "NaN", "Infinity"])
def test_invalid_dish_prices(env, price):
    merchant = Client(env[0], "merchant")
    result = merchant.post("/api/dishes", {"name": "新菜", "category": "盖饭", "price": price, "stock": 1})
    assert result.status_code == 400


def test_paused_shop_does_not_cancel_existing(env):
    app, _ = env
    customer = Client(app)
    order = create_order(customer).json
    merchant = Client(app, "merchant")
    assert merchant.request("PATCH", "/api/shops/1", {"is_open": False}).status_code == 200
    assert create_order(customer).status_code == 409
    assert customer.get(f"/api/orders/{order['id']}").json["status"] == "pending_payment"


def test_sms_expiry_and_one_time_use(env):
    app, clock = env
    client = app.test_client()
    token = client.get("/api/session").json["csrf"]
    headers = {"X-CSRF-Token": token}
    code = client.post("/api/sms", json={"phone": "13800000001"}, headers=headers).json["demo_code"]
    result = client.post(
        "/api/login", json={"mode": "phone", "phone": "13800000001", "code": code}, headers=headers
    )
    assert result.status_code == 200
    with transaction(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM sms_codes").fetchone()[0] == 0


def test_failed_audit_and_money_filter(env):
    app, _ = env
    customer = Client(app)
    create_order(customer)
    assert len(customer.get("/api/orders?min_amount=23&max_amount=23").json) == 1
    assert customer.get("/api/orders?min_amount=24").json == []
    customer.post("/api/orders/1/actions", {"action": "ready", "version": 1})
    logs = Client(app, "admin").get("/api/logs?result=denied").json
    assert logs and logs[0]["result"] == "denied"
