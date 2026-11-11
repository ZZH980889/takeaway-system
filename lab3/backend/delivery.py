"""Atomic rider assignment, capacity constraints, and fulfillment."""

from .common import active_business, get_row, log, order_access, require, text
from .orders import change_status

OCCUPIED = ("offered", "accepted", "delivering")


def occupied(connection, rider_id):
    """Count authoritative task rows rather than maintaining a drifting counter."""
    return connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE rider_id=? AND status IN ('offered','accepted','delivering')",
        (rider_id,),
    ).fetchone()[0]


def assignment_log(connection, now, task, rider, action, reason=""):
    """Keep a durable history even after a task is reassigned."""
    connection.execute(
        "INSERT INTO assignments(task_id,rider_id,action,reason,created_at) VALUES(?,?,?,?,?)",
        (task, rider, action, reason, now),
    )


def list_tasks(connection, actor):
    """Unassigned hall entries intentionally omit the customer's private address."""
    if actor["role"] == "admin":
        rows = connection.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 500")
    else:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE rider_id=? OR (status='open' AND escalated=0) "
            "ORDER BY id DESC LIMIT 500",
            (actor["id"],),
        )
    result = []
    for row in rows:
        task = dict(row)
        task["mine"] = task["rider_id"] == actor["id"]
        result.append(task)
    return {"items": result, "occupied": occupied(connection, actor["id"]), "capacity": 3}


def act_on_task(connection, now, actor, identifier, data):
    """Every operation executes under one immediate write transaction."""
    task = get_row(connection, "tasks", identifier)
    order = get_row(connection, "orders", task["order_id"])
    action = data.get("action")
    require(order["status"] in {"ready", "delivering"}, "订单不可配送", 409)
    if action == "claim":
        active_business(actor)
        require(actor["online"], "请先上线", 409)
        require(task["status"] == "open" and not task["escalated"], "任务已被承接或交平台处理", 409)
        require(occupied(connection, actor["id"]) < 3, "骑手名额已满", 409)
        connection.execute(
            "UPDATE tasks SET rider_id=?,status='accepted' WHERE id=?", (actor["id"], identifier)
        )
    else:
        require(task["rider_id"] == actor["id"], "无权操作他人任务", 403)
        if action in {"accept", "reject"}:
            require(task["status"] == "offered", "派单已失效", 409)
            if action == "accept":
                active_business(actor)
                require(actor["online"], "请先上线", 409)
                require(occupied(connection, actor["id"]) <= 3, "骑手名额已满", 409)
                connection.execute("UPDATE tasks SET status='accepted' WHERE id=?", (identifier,))
            else:
                connection.execute(
                    "UPDATE tasks SET rider_id=NULL,status='open',offered_at=NULL WHERE id=?", (identifier,)
                )
        elif action == "abandon":
            require(task["status"] == "accepted", "取餐后不能直接放弃", 409)
            reason = text(data.get("reason"), "放弃原因", 500)
            connection.execute(
                "UPDATE tasks SET rider_id=NULL,status='open',offered_at=NULL WHERE id=?", (identifier,)
            )
            assignment_log(connection, now, identifier, actor["id"], action, reason)
        elif action == "pickup":
            require(task["status"] == "accepted" and order["status"] == "ready", "当前不能取餐", 409)
            connection.execute("UPDATE tasks SET status='delivering' WHERE id=?", (identifier,))
            change_status(connection, order, "delivering", now)
        elif action == "deliver":
            require(task["status"] == "delivering" and order["status"] == "delivering", "当前不能送达", 409)
            connection.execute(
                "UPDATE tasks SET status='delivered',completed_at=? WHERE id=?", (now, identifier)
            )
            change_status(connection, order, "delivered", now, "delivered_at")
        else:
            require(False, "未知配送操作")
    if action != "abandon":
        assignment_log(connection, now, identifier, actor["id"], action)
    log(connection, now, actor["id"], action, "order", order["id"], {"task_id": identifier})
    return get_row(connection, "tasks", identifier)


def position(connection, now, actor, identifier):
    """Only an owner of an actively delivering order may see simulated location."""
    order = get_row(connection, "orders", identifier)
    order_access(connection, actor, order)
    require(order["status"] == "delivering", "仅配送中可查询骑手位置", 409)
    tick = int(now // 10)
    # A deterministic route avoids collecting any actual device coordinates.
    return {
        "simulated": True,
        "refresh_seconds": 10,
        "tick": tick,
        "x": 20 + tick % 60,
        "y": 50,
        "route": [[10, 50], [90, 50]],
    }


def offer_task(connection, now, task):
    """Prefer least-loaded online rider, then least recently offered for this task."""
    candidates = connection.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM tasks t WHERE t.rider_id=u.id AND "
        "t.status IN ('offered','accepted','delivering')) load, "
        "COALESCE((SELECT MAX(a.created_at) FROM assignments a "
        "WHERE a.rider_id=u.id AND a.task_id=?),0) last_offer "
        "FROM users u WHERE u.role='rider' AND u.online=1 AND u.enabled=1 "
        "AND u.approval='approved' ORDER BY load,last_offer,u.id",
        (task["id"],),
    ).fetchall()
    for rider in candidates:
        if rider["load"] >= 3:
            continue
        connection.execute(
            "UPDATE tasks SET rider_id=?,status='offered',offered_at=? WHERE id=?",
            (rider["id"], now, task["id"]),
        )
        assignment_log(connection, now, task["id"], rider["id"], "offer")
        log(connection, now, None, "自动派单", "order", task["order_id"], {"rider_id": rider["id"]})
        return True
    return False
