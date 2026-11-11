"""Order lifecycle, immutable snapshots, cancellation, and payment."""

import json
from .catalog import cart_rows
from .common import active_business, get_row, integer, log, order_access, require

TERMINAL = {"completed", "cancelled", "closed"}
REJECTION_REASONS = {"食材售罄", "设备故障", "门店打烊"}


def change_status(connection, order, target, now, timestamp=None, reason=""):
    """Use optimistic version checks even though write transactions serialize writers."""
    fields = ["status=?", "version=version+1", "reason=?"]
    values = [target, reason]
    if timestamp:
        require(
            timestamp in {"paid_at", "ready_at", "delivered_at", "completed_at", "cancelled_at"},
            "时间字段非法",
        )
        fields.append(f"{timestamp}=?")
        values.append(now)
    cursor = connection.execute(
        "UPDATE orders SET " + ",".join(fields) + " WHERE id=? AND version=?",
        (*values, order["id"], order["version"]),
    )
    require(cursor.rowcount == 1, "订单状态已变化，请刷新", 409, "stale_version")


def require_version(order, data):
    """Reject requests made from an obsolete order page."""
    require(
        integer(data.get("version"), "订单版本", 1) == order["version"],
        "订单已变化，请刷新后操作",
        409,
        "stale_version",
    )


def create_order(connection, now, actor, data):
    """Create one-shop order and reserve its stock in the caller's transaction."""
    active_business(actor)
    address = get_row(connection, "addresses", integer(data.get("address_id"), "地址编号", 1))
    require(address["customer_id"] == actor["id"] and not address["deleted"], "无权使用该地址", 403)
    shop = get_row(connection, "shops", integer(data.get("shop_id"), "店铺编号", 1))
    merchant = get_row(connection, "users", shop["merchant_id"])
    require(
        shop["enabled"] and shop["is_open"] and merchant["enabled"] and merchant["approval"] == "approved",
        "店铺当前不接受新订单",
        409,
    )
    require(address["region"] == shop["region"], "地址超出配送范围")
    rows = [row for row in cart_rows(connection, actor) if row["shop_id"] == shop["id"]]
    require(rows, "该店购物车为空")
    require(all(not row["deleted"] and row["on_sale"] for row in rows), "部分菜品已下架，请调整购物车", 409)
    expected = data.get("confirmed_prices", {})
    require(isinstance(expected, dict), "价格确认格式错误")
    changed = [
        row
        for row in rows
        if row["seen_price"] != row["price"] and expected.get(str(row["dish_id"])) != row["price"]
    ]
    require(
        not changed,
        "菜品价格已变化，请确认最新价格",
        409,
        "price_changed",
        [
            {
                "dish_id": row["dish_id"],
                "name": row["name"],
                "old_price": row["seen_price"],
                "price": row["price"],
            }
            for row in changed
        ],
    )
    total = sum(row["price"] * row["quantity"] for row in rows)
    snapshot = {key: address[key] for key in ("region", "detail", "contact", "phone")}
    identifier = connection.execute(
        "INSERT INTO orders(customer_id,shop_id,status,food_amount,address_snapshot,"
        "created_at) VALUES(?,?,'pending_payment',?,?,?)",
        (actor["id"], shop["id"], total, json.dumps(snapshot, ensure_ascii=False), now),
    ).lastrowid
    for row in rows:
        cursor = connection.execute(
            "UPDATE dishes SET stock=stock-?,version=version+1 "
            "WHERE id=? AND stock>=? AND on_sale=1 AND deleted=0",
            (row["quantity"], row["dish_id"], row["quantity"]),
        )
        require(cursor.rowcount == 1, "菜品库存不足，订单未创建", 409, "out_of_stock")
        connection.execute(
            "INSERT INTO order_items(order_id,dish_id,name,unit_price,quantity) VALUES(?,?,?,?,?)",
            (identifier, row["dish_id"], row["name"], row["price"], row["quantity"]),
        )
        connection.execute(
            "DELETE FROM cart WHERE customer_id=? AND dish_id=?", (actor["id"], row["dish_id"])
        )
    log(
        connection,
        now,
        actor["id"],
        "创建订单",
        "order",
        identifier,
        {"food_amount": total, "delivery_amount": 500},
    )
    return order_detail(connection, actor, identifier)


def order_detail(connection, actor, identifier):
    """Filter personal address details for task/order ownership."""
    order = get_row(connection, "orders", identifier)
    order_access(connection, actor, order)
    order["address"] = json.loads(order.pop("address_snapshot"))
    order["total"] = order["food_amount"] + order["delivery_amount"]
    order["items"] = [
        dict(row)
        for row in connection.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (identifier,))
    ]
    order["refunds"] = [
        dict(row)
        for row in connection.execute("SELECT * FROM refunds WHERE order_id=? ORDER BY id", (identifier,))
    ]
    return order


