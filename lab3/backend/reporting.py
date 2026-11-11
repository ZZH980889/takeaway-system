"""Permission-aware reporting and immutable audit queries."""

from datetime import datetime, timezone
from .common import require


def statistics(connection, actor, query):
    """Refunds are deducted from the original completion date, not refund date."""
    period = query.get("period", "day")
    require(period in {"day", "month"}, "统计周期仅支持日或月")
    format_string = "%Y-%m-%d" if period == "day" else "%Y-%m"
    # All grouping is explicitly in Shanghai time, regardless of host timezone.
    sql = """
        SELECT o.id,o.completed_at,o.food_amount,o.delivery_amount,
          COALESCE((SELECT SUM(r.food_amount) FROM refunds r
            WHERE r.order_id=o.id AND r.status='refunded'),0) food_refund,
          COALESCE((SELECT SUM(r.delivery_amount) FROM refunds r
            WHERE r.order_id=o.id AND r.status='refunded'),0) delivery_refund
        FROM orders o JOIN shops s ON s.id=o.shop_id
        WHERE o.status='completed'
    """
    args = []
    if actor["role"] == "merchant":
        sql += " AND s.merchant_id=?"
        args.append(actor["id"])
    if query.get("from"):
        sql += " AND o.completed_at>=?"
        args.append(float(query["from"]))
    if query.get("to"):
        sql += " AND o.completed_at<=?"
        args.append(float(query["to"]))
    groups = {}
    for row in connection.execute(sql, args):
        timestamp = datetime.fromtimestamp(row["completed_at"] + 8 * 3600, timezone.utc)
        key = timestamp.strftime(format_string)
        bucket = groups.setdefault(
            key, {"period": key, "completed_orders": 0, "food_net": 0, "delivery_net": 0}
        )
        bucket["completed_orders"] += 1
        bucket["food_net"] += row["food_amount"] - row["food_refund"]
        bucket["delivery_net"] += row["delivery_amount"] - row["delivery_refund"]
    return {
        "period_type": period,
        "timezone": "Asia/Shanghai",
        "items": [groups[key] for key in sorted(groups)],
    }


def audit_query(connection, query):
    """Read-only filters; intentionally no update/delete service exists."""
    clauses = ["1=1"]
    args = []
    for key, column in [("actor_id", "a.actor_id"), ("action", "a.action"), ("result", "a.result")]:
        if query.get(key):
            clauses.append(f"{column}=?")
            args.append(query[key])
    if query.get("order_id"):
        clauses.append("a.object_type='order' AND a.object_id=?")
        args.append(query["order_id"])
    for key, operator in [("from", ">="), ("to", "<=")]:
        if query.get(key):
            clauses.append(f"a.created_at{operator}?")
            args.append(float(query[key]))
    limit = min(max(int(query.get("limit", 100)), 1), 500)
    rows = connection.execute(
        "SELECT a.*,u.account actor FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_id "
        "WHERE " + " AND ".join(clauses) + " ORDER BY a.id DESC LIMIT ?",
        (*args, limit),
    )
    return [dict(row) for row in rows]
