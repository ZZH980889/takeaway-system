"""Shop, dish, address, and persistent shopping-cart services."""

from .accounts import validate_phone
from .common import active_business, get_row, integer, log, money, require, text


def owned_shop(connection, actor, identifier=None):
    """A merchant may only change their one shop."""
    if identifier is None:
        row = connection.execute("SELECT * FROM shops WHERE merchant_id=?", (actor["id"],)).fetchone()
        require(row is not None, "店铺不存在", 404)
        return dict(row)
    shop = get_row(connection, "shops", identifier)
    require(actor["role"] == "admin" or shop["merchant_id"] == actor["id"], "无权操作该店铺", 403)
    return shop


def update_shop(connection, now, actor, identifier, data):
    """Changing availability never cancels historical orders."""
    owned_shop(connection, actor, identifier)
    allowed = {"name", "description", "region", "is_open"}
    if actor["role"] == "admin":
        allowed.add("enabled")
    require(data and set(data) <= allowed, "店铺字段不合法")
    changes = {}
    for key, value in data.items():
        if key in {"is_open", "enabled"}:
            require(isinstance(value, bool), "营业和启用状态必须为布尔值")
            if key == "is_open" and value and actor["role"] != "admin":
                active_business(actor)
            changes[key] = int(value)
        else:
            changes[key] = text(value, key, 300, 0 if key == "description" else 1)
    assignments = ",".join(f"{key}=?" for key in changes)
    connection.execute(f"UPDATE shops SET {assignments} WHERE id=?", (*changes.values(), identifier))
    log(connection, now, actor["id"], "修改店铺", "shop", identifier, changes)
    return get_row(connection, "shops", identifier)


def save_dish(connection, now, actor, data, identifier=None):
    """Validate money and integer stock before one versioned update."""
    shop = owned_shop(connection, actor)
    active_business(actor)
    old = get_row(connection, "dishes", identifier) if identifier else None
    require(not old or old["shop_id"] == shop["id"], "无权修改他店菜品", 403)
    name = text(data.get("name"), "菜品名称", 80)
    category = text(data.get("category"), "菜品分类", 40)
    price = money(data.get("price"), positive=True)
    stock = integer(data.get("stock"), "库存")
    on_sale = data.get("on_sale", True)
    require(isinstance(on_sale, bool), "上架状态必须为布尔值")
    if old:
        require(not old["deleted"], "已删除菜品不可修改", 409)
        version = integer(data.get("version"), "版本", 1)
        require(version == old["version"], "菜品已变化，请刷新", 409, "stale_version")
        connection.execute(
            "UPDATE dishes SET name=?,category=?,price=?,stock=?,on_sale=?,version=version+1 WHERE id=?",
            (name, category, price, stock, int(on_sale), identifier),
        )
    else:
        identifier = connection.execute(
            "INSERT INTO dishes(shop_id,name,category,price,stock,on_sale) VALUES(?,?,?,?,?,?)",
            (shop["id"], name, category, price, stock, int(on_sale)),
        ).lastrowid
    log(
        connection,
        now,
        actor["id"],
        "保存菜品",
        "dish",
        identifier,
        {"name": name, "price": price, "stock": stock},
    )
    return get_row(connection, "dishes", identifier)


def delete_dish(connection, now, actor, identifier):
    """Soft deletion preserves historical foreign keys and snapshots."""
    dish = get_row(connection, "dishes", identifier)
    owned_shop(connection, actor, dish["shop_id"])
    connection.execute("UPDATE dishes SET deleted=1,on_sale=0,version=version+1 WHERE id=?", (identifier,))
    log(connection, now, actor["id"], "删除菜品", "dish", identifier)
    return {"id": identifier, "deleted": True}


def dishes(connection, actor, query):
    """Combine filters and hide unpublished dishes from customers."""
    clauses = ["d.deleted=0"]
    values = []
    if actor["role"] == "merchant":
        clauses.append("s.merchant_id=?")
        values.append(actor["id"])
    elif actor["role"] != "admin":
        clauses.extend(["d.on_sale=1", "s.enabled=1", "u.enabled=1", "u.approval='approved'"])
    for key, field in [("shop_id", "d.shop_id"), ("category", "d.category")]:
        if query.get(key):
            clauses.append(f"{field}=?")
            values.append(query[key])
    if query.get("keyword"):
        clauses.append("d.name LIKE ?")
        values.append("%" + query["keyword"] + "%")
    for key, operator in [("min_price", ">="), ("max_price", "<=")]:
        if query.get(key) is not None:
            clauses.append(f"d.price {operator} ?")
            values.append(money(query[key]))
    if query.get("in_stock") == "true":
        clauses.append("d.stock>0")
    limit = min(max(int(query.get("limit", 100)), 1), 500)
    sql = (
        "SELECT d.*,s.name shop_name,s.is_open FROM dishes d "
        "JOIN shops s ON s.id=d.shop_id JOIN users u ON u.id=s.merchant_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY d.id DESC LIMIT ?"
    )
    return [dict(row) for row in connection.execute(sql, (*values, limit))]


def save_address(connection, now, actor, data, identifier=None):
    """Addresses belong to their customer; orders keep independent snapshots."""
    region = text(data.get("region"), "区域", 80)
    detail = text(data.get("detail"), "详细地址", 200, 4)
    contact = text(data.get("contact"), "联系人", 40)
    phone = validate_phone(data.get("phone"))
    if identifier:
        old = get_row(connection, "addresses", identifier)
        require(old["customer_id"] == actor["id"] and not old["deleted"], "无权修改地址", 403)
        connection.execute(
            "UPDATE addresses SET region=?,detail=?,contact=?,phone=? WHERE id=?",
            (region, detail, contact, phone, identifier),
        )
    else:
        identifier = connection.execute(
            "INSERT INTO addresses(customer_id,region,detail,contact,phone) VALUES(?,?,?,?,?)",
            (actor["id"], region, detail, contact, phone),
        ).lastrowid
    log(connection, now, actor["id"], "保存地址", "address", identifier)
    return get_row(connection, "addresses", identifier)


def cart_rows(connection, actor):
    """Return both seen and latest prices so checkout can require confirmation."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT c.*,d.name,d.price,d.stock,d.on_sale,d.deleted,d.shop_id,s.name shop_name "
            "FROM cart c JOIN dishes d ON c.dish_id=d.id JOIN shops s ON s.id=d.shop_id "
            "WHERE c.customer_id=? ORDER BY d.shop_id,d.id",
            (actor["id"],),
        )
    ]


def update_cart(connection, now, actor, data):
    """Zero quantity removes an item; negative and fractional input is rejected."""
    active_business(actor)
    identifier = integer(data.get("dish_id"), "菜品编号", 1)
    quantity = integer(data.get("quantity"), "购买数量", 0, 999)
    dish = get_row(connection, "dishes", identifier)
    if quantity == 0:
        connection.execute("DELETE FROM cart WHERE customer_id=? AND dish_id=?", (actor["id"], identifier))
    else:
        require(not dish["deleted"] and dish["on_sale"], "菜品已下架", 409)
        require(quantity <= dish["stock"], "库存不足", 409)
        connection.execute(
            "INSERT INTO cart VALUES(?,?,?,?) ON CONFLICT(customer_id,dish_id) "
            "DO UPDATE SET quantity=excluded.quantity",
            (actor["id"], identifier, quantity, dish["price"]),
        )
    log(connection, now, actor["id"], "修改购物车", "dish", identifier, {"quantity": quantity})
    return cart_rows(connection, actor)
