"""Measure requirement-sized queries without changing the real demonstration database."""

from concurrent.futures import ThreadPoolExecutor
import importlib.metadata
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app import create_app
from backend.db import transaction
from backend.seed import seed


def main():
    """Twenty independent authenticated clients each make ten timed queries."""
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(directory) / "bench.sqlite3"),
                "SECRET_KEY": "benchmark-only",
            }
        )
        seed(app)
        now = time.time()
        with transaction(app.config["DATABASE"], write=True) as db:
            db.executemany(
                "INSERT INTO dishes(shop_id,name,category,price,stock) VALUES(1,?,'盖饭',1800,100)",
                [(f"性能菜品{i}",) for i in range(994)],
            )
            db.executemany(
                "INSERT INTO orders(customer_id,shop_id,status,food_amount,address_snapshot,"
                "created_at,completed_at) VALUES(1,1,'completed',1800,'{}',?,?)",
                [(now - i, now - i) for i in range(10000)],
            )

        def measure(_index):
            client = app.test_client()
            csrf = client.get("/api/session").json["csrf"]
            login = client.post(
                "/api/login",
                json={"account": "customer", "password": "Demo12345!"},
                headers={"X-CSRF-Token": csrf},
            )
            assert login.status_code == 200
            results = []
            for path in ["/api/dishes?limit=50", "/api/orders?limit=50"] * 5:
                start = time.perf_counter()
                response = client.get(path)
                elapsed = time.perf_counter() - start
                results.append({"path": path, "seconds": elapsed, "status": response.status_code})
            return results

        with ThreadPoolExecutor(max_workers=20) as pool:
            groups = list(pool.map(measure, range(20)))
        results = [item for group in groups for item in group]
        summary = {}
        for path in {item["path"] for item in results}:
            selected = [r for r in results if r["path"] == path]
            duration = sorted(r["seconds"] for r in selected)
            success = sum(r["status"] == 200 and r["seconds"] <= 2 for r in selected)
            summary[path] = {
                "requests": len(selected),
                "successful_within_2s": success,
                "pass_ratio": success / len(selected),
                "p95_seconds": duration[int(len(duration) * 0.95) - 1],
                "max_seconds": max(duration),
                "median_seconds": statistics.median(duration),
            }
        report = {
            "python": sys.version,
            "platform": platform.platform(),
            "cpu": platform.processor(),
            "flask": importlib.metadata.version("flask"),
            "data": {"dishes": 1000, "orders": 10000},
            "concurrent_clients": 20,
            "method": "Flask test client in-process; includes request hooks and SQLite, excludes TCP/browser",
            "summary": summary,
            "results": results,
        }
        output = Path(__file__).resolve().parents[1] / "verification"
        output.mkdir(exist_ok=True)
        (output / "benchmark.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
