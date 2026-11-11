"""Accounts, simulated SMS, password verification, and platform approval."""

import re
import secrets
from werkzeug.security import check_password_hash, generate_password_hash
from .common import active_business, get_row, log, require, text


def public_user(row):
    """Expose profile without credentials."""
    return {
        key: row[key] for key in ("id", "account", "phone", "role", "name", "approval", "enabled", "online")
    }


def validate_phone(value):
    """Use the classroom's mainland mobile format."""
    require(isinstance(value, str) and re.fullmatch(r"1\d{10}", value), "手机号必须为11位数字")
    return value


def register(connection, now, data):
    """Register non-admin accounts with pending business approval."""
    account = text(data.get("account"), "账号", 30, 3)
    require(re.fullmatch(r"[A-Za-z0-9_]+", account), "账号仅含字母数字下划线")
    phone = validate_phone(data.get("phone"))
    password = text(data.get("password"), "密码", 128, 8)
    role = data.get("role")
    require(role in {"customer", "merchant", "rider"}, "不能注册该身份")
    require(
        not connection.execute("SELECT 1 FROM users WHERE account=? OR phone=?", (account, phone)).fetchone(),
        "账号或手机号已注册",
        409,
        "duplicate_account",
    )
    name = text(data.get("name", account), "名称", 60)
    approval = "approved" if role == "customer" else "pending"
    cursor = connection.execute(
        "INSERT INTO users(account,phone,password_hash,role,name,approval,created_at) VALUES(?,?,?,?,?,?,?)",
        (account, phone, generate_password_hash(password), role, name, approval, now),
    )
    identifier = cursor.lastrowid
    if role == "merchant":
        connection.execute(
            "INSERT INTO shops(merchant_id,name,is_open) VALUES(?,?,0)",
            (identifier, name),
        )
    log(connection, now, identifier, "注册", "user", identifier)
    return public_user(get_row(connection, "users", identifier))


def issue_sms(connection, now, data):
    """Persist an expiring classroom OTP; no external SMS is sent."""
    phone = validate_phone(data.get("phone"))
    old = connection.execute("SELECT * FROM sms_codes WHERE phone=?", (phone,)).fetchone()
    require(not old or now - old["sent_at"] >= 30, "发送过于频繁，请30秒后再试", 429, "rate_limit")
    code = f"{secrets.randbelow(1000000):06d}"
    connection.execute(
        "INSERT INTO sms_codes VALUES(?,?,?,?,0) ON CONFLICT(phone) DO UPDATE SET "
        "code_hash=excluded.code_hash,expires_at=excluded.expires_at,"
        "sent_at=excluded.sent_at,attempts=0",
        (phone, generate_password_hash(code), now + 300, now),
    )
    return {"simulated": True, "demo_code": code, "expires_in": 300}


def login(connection, now, data):
    """Return a profile after credential verification; commit failed OTP attempts."""
    mode = data.get("mode", "password")
    if mode == "password":
        row = connection.execute("SELECT * FROM users WHERE account=?", (data.get("account"),)).fetchone()
        valid = bool(
            row
            and isinstance(data.get("password"), str)
            and check_password_hash(row["password_hash"], data["password"])
        )
    else:
        require(mode == "phone", "未知登录方式")
        phone = validate_phone(data.get("phone"))
        row = connection.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        code = connection.execute("SELECT * FROM sms_codes WHERE phone=?", (phone,)).fetchone()
        valid = bool(
            code
            and code["expires_at"] > now
            and code["attempts"] < 5
            and isinstance(data.get("code"), str)
            and check_password_hash(code["code_hash"], data["code"])
        )
        if code:
            connection.execute("UPDATE sms_codes SET attempts=attempts+1 WHERE phone=?", (phone,))
        if valid and row:
            connection.execute("DELETE FROM sms_codes WHERE phone=?", (phone,))
    if not valid or not row:
        log(connection, now, None, "登录", "session", "unknown", result="denied")
        return {"error": "账号、密码或验证码不正确", "status": 401}
    if (row["role"] == "admin") != bool(data.get("admin", False)):
        log(connection, now, row["id"], "登录入口不符", "session", row["id"], result="denied")
        return {"error": "请使用与账号身份匹配的登录入口", "status": 403}
    log(connection, now, row["id"], "登录", "user", row["id"])
    return public_user(dict(row))


def manage_user(connection, now, actor, identifier, data):
    """Administrators approve, reject, or disable accounts without erasing history."""
    target = get_row(connection, "users", identifier)
    require(target["role"] != "admin", "不通过该入口修改管理员")
    if "approval" in data:
        require(target["role"] in {"merchant", "rider"}, "该用户不需要入驻审核")
        approval = data["approval"]
        require(approval in {"approved", "rejected"}, "审核结果不合法")
        connection.execute("UPDATE users SET approval=? WHERE id=?", (approval, identifier))
    if "enabled" in data:
        require(isinstance(data["enabled"], bool), "启用状态必须为布尔值")
        connection.execute("UPDATE users SET enabled=? WHERE id=?", (int(data["enabled"]), identifier))
    log(
        connection,
        now,
        actor["id"],
        "用户审核或停用",
        "user",
        identifier,
        {key: data[key] for key in ("approval", "enabled") if key in data},
    )
    return public_user(get_row(connection, "users", identifier))


def set_online(connection, now, actor, data):
    """Leaving online status does not abandon already accepted tasks."""
    value = data.get("online")
    require(isinstance(value, bool), "在线状态必须为布尔值")
    if value:
        active_business(actor)
    connection.execute("UPDATE users SET online=? WHERE id=?", (int(value), actor["id"]))
    log(connection, now, actor["id"], "切换在线状态", "user", actor["id"], {"online": value})
    return public_user(get_row(connection, "users", actor["id"]))