def list_orders(connection, actor, query):
    """Combine time, amount, state and identifier filters inside the access scope."""
    clauses = ["1=1"]
    values = []
    if actor["role"] == "customer":
        clauses.append("o.customer_id=?")
        values.append(actor["id"])
    elif actor["role"] == "merchant":
        clauses.append("s.merchant_id=?")
        values.append(actor["id"])
    elif actor["role"] == "rider":
        clauses.append("EXISTS(SELECT 1 FROM tasks t WHERE t.order_id=o.id AND t.rider_id=?)")
        values.append(actor["id"])
    for field in ("id", "status", "shop_id"):
        if query.get(field):
            clauses.append(f"o.{field}=?")
            values.append(query[field])
    for key, field, operator in [
        ("from", "created_at", ">="),
        ("to", "created_at", "<="),
        ("min_amount", "food_amount+o.delivery_amount", ">="),
        ("max_amount", "food_amount+o.delivery_amount", "<="),
    ]:
        if query.get(key) not in (None, ""):
            from .common import money

            value = money(query[key]) if "amount" in key else float(query[key])
            clauses.append(f"o.{field}{operator}?")
            values.append(value)
    limit = min(max(int(query.get("limit", 100)), 1), 500)
    offset = max(int(query.get("offset", 0)), 0)
    sql = (
        "SELECT o.*,s.name shop_name FROM orders o JOIN shops s ON s.id=o.shop_id "
        "WHERE " + " AND ".join(clauses) + " ORDER BY o.id DESC LIMIT ? OFFSET ?"
    )
    result = []
    for row in connection.execute(sql, (*values, limit, offset)):
        item = dict(row)
        item.pop("address_snapshot")
        item["total"] = item["food_amount"] + item["delivery_amount"]
        result.append(item)
    return result


def release_inventory(connection, identifier):
    """Only call while transitioning out of a not-yet-prepared state."""
    for item in connection.execute("SELECT * FROM order_items WHERE order_id=?", (identifier,)):
        connection.execute(
            "UPDATE dishes SET stock=stock+?,version=version+1 WHERE id=?",
            (item["quantity"], item["dish_id"]),
        )


def close_order(connection, now, order, reason, actor_id=None, unpaid_timeout=False):
    """Close task and reserve-independent rider capacity in the same transaction."""
    require(order["status"] not in TERMINAL, "订单已终止", 409)
    if order["status"] in {"pending_payment", "pending_acceptance"}:
        release_inventory(connection, order["id"])
    change_status(connection, order, "closed" if unpaid_timeout else "cancelled", now, "cancelled_at", reason)
    connection.execute(
        "UPDATE tasks SET status='closed',completed_at=? WHERE order_id=?",
        (now, order["id"]),
    )
    log(
        connection,
        now,
        actor_id,
        "关闭订单" if unpaid_timeout else "取消订单",
        "order",
        order["id"],
        {"reason": reason},
    )


def act_on_order(connection, now, actor, identifier, data):
    """Public transitions; administrative termination belongs to exception service."""
    from .refunds import request_refund

    order = get_row(connection, "orders", identifier)
    order_access(connection, actor, order)
    require_version(order, data)
    action = data.get("action")
    role = actor["role"]
    if action == "pay":
        require(role == "customer", "仅顾客可付款", 403)
        active_business(actor)
        require(order["status"] == "pending_payment", "当前订单不可付款", 409)
        if data.get("outcome", "success") == "failure":
            log(connection, now, actor["id"], "模拟支付", "order", identifier, result="failed")
            return {"paid": False, "order": order_detail(connection, actor, identifier)}
        require(data.get("outcome", "success") == "success", "支付结果非法")
        connection.execute(
            "INSERT INTO payments(order_id,amount,created_at) VALUES(?,?,?)",
            (identifier, order["food_amount"] + 500, now),
        )
        change_status(connection, order, "pending_acceptance", now, "paid_at")
    elif action in {"cancel", "reject"}:
        require(role == ("customer" if action == "cancel" else "merchant"), "无权执行操作", 403)
        allowed = {"pending_payment", "pending_acceptance"} if action == "cancel" else {"pending_acceptance"}
        require(order["status"] in allowed, "已开始制作，请申请平台处理", 409)
        reason = "顾客取消" if action == "cancel" else data.get("reason")
        require(action != "reject" or reason in REJECTION_REASONS, "拒单理由不在允许范围")
        close_order(connection, now, order, reason, actor["id"])
        if order["status"] == "pending_acceptance":
            request_refund(connection, now, identifier, order["food_amount"], 500, reason)
    elif action == "accept":
        require(role == "merchant" and order["status"] == "pending_acceptance", "当前不能接单", 409)
        change_status(connection, order, "preparing", now)
    elif action == "ready":
        require(role == "merchant" and order["status"] == "preparing", "当前不能出餐", 409)
        change_status(connection, order, "ready", now, "ready_at")
        connection.execute(
            "INSERT INTO tasks(order_id,status,ready_at) VALUES(?,'open',?)", (identifier, now)
        )
    elif action == "complete":
        require(role == "customer" and order["status"] == "delivered", "当前不能确认收餐", 409)
        change_status(connection, order, "completed", now, "completed_at")
    else:
        require(False, "未知订单操作")
    log(connection, now, actor["id"], action, "order", identifier)
    return order_detail(connection, actor, identifier)
