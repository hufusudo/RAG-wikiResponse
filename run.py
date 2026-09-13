"""启动开发服务器：python run.py（默认 http://127.0.0.1:8000）"""
from __future__ import annotations

import uvicorn

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="127.0.0.1", port=8000)
