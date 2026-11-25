"""运行期配置。数据库路径可由环境变量 SCENE_DB_PATH 覆盖（测试用）。"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "scenes.db"


def get_db_path() -> Path:
    return Path(os.environ.get("SCENE_DB_PATH", str(DEFAULT_DB_PATH)))
