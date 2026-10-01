"""运行配置：端口、跨域、运行环境。

环境变量由发布前检查流程统一核对（见 release-check/manifest.json）：
APP_ENV 标识运行环境，APP_PORT 覆盖监听端口（默认 8000）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _default_port() -> int:
    raw = os.getenv("APP_PORT", "8000")
    try:
        return int(raw)
    except ValueError:
        return 8000


@dataclass(frozen=True)
class Settings:
    app_name: str = "城市地下管网巡检养护平台"
    env: str = os.getenv("APP_ENV", "local")
    port: int = field(default_factory=_default_port)
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200


settings = Settings()
