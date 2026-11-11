"""Flask application factory and permission-scoped JSON API."""

import atexit
from functools import wraps
import os
from pathlib import Path
import secrets
import sqlite3
import time
from flask import Flask, g, jsonify, request, session
from werkzeug.exceptions import HTTPException
from . import accounts, catalog, delivery, orders, refunds, reporting
from .common import BusinessError, log, require, run_once
from .db import initialize, transaction
from .jobs import Scheduler, scan


def create_app(config=None):
    """Create a testable application without starting background threads."""
    base = Path(__file__).resolve().parents[1]
    app = Flask(__name__, static_folder=str(base / "frontend"), static_url_path="/assets")
    app.config.update(
        DATABASE=str(base / "data" / "takeaway.sqlite3"),
        CLOCK=time.time,
        TIME_SCALE=1,
        TESTING=False,
        SECRET_KEY=os.environ.get("TAKEAWAY_SECRET"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=262144,
    )
    app.config.from_mapping(config or {})
    scale = float(app.config["TIME_SCALE"])
    require(1 <= scale <= 120, "演示时间缩放必须在1至120之间")
    app.config["TIME_SCALE"] = scale
    initialize(app.config["DATABASE"])
    if not app.config["SECRET_KEY"]:
        secret_file = Path(app.config["DATABASE"]).with_suffix(".secret")
        if not secret_file.exists():
            secret_file.write_text(secrets.token_hex(32), encoding="ascii")
        app.config["SECRET_KEY"] = secret_file.read_text(encoding="ascii")

    @app.before_request
    def prepare_request():
        """Resolve user from signed cookie and require CSRF token on all writes."""
        g.actor = None
        if "user_id" in session:
            with transaction(app.config["DATABASE"]) as connection:
                row = connection.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
                if row:
                    g.actor = dict(row)
        if request.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            token = session.get("csrf")
            require(
                token and secrets.compare_digest(token, request.headers.get("X-CSRF-Token", "")),
                "安全令牌无效，请刷新页面",
                403,
                "csrf",
            )

    @app.after_request
    def response_headers(response):
        """Keep API data private and disable MIME guessing."""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(BusinessError)
    def business_error(error):
        """Log denied business actions outside rolled-back business transactions."""
        if request.method != "GET":
            try:
                with transaction(app.config["DATABASE"], write=True) as connection:
                    log(
                        connection,
                        app.config["CLOCK"](),
                        g.actor["id"] if getattr(g, "actor", None) else None,
                        request.endpoint or "请求拒绝",
                        "request",
                        request.path,
                        {"code": error.code},
                        result="denied",
                    )
            except sqlite3.Error:
                app.logger.exception("Failure audit could not be stored")
        return jsonify(error=error.message, code=error.code, details=error.details), error.status

    @app.errorhandler(ValueError)
    @app.errorhandler(TypeError)
    def invalid_input(_error):
        return jsonify(error="参数格式不正确", code="invalid"), 400

    @app.errorhandler(sqlite3.IntegrityError)
    def database_conflict(_error):
        return jsonify(error="数据冲突，请刷新后重试", code="conflict"), 409

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.name, code="http_error"), error.code

    def guard(*roles):
        """The route's allowed roles supplement service-level object checks."""

        def decorate(function):
            @wraps(function)
            def wrapped(*args, **kwargs):
                require(g.actor, "请先登录", 401, "unauthenticated")
                require(not roles or g.actor["role"] in roles, "当前身份无权访问", 403, "forbidden")
                return function(*args, **kwargs)

            return wrapped

        return decorate

    def body():
        data = request.get_json(silent=True)
        require(isinstance(data, dict), "请提交JSON对象")
        return data

    def read(callback):
        with transaction(app.config["DATABASE"]) as connection:
            return jsonify(callback(connection))

    def write(operation, callback, idempotent=True):
        data = body()
        now = app.config["CLOCK"]()
        # Deadline transitions must persist even if the subsequent command is rejected.
        with transaction(app.config["DATABASE"], write=True) as connection:
            scan(connection, now, app.config["TIME_SCALE"])
        with transaction(app.config["DATABASE"], write=True) as connection:
            if idempotent:
                result = run_once(
                    connection,
                    now,
                    g.actor["id"],
                    request.headers.get("Idempotency-Key"),
                    operation,
                    data,
                    lambda: callback(connection, now, data),
                )
            else:
                result = callback(connection, now, data)
        return jsonify(result)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/session")
    def current_session():
        session.setdefault("csrf", secrets.token_hex(24))
        return jsonify(
            user=accounts.public_user(g.actor) if g.actor else None,
            csrf=session["csrf"],
            simulated=True,
            time_scale=app.config["TIME_SCALE"],
            now=app.config["CLOCK"](),
        )

    @app.post("/api/register")
    def registration():
        with transaction(app.config["DATABASE"], write=True) as connection:
            result = accounts.register(connection, app.config["CLOCK"](), body())
        return jsonify(result), 201

    @app.post("/api/sms")
    def sms():
        with transaction(app.config["DATABASE"], write=True) as connection:
            result = accounts.issue_sms(connection, app.config["CLOCK"](), body())
        return jsonify(result)

    @app.post("/api/login")
    def authentication():
        with transaction(app.config["DATABASE"], write=True) as connection:
            result = accounts.login(connection, app.config["CLOCK"](), body())
        if "error" in result:
            return jsonify(error=result["error"]), result["status"]
        session.clear()
        session["user_id"] = result["id"]
        session["csrf"] = secrets.token_hex(24)
        return jsonify(user=result, csrf=session["csrf"])

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/users")
    @guard("admin")
    def users():
        return read(
            lambda connection: [
                accounts.public_user(dict(row))
                for row in connection.execute("SELECT * FROM users ORDER BY id")
            ]
        )

    @app.patch("/api/users/<int:identifier>")
    @guard("admin")
    def user_management(identifier):
        return write(
            f"user:{identifier}",
            lambda db, now, data: accounts.manage_user(db, now, g.actor, identifier, data),
        )

    @app.post("/api/rider/online")
    @guard("rider")
    def rider_online():
        return write("online", lambda db, now, data: accounts.set_online(db, now, g.actor, data))

    @app.get("/api/shops")
    @guard()
    def shops():
        def query(db):
            if g.actor["role"] == "merchant":
                rows = db.execute("SELECT * FROM shops WHERE merchant_id=?", (g.actor["id"],))
            else:
                rows = db.execute("SELECT * FROM shops ORDER BY id")
            return [dict(row) for row in rows]

        return read(query)

    @app.patch("/api/shops/<int:identifier>")
    @guard("merchant", "admin")
    def shop_management(identifier):
        return write(
            f"shop:{identifier}",
            lambda db, now, data: catalog.update_shop(db, now, g.actor, identifier, data),
        )

    @app.get("/api/dishes")
    @guard()
    def dish_list():
        return read(lambda db: catalog.dishes(db, g.actor, request.args))

    @app.post("/api/dishes")
    @guard("merchant")
    def dish_creation():
        return write("create_dish", lambda db, now, data: catalog.save_dish(db, now, g.actor, data))

    @app.put("/api/dishes/<int:identifier>")
    @guard("merchant")
    def dish_update(identifier):
        return write(
            f"dish:{identifier}", lambda db, now, data: catalog.save_dish(db, now, g.actor, data, identifier)
        )

    @app.delete("/api/dishes/<int:identifier>")
    @guard("merchant")
    def dish_deletion(identifier):
        return write(
            f"delete_dish:{identifier}",
            lambda db, now, data: catalog.delete_dish(db, now, g.actor, identifier),
        )

    @app.get("/api/addresses")
    @guard("customer")
    def addresses():
        return read(
            lambda db: [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM addresses WHERE customer_id=? AND deleted=0", (g.actor["id"],)
                )
            ]
        )

    @app.post("/api/addresses")
    @guard("customer")
    def address_creation():
        return write("create_address", lambda db, now, data: catalog.save_address(db, now, g.actor, data))

    @app.put("/api/addresses/<int:identifier>")
    @guard("customer")
    def address_update(identifier):
        return write(
            f"address:{identifier}",
            lambda db, now, data: catalog.save_address(db, now, g.actor, data, identifier),
        )

    @app.get("/api/cart")
    @guard("customer")
    def cart():
        return read(lambda db: catalog.cart_rows(db, g.actor))

    @app.put("/api/cart")
    @guard("customer")
    def cart_update():
        return write("cart", lambda db, now, data: catalog.update_cart(db, now, g.actor, data))

    @app.post("/api/orders")
    @guard("customer")
    def order_creation():
        return write("create_order", lambda db, now, data: orders.create_order(db, now, g.actor, data))

    @app.get("/api/orders")
    @guard()
    def order_list():
        return read(lambda db: orders.list_orders(db, g.actor, request.args))

    @app.get("/api/orders/<int:identifier>")
    @guard()
    def order_details(identifier):
        return read(lambda db: orders.order_detail(db, g.actor, identifier))

    @app.post("/api/orders/<int:identifier>/actions")
    @guard("customer", "merchant")
    def order_action(identifier):
        return write(
            f"order_action:{identifier}",
            lambda db, now, data: orders.act_on_order(db, now, g.actor, identifier, data),
        )

    @app.get("/api/orders/<int:identifier>/position")
    @guard("customer", "rider", "admin")
    def order_position(identifier):
        return read(lambda db: delivery.position(db, app.config["CLOCK"](), g.actor, identifier))

    @app.get("/api/tasks")
    @guard("rider", "admin")
    def tasks():
        return read(lambda db: delivery.list_tasks(db, g.actor))

    @app.post("/api/tasks/<int:identifier>/actions")
    @guard("rider")
    def task_action(identifier):
        return write(
            f"task:{identifier}",
            lambda db, now, data: delivery.act_on_task(db, now, g.actor, identifier, data),
        )

    @app.get("/api/exceptions")
    @guard("admin")
    def exceptions():
        return read(
            lambda db: [
                dict(row) for row in db.execute("SELECT * FROM exceptions ORDER BY id DESC LIMIT 500")
            ]
        )

    @app.post("/api/orders/<int:identifier>/exceptions")
    @guard("customer", "rider")
    def exception_creation(identifier):
        return write(
            f"exception:{identifier}",
            lambda db, now, data: refunds.report_exception(db, now, g.actor, identifier, data),
        )

    @app.post("/api/exceptions/<int:identifier>/resolve")
    @guard("admin")
    def exception_resolution(identifier):
        return write(
            f"resolve:{identifier}",
            lambda db, now, data: refunds.resolve_exception(db, now, g.actor, identifier, data),
        )

    @app.get("/api/refunds")
    @guard("admin")
    def refund_list():
        return read(
            lambda db: [dict(row) for row in db.execute("SELECT * FROM refunds ORDER BY id DESC LIMIT 500")]
        )

    @app.post("/api/refunds/<int:identifier>/process")
    @guard("admin")
    def refund_process(identifier):
        return write(
            f"refund:{identifier}",
            lambda db, now, data: refunds.process_refund(db, now, g.actor, identifier, data),
        )

    @app.post("/api/orders/<int:identifier>/refunds")
    @guard("admin")
    def refund_creation(identifier):
        def create(db, now, data):
            from .common import money, text

            reason = text(data.get("reason"), "退款原因", 500)
            result = refunds.request_refund(
                db,
                now,
                identifier,
                money(data.get("food_amount", 0)),
                money(data.get("delivery_amount", 0)),
                reason,
            )
            log(db, now, g.actor["id"], "申请退款", "order", identifier, {"refund_id": result["id"]})
            return result

        return write(f"refund_create:{identifier}", create)

    @app.get("/api/statistics")
    @guard("merchant", "admin")
    def statistics():
        return read(lambda db: reporting.statistics(db, g.actor, request.args))

    @app.get("/api/logs")
    @guard("admin")
    def logs():
        return read(lambda db: reporting.audit_query(db, request.args))

    @app.post("/api/jobs/scan")
    @guard("admin")
    def manual_scan():
        return write("scan", lambda db, now, data: scan(db, now, app.config["TIME_SCALE"]))

    @app.get("/api/health")
    def health():
        return jsonify(
            status="ok",
            simulated_services=["payment", "sms", "location"],
            time_scale=app.config["TIME_SCALE"],
        )

    return app


def start_scheduler(app):
    """Start persistent-deadline checks when serving from the CLI."""
    scheduler = Scheduler(app)
    scheduler.start()
    atexit.register(scheduler.stop)
    return scheduler
