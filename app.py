"""每日仪表盘 · Flask 入口"""
import os, sys
from pathlib import Path

# 确保模块可导入
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from flask import Flask
from routes.api import api, pages


def create_app():
    app = Flask(
        __name__,
        template_folder=str(BASE / "templates"),
        static_folder=str(BASE / "static"),
    )
    app.register_blueprint(api)
    app.register_blueprint(pages)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dashboard-secret-key-2026")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    return app


if __name__ == "__main__":
    app = create_app()
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5050"))
    print(f"📊 仪表盘启动: http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
