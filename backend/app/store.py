"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
发布前检查流程会把按台账回填的诉求数据通过 APP_SEED_FILE 指过来，
该文件存在时只覆盖公众诉求模块；不存在则使用内置示例数据。
"""
from __future__ import annotations

import json
import os
from typing import Any

from app.seed import SEED_ROWS

_COMPLAINT_SEED_FILE = "APP_SEED_FILE"


def _load_seed_tables() -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {
        name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
    }
    seed_file = os.getenv(_COMPLAINT_SEED_FILE)
    if seed_file:
        try:
            with open(seed_file, encoding="utf-8") as fh:
                tables["complaint"] = [dict(row) for row in json.load(fh)]
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{_COMPLAINT_SEED_FILE} 指向的诉求示例数据无法读取：{exc}") from exc
    return tables


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = _load_seed_tables()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
