"""Launch the local web application using server-side .env settings."""

from __future__ import annotations

import argparse
import importlib.util
import sys

from pydantic import ValidationError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 simAgent 网页问答")
    parser.add_argument("--env-file", default=".env", help="配置文件路径，默认当前目录的 .env")
    args = parser.parse_args(argv)
    if any(importlib.util.find_spec(name) is None
           for name in ("fastapi", "uvicorn", "dotenv", "openai")):
        print("请使用 uv run --extra web simagent 启动，以安装网页问答依赖。", file=sys.stderr)
        return 1

    from .config import WebSettings

    try:
        settings = WebSettings.from_env(args.env_file)
    except ValidationError as error:
        fields = ", ".join(".".join(str(part) for part in item["loc"])
                           for item in error.errors(include_input=False))
        print(f"配置格式有误，请修改 {args.env_file} 中的对应配置：{fields}", file=sys.stderr)
        return 1
    except OSError:
        print(f"无法读取配置文件：{args.env_file}", file=sys.stderr)
        return 1

    import uvicorn

    from .web import create_app

    host = f"[{settings.host}]" if ":" in settings.host else settings.host
    print(f"simAgent 网页问答：http://{host}:{settings.port}", flush=True)
    print("修改 .env 后请重启服务；在终端按 Ctrl+C 停止。", flush=True)
    if not settings.configured:
        print("请在 .env 中填写 DEEPSEEK_API_KEY；网页会显示配置提示。", flush=True)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                access_log=False, timeout_graceful_shutdown=5)
    return 0
