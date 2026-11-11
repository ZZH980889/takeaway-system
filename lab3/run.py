"""Run the local classroom application: python run.py --seed."""

import argparse
import os
from backend.app import create_app, start_scheduler
from backend.seed import seed


def main():
    """Keep debug reloader off so only one deadline worker is started."""
    parser = argparse.ArgumentParser(description="食间外卖系统")
    parser.add_argument("--seed", action="store_true", help="首次运行创建演示数据")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--time-scale", type=float, default=1, help="演示时间缩放，默认1为真实计时，最高120")
    args = parser.parse_args()
    config = {"TIME_SCALE": args.time_scale}
    if os.environ.get("TAKEAWAY_DATABASE"):
        config["DATABASE"] = os.environ["TAKEAWAY_DATABASE"]
    app = create_app(config)
    if args.seed:
        seed(app)
    start_scheduler(app)
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
