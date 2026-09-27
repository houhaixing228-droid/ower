"""配置：路径、今天、大模型三件套，全部从环境变量读。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

#: 契约规定：系统的“今天”固定为 2026-09-01。
#: 允许用环境变量覆盖，只为测试留一个口子，默认值就是契约值。
DEFAULT_TODAY = "2026-09-01"

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

#: 开发时可以把 `.env.example` 复制成 `.env` 填自己的 Key（不入库）。
#: **环境变量优先**：评审时他们用环境变量传 DeepSeek 的 Key，这里不会覆盖它们。
DOT_ENV = PROJECT_DIR / ".env"


def load_dot_env(path: Path = DOT_ENV) -> None:
    """读 `.env` 里尚未在环境中设置的项。极简实现，不引第三方依赖。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


def _default_workspace() -> Path:
    """data/ 与 knowledge_base/ 在本项目的上一层。"""
    return PROJECT_DIR.parent


def _path_from_env(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else fallback.resolve()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    kb_dir: Path
    var_dir: Path
    today: date
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout: float
    chat_budget: float

    @property
    def source_db(self) -> Path:
        return self.data_dir / "pos.db"

    @property
    def clean_db(self) -> Path:
        return self.var_dir / "clean.db"

    @property
    def index_path(self) -> Path:
        # 索引缓存跟着仓库走，clone 下来就能直接起服务，不用等建索引。
        return PROJECT_DIR / ".cache" / "index.json"

    @property
    def live(self) -> bool:
        """契约 §7.2：没有 Key 就进入 mock 降级模式，服务照常启动。"""
        return bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    @property
    def llm_mode(self) -> str:
        return "live" if self.live else "mock"


def load_settings() -> Settings:
    load_dot_env()
    workspace = _default_workspace()
    return Settings(
        data_dir=_path_from_env("DATA_DIR", workspace / "data"),
        kb_dir=_path_from_env("KB_DIR", workspace / "knowledge_base"),
        var_dir=_path_from_env("VAR_DIR", PROJECT_DIR / "var"),
        today=date.fromisoformat(os.environ.get("TODAY", DEFAULT_TODAY)),
        # 地址原样使用：不补 /v1，不截路径（契约 §7.2）。
        llm_base_url=os.environ.get("LLM_BASE_URL", "").strip().rstrip("/"),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_model=os.environ.get("LLM_MODEL", "").strip(),
        # 契约 §7.3：单次模型调用超时不小于 120 秒。
        llm_timeout=float(os.environ.get("LLM_TIMEOUT", "120")),
        # 契约 §7.3：/api/chat 整体在 180 秒内返回，这里留出余量。
        chat_budget=float(os.environ.get("CHAT_BUDGET", "150")),
    )
