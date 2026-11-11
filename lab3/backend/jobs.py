"""Restart-safe deadline scans with an injectable clock."""

import threading
from .db import transaction
from .common import get_row, log
from .delivery import assignment_log, offer_task
from .orders import change_status, close_order
from .refunds import request_refund


def scan(connection, now, scale=1):
    """Each timeout rechecks current state inside one write transaction."""
    counts = {"closed": 0, "cancelled": 0, "completed": 0, "offered": 0, "expired_offers": 0, "escalated": 0}
    rows = connection.execute(
        "SELECT * FROM orders WHERE status IN ('pending_payment','pending_acceptance','delivered')"
    ).fetchall()
    for row in rows:
        order = dict(row)
        if order["status"] == "pending_payment" and now - order["created_at"] >= 300 / scale:
            close_order(connection, now, order, "支付超时", unpaid_timeout=True)
            counts["closed"] += 1
        elif order["status"] == "pending_acceptance" and now - order["paid_at"] >= 900 / scale:
            close_order(connection, now, order, "商家接单超时")
            request_refund(connection, now, order["id"], order["food_amount"], 500, "接单超时")
            counts["cancelled"] += 1
        elif order["status"] == "delivered" and now - order["delivered_at"] >= 1800 / scale:
            change_status(connection, order, "completed", now, "completed_at")
            log(connection, now, None, "自动完成", "order", order["id"])
            counts["completed"] += 1
    tasks = connection.execute("SELECT * FROM tasks WHERE status IN ('open','offered')").fetchall()
    for row in tasks:
        task = dict(row)
        order = get_row(connection, "orders", task["order_id"])
        if order["status"] != "ready":
            continue
        if task["status"] == "offered" and now - task["offered_at"] >= 60 / scale:
            assignment_log(connection, now, task["id"], task["rider_id"], "timeout")
            connection.execute(
                "UPDATE tasks SET status='open',rider_id=NULL,offered_at=NULL WHERE id=?", (task["id"],)
            )
            task["status"] = "open"
            counts["expired_offers"] += 1
        reviewed = connection.execute(
            "SELECT 1 FROM exceptions WHERE order_id=? AND reason='20分钟无人承接' AND status='continue'",
            (task["order_id"],),
        ).fetchone()
        if now - task["ready_at"] >= 1200 / scale and not task["escalated"] and not reviewed:
            connection.execute(
                "UPDATE tasks SET status='open',rider_id=NULL,offered_at=NULL,escalated=1 WHERE id=?",
                (task["id"],),
            )
            connection.execute(
                "INSERT OR IGNORE INTO exceptions(order_id,reason,created_at) VALUES(?,'20分钟无人承接',?)",
                (task["order_id"], now),
            )
            log(connection, now, None, "配送等待升级", "order", task["order_id"])
            counts["escalated"] += 1
        elif (
            task["status"] == "open"
            and not task["escalated"]
            and task["dispatch_after"] <= now
            and now - task["ready_at"] >= 480 / scale
        ):
            if offer_task(connection, now, task):
                counts["offered"] += 1
    return counts


class Scheduler:
    """One stoppable daemon per app; scans are safe if multiple workers run."""

    def __init__(self, app):
        self.app = app
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="deadline-scanner")

    def start(self):
        """Start only from the executable entry point, never during import."""
        self.thread.start()

    def stop(self):
        """Allow tests and normal process shutdown to stop the worker."""
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def run(self):
        """Log scan failures instead of silently terminating the background job."""
        while not self.stop_event.wait(1):
            try:
                with transaction(self.app.config["DATABASE"], write=True) as connection:
                    scan(connection, self.app.config["CLOCK"](), self.app.config["TIME_SCALE"])
            except Exception:  # Worker must survive a transient SQLite busy error.
                self.app.logger.exception("Deadline scan failed")
