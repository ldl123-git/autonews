"""启动脚本：python run.py

环境变量：
    HOST  监听地址，默认 0.0.0.0（WSL 下需监听 0.0.0.0 才能从 Windows 宿主访问）
    PORT  监听端口，默认 8000
"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        log_level="info",
    )
