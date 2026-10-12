"""Token 绑定与订阅的持久化存储。

沿用 AstrBot 插件的通行做法（参考 astrbot_plugin_arknights 的 core/store.py）：
凭据写进 AstrBot 插件数据目录下的单个 JSON 文件，权限 ``0600``，每次操作
读改写一遍，文件很小，不持有长期内存副本，也就不存在并发写丢更新。

本模块刻意不在导入期引用 ``astrbot`` 之外的东西，便于脱框架自测。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

PLUGIN_DIR_NAME = "astrbot_plugin_kuro_sign"


def _default_path() -> Path:
    """解析 AstrBot 插件数据目录下的存储文件路径。

    Returns:
        绑定文件绝对路径。
    """
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

    return Path(get_astrbot_plugin_data_path()) / PLUGIN_DIR_NAME / "users.json"


def _empty() -> dict[str, Any]:
    """返回一份全新的空存储结构。"""
    return {"users": {}, "subs": {"sign_groups": []}}


def today() -> str:
    """返回本地日期字符串，用于判断今天是否已签到。"""
    return time.strftime("%Y-%m-%d")


class Store:
    """绑定与订阅存储。"""

    def __init__(self, path: Path | None = None) -> None:
        """初始化存储。

        Args:
            path: 显式存储路径；为 ``None`` 时延迟到首次使用时解析，
                这样单测可以完全不依赖框架。
        """
        self.path: Path | None = Path(path) if path is not None else None
        self._lock = asyncio.Lock()

    def _resolve_path(self) -> Path:
        """返回存储路径，必要时解析框架默认目录。"""
        if self.path is None:
            self.path = _default_path()
        return self.path

    def _read(self) -> dict[str, Any]:
        """读取并规整存储内容。

        文件缺失 / 不可读 / 内容损坏一律当作空存储处理，
        免得一个坏文件把整个插件弄成不可用。

        Returns:
            规整后的存储字典。
        """
        path = self._resolve_path()
        if not path.exists():
            return _empty()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("读取 %s 失败（%s），按空存储处理", path, exc)
            return _empty()
        if not isinstance(data, dict):
            return _empty()
        users = data.setdefault("users", {})
        if not isinstance(users, dict):
            data["users"] = {}
        subs = data.setdefault("subs", {})
        if not isinstance(subs, dict):
            subs = {}
            data["subs"] = subs
        groups = subs.setdefault("sign_groups", [])
        if not isinstance(groups, list):
            subs["sign_groups"] = []
        return data

    def _write(self, data: dict[str, Any]) -> None:
        """原子落盘，并把权限收紧到属主可读写。

        Args:
            data: 要序列化的存储字典。
        """
        path = self._resolve_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            # Windows 上 chmod 支持有限，失败不影响功能。
            pass
        os.replace(tmp, path)

    # ------------------------------------------------------------------ 用户

    async def get_user(self, user_key: str) -> dict[str, Any] | None:
        """读取单个用户的绑定记录。

        Args:
            user_key: 平台用户标识。

        Returns:
            用户记录；未绑定时为 ``None``。
        """
        async with self._lock:
            return self._read()["users"].get(str(user_key))

    async def all_users(self) -> dict[str, Any]:
        """返回全部绑定记录。"""
        async with self._lock:
            return dict(self._read()["users"])

    async def count_users(self) -> int:
        """返回已绑定用户数。"""
        async with self._lock:
            return len(self._read()["users"])

    async def upsert_user(
        self,
        user_key: str,
        token: str,
        source: str = "android",
        umo: str = "",
        games: list[str] | None = None,
    ) -> None:
        """写入或更新一个用户的绑定。

        重复绑定时保留原有的游戏列表、社区任务开关与签到记录，
        只覆盖 Token 相关的字段——否则用户重绑一次就会把偏好洗掉。

        Args:
            user_key: 平台用户标识。
            token: 库街区 Token。
            source: Token 来源。
            umo: 绑定时所在会话，用于之后的主动推送。
            games: 该用户要签到的游戏 ID 列表，缺省沿用旧值或默认值。
        """
        user_key = str(user_key)
        async with self._lock:
            data = self._read()
            users = data["users"]
            existing = users.get(user_key) or {}
            now = int(time.time())
            record = dict(existing)
            record.update(
                {
                    "token": token,
                    "source": source or "android",
                    "umo": umo or existing.get("umo") or "",
                    "nickname": existing.get("nickname") or "",
                    "games": list(games) if games else list(existing.get("games") or []),
                    "bbs_tasks": bool(existing.get("bbs_tasks", True)),
                    "last_sign": existing.get("last_sign") or "",
                    "last_result": existing.get("last_result") or "",
                    "invalid": False,
                    "created_at": existing.get("created_at") or now,
                    "updated_at": now,
                }
            )
            users[user_key] = record
            self._write(data)

    async def set_games(self, user_key: str, games: list[str]) -> bool:
        """设置用户要签到的游戏列表。

        Args:
            user_key: 平台用户标识。
            games: 游戏 ID 列表。

        Returns:
            用户存在并写入成功时为 ``True``。
        """
        user_key = str(user_key)
        async with self._lock:
            data = self._read()
            user = data["users"].get(user_key)
            if not user:
                return False
            user["games"] = [str(item) for item in games]
            user["updated_at"] = int(time.time())
            self._write(data)
            return True

    async def set_bbs_tasks(self, user_key: str, enabled: bool) -> bool:
        """开关某个用户的库街区社区任务。

        Args:
            user_key: 平台用户标识。
            enabled: 是否开启。

        Returns:
            用户存在并写入成功时为 ``True``。
        """
        user_key = str(user_key)
        async with self._lock:
            data = self._read()
            user = data["users"].get(user_key)
            if not user:
                return False
            user["bbs_tasks"] = bool(enabled)
            user["updated_at"] = int(time.time())
            self._write(data)
            return True

    async def save_result(self, user_key: str, lines: list[str], ok: bool) -> None:
        """记录一次签到的结果。

        Args:
            user_key: 平台用户标识。
            lines: 结果明细。
            ok: 本次是否算成功（成功才写 ``last_sign``，失败还会重试）。
        """
        user_key = str(user_key)
        async with self._lock:
            data = self._read()
            user = data["users"].get(user_key)
            if not user:
                return
            user["last_result"] = " / ".join(lines)[:500]
            if ok:
                user["last_sign"] = today()
                user["invalid"] = False
            user["updated_at"] = int(time.time())
            self._write(data)

    async def mark_invalid(self, user_key: str) -> None:
        """把某个用户标记为 Token 失效。

        Args:
            user_key: 平台用户标识。
        """
        user_key = str(user_key)
        async with self._lock:
            data = self._read()
            user = data["users"].get(user_key)
            if not user:
                return
            user["invalid"] = True
            user["updated_at"] = int(time.time())
            self._write(data)

    async def remove_user(self, user_key: str) -> bool:
        """删除一个用户并清掉其订阅。

        Args:
            user_key: 平台用户标识。

        Returns:
            用户存在时为 ``True``。
        """
        user_key = str(user_key)
        async with self._lock:
            data = self._read()
            if user_key not in data["users"]:
                return False
            data["users"].pop(user_key, None)
            self._write(data)
            return True

    # ------------------------------------------------------------------ 订阅

    async def list_sign_groups(self) -> list[dict[str, str]]:
        """返回自动签到结果的群通知目标。"""
        async with self._lock:
            return [dict(item) for item in self._read()["subs"]["sign_groups"]]

    async def set_sign_group(self, group_id: str, umo: str, enabled: bool) -> None:
        """开关某个群的自动签到结果通知。

        Args:
            group_id: 平台群号。
            umo: 群会话标识，开启时必填。
            enabled: 目标状态。
        """
        group_id = str(group_id)
        async with self._lock:
            data = self._read()
            groups = [
                item
                for item in data["subs"]["sign_groups"]
                if str(item.get("group_id", "")) != group_id
            ]
            if enabled:
                groups.append({"group_id": group_id, "umo": str(umo)})
            data["subs"]["sign_groups"] = groups
            self._write(data)
