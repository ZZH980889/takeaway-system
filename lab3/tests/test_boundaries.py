"""Boundary checks supplement happy-path workflow tests."""

from concurrent.futures import ThreadPoolExecutor

from backend.db import transaction
from backend.jobs import scan
from test_workflows import Client, create_order, order_action, ready_order


def test_three_task_capacity(env):
    app, _ = env
    tasks = [ready_order(app)[3] for _ in range(4)]
    rider = Client(app, "rider")
    codes = [
        rider.post(f"/api/tasks/{task['id']}/actions", {"action": "claim"}).status_code for task in tasks
    ]
    assert codes == [200, 200, 200, 409]
    assert rider.get("/api/tasks").json["occupied"] == 3


def test_abandon_does_not_restart_clock(env):
    app, clock = env
    _, _, _, task = ready_order(app)
    rider = Client(app, "rider")
    rider.post(f"/api/tasks/{task['id']}/actions", {"action": "claim"})
    clock[0] += 100
    result = rider.post(f"/api/tasks/{task['id']}/actions", {"action": "abandon", "reason": "车辆故障"})
    assert result.status_code == 200
    assert result.json["ready_at"] == task["ready_at"]
    assert rider.get("/api/tasks").json["occupied"] == 0


def test_rider_cannot_abandon_after_pickup(env):
    app, _ = env
    _, _, _, task = ready_order(app)
    rider = Client(app, "rider")
    rider.post(f"/api/tasks/{task['id']}/actions", {"action": "claim"})
    rider.post(f"/api/tasks/{task['id']}/actions", {"action": "pickup"})
    result = rider.post(f"/api/tasks/{task['id']}/actions", {"action": "abandon", "reason": "想放弃"})
    assert result.status_code == 409


def test_automatic_completion_boundary(env):
    app, clock = env
    _, _, order, task = ready_order(app)
    rider = Client(app, "rider")
    for action in ("claim", "pickup", "deliver"):
        rider.post(f"/api/tasks/{task['id']}/actions", {"action": action})
    with transaction(app.config["DATABASE"], True) as db:
        assert scan(db, clock[0] + 1799)["completed"] == 0
        assert scan(db, clock[0] + 1800)["completed"] == 1
        assert scan(db, clock[0] + 1800)["completed"] == 0
    assert Client(app).get(f"/api/orders/{order['id']}").json["status"] == "completed"


def test_disabled_rider_can_finish_existing_task(env):
    app, _ = env
    _, _, _, task = ready_order(app)
    rider = Client(app, "rider")
    rider.post(f"/api/tasks/{task['id']}/actions", {"action": "claim"})
    admin = Client(app, "admin")
    admin.request("PATCH", "/api/users/3", {"enabled": False})
    assert rider.post(f"/api/tasks/{task['id']}/actions", {"action": "pickup"}).status_code == 200
    assert rider.post(f"/api/tasks/{task['id']}/actions", {"action": "deliver"}).status_code == 200
    assert rider.post("/api/rider/online", {"online": True}).status_code == 403


def test_pending_account_must_be_approved(env):
    app, _ = env
    guest = app.test_client()
    csrf = guest.get("/api/session").json["csrf"]
    headers = {"X-CSRF-Token": csrf}
    result = guest.post(
        "/api/register",
        json={"account": "new_rider", "password": "Demo12345!", "phone": "13900000001", "role": "rider"},
        headers=headers,
    )
    assert result.status_code == 201
    rider = Client(app, "new_rider")
    assert rider.post("/api/rider/online", {"online": True}).status_code == 403
    admin = Client(app, "admin")
    admin.request("PATCH", f"/api/users/{result.json['id']}", {"approval": "approved"})
    assert rider.post("/api/rider/online", {"online": True}).status_code == 200


def test_out_of_region_address_rejected(env):
    customer = Client(env[0])
    address = customer.post(
        "/api/addresses",
        {"region": "鼓楼校区", "detail": "学生宿舍1栋", "contact": "同学", "phone": "13800000001"},
    ).json
    customer.put("/api/cart", {"dish_id": 1, "quantity": 1})
    assert customer.post("/api/orders", {"shop_id": 1, "address_id": address["id"]}).status_code == 400


