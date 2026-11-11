"""Shared validation, money, audit, and idempotency utilities."""

import hashlib
import json
from decimal import Decimal, InvalidOperation


class BusinessError(Exception):
    """An expected, client-visible business rejection."""

    def __init__(self, message, status=400, code="invalid", details=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details


def require(condition, message, status=400, code="invalid", details=None):
    """Reject invalid requests before modifying related records."""
    if not condition:
        raise BusinessError(message, status, code, details)


def integer(value, name, minimum=0, maximum=1000000):
    """Accept JSON integers only, excluding booleans and fractions."""
    require(isinstance(value, int) and not isinstance(value, bool), f"{name}必须是整数")
    require(minimum <= value <= maximum, f"{name}超出允许范围")
    return value


def money(value, positive=False):
    """Convert decimal yuan to integer cents without float arithmetic."""
    require(not isinstance(value, bool), "金额格式错误")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BusinessError("金额格式错误") from None
    require(amount.is_finite(), "金额必须是有限数值")
    require(amount >= 0 and amount <= Decimal("1000000"), "金额超出范围")
    require(amount == amount.quantize(Decimal("0.01")), "金额最多两位小数")
    require(not positive or amount > 0, "价格必须大于零")
    return int(amount * 100)


def text(value, name, maximum=200, minimum=1):
    """Validate a bounded, nonempty string."""
    require(isinstance(value, str), f"{name}必须是文字")
    result = value.strip()
    require(minimum <= len(result) <= maximum, f"{name}长度不合法")
    return result


def get_row(connection, table, identifier):
    """Read by a trusted table name, with an explicit not-found response."""
    allowed = {"users", "shops", "dishes", "addresses", "orders", "tasks", "refunds", "exceptions"}
    require(table in allowed, "不支持的数据对象")
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (identifier,)).fetchone()
    require(row is not None, "记录不存在", 404, "not_found")
    return dict(row)


def log(connection, now, actor, action, kind, identifier, changes=None, result="success"):
    """Append audit data; never accept passwords or full login payloads."""
    connection.execute(
        "INSERT INTO audit_logs(actor_id,action,object_type,object_id,"
        "result,changes,created_at) VALUES(?,?,?,?,?,?,?)",
        (actor, action, kind, str(identifier), result, json.dumps(changes or {}, ensure_ascii=False), now),
    )


def run_once(connection, now, actor, key, operation, payload, callback):
    """Store request result in the same transaction as business writes."""
    key = text(key, "请求键", 128, 8)
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = connection.execute(
        "SELECT * FROM requests WHERE actor_id=? AND request_key=?",
        (actor, key),
    ).fetchone()
    if previous:
        require(
            previous["operation"] == operation and previous["fingerprint"] == fingerprint,
            "同一请求键不能用于不同操作或参数",
            409,
            "key_conflict",
        )
        return json.loads(previous["response"])
    result = callback()
    connection.execute(
        "INSERT INTO requests VALUES(?,?,?,?,?,?)",
        (actor, key, operation, fingerprint, json.dumps(result, ensure_ascii=False), now),
    )
    return result


def order_access(connection, actor, order):
    """Enforce ownership even when clients guess an identifier."""
    role = actor["role"]
    if role == "admin":
        return
    if role == "customer":
        allowed = order["customer_id"] == actor["id"]
    elif role == "merchant":
        shop = get_row(connection, "shops", order["shop_id"])
        allowed = shop["merchant_id"] == actor["id"]
    else:
        allowed = (
            connection.execute(
                "SELECT 1 FROM tasks WHERE order_id=? AND rider_id=?",
                (order["id"], actor["id"]),
            ).fetchone()
            is not None
        )
    require(allowed, "无权访问该订单", 403, "forbidden")


def active_business(actor):
    """A disabled account retains historical fulfillment, not new business."""
    require(
        actor["enabled"] and actor["approval"] == "approved",
        "账号未审核通过或已停用，不能开展新业务",
        403,
        "restricted",
    )
