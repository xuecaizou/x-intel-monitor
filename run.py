"""启动入口：python run.py  →  http://127.0.0.1:8083"""
import uvicorn

from app.config import load

if __name__ == "__main__":
    cfg = load()
    host, port = cfg["server"]["host"], int(cfg["server"]["port"])
    print(f"X-Intel · X 情报监控台  →  http://{host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port, log_level="warning")