def test_partial_refund_updates_original_completion_bucket(env):
    app, clock = env
    customer, _, order, task = ready_order(app)
    rider = Client(app, "rider")
    for action in ("claim", "pickup", "deliver"):
        rider.post(f"/api/tasks/{task['id']}/actions", {"action": action})
    order_action(customer, order["id"], "complete")
    merchant = Client(app, "merchant")
    original = merchant.get("/api/statistics").json["items"][0]
    clock[0] += 86400
    admin = Client(app, "admin")
    refund = admin.post(
        f"/api/orders/{order['id']}/refunds",
        {"food_amount": "3.00", "delivery_amount": "1.00", "reason": "核实部分补偿"},
    ).json
    admin.post(f"/api/refunds/{refund['id']}/process", {"outcome": "success"})
    changed = merchant.get("/api/statistics").json["items"][0]
    assert changed["period"] == original["period"]
    assert changed["food_net"] == original["food_net"] - 300
    assert changed["delivery_net"] == original["delivery_net"] - 100


def test_cancel_accept_race_has_one_valid_winner(env):
    app, _ = env
    customer, merchant = Client(app), Client(app, "merchant")
    order = create_order(customer).json
    order_action(customer, order["id"], "pay")
    version = customer.get(f"/api/orders/{order['id']}").json["version"]

    def act(pair):
        client, action = pair
        return client.post(
            f"/api/orders/{order['id']}/actions", {"action": action, "version": version}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(act, [(customer, "cancel"), (merchant, "accept")]))
    assert sorted(codes) == [200, 409]


def test_second_rider_cannot_operate_accepted_task(env):
    app, _ = env
    _, _, _, task = ready_order(app)
    Client(app, "rider").post(f"/api/tasks/{task['id']}/actions", {"action": "claim"})
    result = Client(app, "rider2").post(f"/api/tasks/{task['id']}/actions", {"action": "pickup"})
    assert result.status_code == 403


def test_new_address_does_not_mutate_order_snapshot(env):
    app, _ = env
    client = Client(app)
    order = create_order(client).json
    client.put(
        "/api/addresses/1",
        {"region": "仙林校区", "detail": "学生宿舍9栋", "contact": "改名", "phone": "13800000001"},
    )
    unchanged = client.get(f"/api/orders/{order['id']}").json
    assert unchanged["address"]["detail"] == "学生宿舍1栋"


def test_demo_time_scale_does_not_change_default(env):
    app, _ = env
    assert app.config["TIME_SCALE"] == 1


def test_admin_registration_is_forbidden(env):
    app, _ = env
    guest = app.test_client()
    token = guest.get("/api/session").json["csrf"]
    result = guest.post(
        "/api/register",
        json={"account": "eviladmin", "role": "admin", "password": "Demo12345!", "phone": "13900000009"},
        headers={"X-CSRF-Token": token},
    )
    assert result.status_code == 400


def test_dish_version_prevents_stale_edit(env):
    merchant = Client(env[0], "merchant")
    dish = merchant.get("/api/dishes").json[-1]
    payload = {
        "name": dish["name"],
        "category": dish["category"],
        "price": "19.00",
        "stock": 3,
        "version": dish["version"],
    }
    assert merchant.put(f"/api/dishes/{dish['id']}", payload).status_code == 200
    assert merchant.put(f"/api/dishes/{dish['id']}", payload).status_code == 409


def test_admin_continue_resumes_dispatch_after_escalation(env):
    app, clock = env
    _, _, order, task = ready_order(app)
    with transaction(app.config["DATABASE"], True) as db:
        assert scan(db, clock[0] + 1200)["escalated"] == 1
        exception = db.execute("SELECT id FROM exceptions WHERE order_id=?", (order["id"],)).fetchone()
    clock[0] += 1200
    admin = Client(app, "admin")
    from backend.refunds import resolve_exception

    with transaction(app.config["DATABASE"], True) as db:
        resolve_exception(
            db, clock[0], admin.user, exception["id"], {"decision": "continue", "reason": "已联系骑手"}
        )
    with transaction(app.config["DATABASE"], True) as db:
        assert scan(db, clock[0])["escalated"] == 0
        assert scan(db, clock[0] + 60)["offered"] == 1
        current = db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
        assert current["ready_at"] == task["ready_at"]
        assert current["status"] == "offered"
