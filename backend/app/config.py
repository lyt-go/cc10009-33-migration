"""运行配置：端口、跨域、运行环境与本地开发兼容开关。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 本地开发可以直接把开关写进仓库根目录的 .env；生产环境变量优先，缺省值兜底。
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
if _ENV_FILE.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_ENV_FILE, override=False)
    except ImportError:  # pragma: no cover - dotenv 是可选依赖
        pass


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = "分布式网页抓取调度平台"
    env: str = os.environ.get("APP_ENV", "local")
    port: int = int(os.environ.get("APP_PORT", "8000"))
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 分阶段同步迁移开关：本地开发默认开启，存量部署可以先用 SYNC_STAGED_ENABLED=0
    # 关掉新入口，只保留旧链路动作，等数据库迁移完成后再打开。
    staged_sync_enabled: bool = field(
        default_factory=lambda: _env_flag("SYNC_STAGED_ENABLED", True)
    )
    # 旧载荷在迁移期间是否仍允许一次性确认；关闭后旧任务只能按段续做。
    legacy_confirm_enabled: bool = field(
        default_factory=lambda: _env_flag("SYNC_LEGACY_CONFIRM_ENABLED", True)
    )


settings = Settings()
