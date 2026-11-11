"""Independent refunds with reserved balances and safe retries."""

from .common import get_row, log, money, order_access, require, text
from .orders import close_order


def request_refund(connection, now, identifier, food, delivery, reason):
    """Reserve balances even for failed refunds to prevent parallel over-refunding."""
    payment = connection.execute("SELECT * FROM payments WHERE order_id=?", (identifier,)).fetchone()
    require(payment is not None, "订单尚未付款，不可退款", 409)
    order = get_row(connection, "orders", identifier)
    reserved = connection.execute(
        "SELECT COALESCE(SUM(food_amount),0),COALESCE(SUM(delivery_amount),0) FROM refunds WHERE order_id=?",
        (identifier,),
    ).fetchone()
    require(food >= 0 and delivery >= 0 and food + delivery > 0, "退款总额必须大于零")
    require(
        food + reserved[0] <= order["food_amount"] and delivery + reserved[1] <= 500,
        "退款超过对应可退余额（含处理中和失败待重试申请）",
        409,
    )
    refund_id = connection.execute(
        "INSERT INTO refunds(order_id,payment_id,food_amount,delivery_amount,status,"
        "reason,created_at,updated_at) VALUES(?,?,?,?,'processing',?,?,?)",
        (identifier, payment["id"], food, delivery, reason, now, now),
    ).lastrowid
    return get_row(connection, "refunds", refund_id)


def process_refund(connection, now, actor, identifier, data):
    """Explicit simulator succeeds/fails independently from order state."""
    refund = get_row(connection, "refunds", identifier)
    require(refund["status"] in {"processing", "failed"}, "退款已成功，不可重复执行", 409)
    outcome = data.get("outcome")
    require(outcome in {"success", "failure"}, "请选择模拟退款成功或失败")
    status = "refunded" if outcome == "success" else "failed"
    connection.execute(
        "UPDATE refunds SET status=?,attempts=attempts+1,updated_at=? WHERE id=?", (status, now, identifier)
    )
    log(
        connection,
        now,
        actor["id"],
        "处理或重试退款",
        "order",
        refund["order_id"],
        {"refund_id": identifier, "status": status},
    )
    return get_row(connection, "refunds", identifier)


def report_exception(connection, now, actor, identifier, data):
    """Customers request assistance; riders must own the delivering task."""
    order = get_row(connection, "orders", identifier)
    order_access(connection, actor, order)
    require(order["status"] in {"preparing", "ready", "delivering"}, "该订单不在异常处理阶段", 409)
    reason = text(data.get("reason"), "异常原因", 500)
    old = connection.execute(
        "SELECT id FROM exceptions WHERE order_id=? AND status='pending'", (identifier,)
    ).fetchone()
    require(not old, "该订单已有待处理异常", 409)
    exception_id = connection.execute(
        "INSERT INTO exceptions(order_id,reporter_id,reason,created_at) VALUES(?,?,?,?)",
        (identifier, actor["id"], reason, now),
    ).lastrowid
    log(connection, now, actor["id"], "上报异常", "order", identifier, {"reason": reason})
    return get_row(connection, "exceptions", exception_id)


def resolve_exception(connection, now, actor, identifier, data):
    """Resolve once and close fulfillment before creating an independent refund."""
    exception = get_row(connection, "exceptions", identifier)
    require(exception["status"] == "pending", "异常已处理", 409)
    reason = text(data.get("reason"), "处理说明", 500)
    decision = data.get("decision")
    require(decision in {"continue", "terminate"}, "处理决定非法")
    order = get_row(connection, "orders", exception["order_id"])
    refund = None
    if decision == "terminate":
        food = money(data.get("food_amount", 0))
        delivery = money(data.get("delivery_amount", 0))
        close_order(connection, now, order, reason, actor["id"])
        if food + delivery:
            refund = request_refund(connection, now, order["id"], food, delivery, reason)
    else:
        require(order["status"] not in {"cancelled", "closed", "completed"}, "终止订单不能继续配送", 409)
        # A prior dispatch escalation may be returned to automatic allocation.
        connection.execute(
            "UPDATE tasks SET escalated=0,dispatch_after=? WHERE order_id=?", (now + 60, order["id"])
        )
    connection.execute(
        "UPDATE exceptions SET status=?,resolution=?,resolved_at=? WHERE id=?",
        (decision, reason, now, identifier),
    )
    log(
        connection,
        now,
        actor["id"],
        "平台处理异常",
        "order",
        order["id"],
        {"decision": decision, "reason": reason},
    )
    return {"exception": get_row(connection, "exceptions", identifier), "refund": refund}
