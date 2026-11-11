"""Explicit demo seed; does not reset existing application data."""

from werkzeug.security import generate_password_hash
from .db import transaction


def seed(app):
    """Return predictable classroom accounts with one-way password hashes."""
    with transaction(app.config["DATABASE"], write=True) as connection:
        if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return False
        now = app.config["CLOCK"]()
        for index, (account, role, name) in enumerate(
            [
                ("customer", "customer", "张同学"),
                ("merchant", "merchant", "食间小馆"),
                ("rider", "rider", "李骑手"),
                ("admin", "admin", "平台管理员"),
                ("customer2", "customer", "王同学"),
                ("rider2", "rider", "王骑手"),
            ],
            1,
        ):
            connection.execute(
                "INSERT INTO users(account,phone,password_hash,role,name,approval,online,"
                "created_at) VALUES(?,?,?,?,?,'approved',?,?)",
                (
                    account,
                    f"1380000000{index}",
                    generate_password_hash("Demo12345!"),
                    role,
                    name,
                    int(role == "rider"),
                    now,
                ),
            )
        connection.execute("INSERT INTO shops(merchant_id,name) VALUES(2,'食间小馆')")
        for name, category, price, stock in [
            ("宫保鸡丁饭", "盖饭", 1800, 12),
            ("照烧鸡腿饭", "盖饭", 2200, 8),
            ("番茄鸡蛋面", "面食", 1600, 10),
            ("鲜蔬沙拉", "轻食", 2000, 9),
            ("冬瓜排骨汤", "汤品", 1200, 0),
            ("冰柠檬茶", "饮品", 600, 20),
        ]:
            connection.execute(
                "INSERT INTO dishes(shop_id,name,category,price,stock) VALUES(1,?,?,?,?)",
                (name, category, price, stock),
            )
        for user_id in (1, 5):
            connection.execute(
                "INSERT INTO addresses(customer_id,region,detail,contact,phone) "
                "VALUES(?,'仙林校区','学生宿舍1栋','演示联系人','13800000001')",
                (user_id,),
            )
    return True
