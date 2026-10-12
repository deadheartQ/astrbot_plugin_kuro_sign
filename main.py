"""库街区（鸣潮 / 战双帕弥什）自动签到插件。

Token 只在私聊里绑定：一个 Token 等于一个账号的完整凭据，群里发一次等于送号。
绑定后每天按配置时间自动签到，结果推给订阅了通知的群，或私聊给账号主人。

接口层移植自 Night-stars-1/Kuro-Sign（MIT）。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

try:  # 正常按包加载时走相对导入
    from .core.api import KuroAuthError, KuroBusinessError, KuroClient, KuroError
    from .core.signer import (
        BbsOptions,
        game_name,
        game_options_text,
        games_text,
        parse_games,
        resolve_game_choice,
        sign_account,
    )
    from .core.store import Store, today
except ImportError:  # pragma: no cover - 少数加载方式下插件目录不在包路径上
    from core.api import KuroAuthError, KuroBusinessError, KuroClient, KuroError
    from core.signer import (
        BbsOptions,
        game_name,
        game_options_text,
        games_text,
        parse_games,
        resolve_game_choice,
        sign_account,
    )
    from core.store import Store, today

PLUGIN_NAME = "astrbot_plugin_kuro_sign"
PLUGIN_VERSION = "1.2.0"
GLOBAL_KEY = "__default__"
DEFAULT_GAMES = ("2", "3")
MAX_MESSAGE_CHARS = 1200
SMS_PENDING_TTL = 300
"""登录流程记住手机号的时长（秒）。只是一个中转，不落盘、不长期保留。"""

GAME_PENDING_TTL = 600
"""游戏菜单等待用户回复的时长（秒）。同短信登录：只在内存里，过期即忘。"""

_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
_CODE_RE = re.compile(r"^\d{4,8}$")

HELP_TEXT = """库街区自动签到

kj登录 手机号         短信验证码登录（不用抓包），按提示回复验证码即可
kj绑定 Token [来源]   已经抓过包的，可直接私聊绑定 Token（来源默认 android，可填 h5）
kj状态               查看绑定状态与最近一次签到结果
kj签到               立即签到自己的账号
kj游戏               选择要签到的游戏（弹出菜单后直接回序号或游戏名）
kj任务 on|off        开关库街区社区任务（签到 / 浏览 / 点赞 / 分享）
kj订阅               在群里开关本群的自动签到结果通知
kj解绑               解除绑定并删除 Token

短信登录：在库街区网页或 APP 的登录页点发送验证码，把手机号和验证码发给机器人即可。
Token 获取：也可用库街区 APP 抓包，或用 GetKjqToken 工具获取。
安全提醒：登录相关操作只在本机器人的私聊里做，不要把验证码或 Token 发到群里。"""

SMS_HELP_TEXT = """短信验证码登录（私聊）

方式一（推荐）：先到库街区网页或 APP 的登录页（kurobbs.com/login-page）
点「发送验证码」，收到后一次发过来：
    kj登录 手机号 验证码

方式二：先发「kj登录 手机号」让机器人帮你发码，再回复：
    kj登录 验证码

⚠️ 库街区是单设备登录：登录成功后很可能把手机 APP 上的登录态顶掉，
需要在 APP 里重新登录一次。介意的话请改用 kj绑定 <Token>（抓包方式，手机与机器人共存）。"""


@dataclass
class _Target:
    """待签到的一个账号。"""

    key: str
    label: str
    umo: str
    token: str
    source: str
    games: list[str]
    bbs: bool


@dataclass
class _Result:
    """一个账号的签到结果。"""

    key: str
    label: str
    umo: str
    lines: list[str] = field(default_factory=list)
    auth_error: bool = False


def _mask_mobile(mobile: str) -> str:
    """手机号中间四位打码，避免在聊天或日志里暴露完整号码。

    Args:
        mobile: 手机号。

    Returns:
        打码后的号码；长度不对时原样返回。
    """
    text = str(mobile or "")
    return f"{text[:3]}****{text[-4:]}" if len(text) == 11 else text


def _command_args(text: str, command: str) -> list[str]:
    """从消息原文里剥掉指令名并切出参数。

    Args:
        text: 消息原文。
        command: 指令名，如 ``kj绑定``。

    Returns:
        参数列表。
    """
    raw = str(text or "").strip()
    for prefix in (command, "/" + command):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    return [part for part in raw.split() if part]


def _parse_sign_time(raw: Any) -> tuple[int, int] | None:
    """解析 ``HH:MM`` 形式的签到时间。

    支持全角冒号与 ``HHMM`` 紧凑写法；解析不出时返回 ``None``，
    由调用方回退到默认时间，不抛异常——配置写错不该让插件起不来。

    Args:
        raw: 配置值。

    Returns:
        ``(hour, minute)`` 或 ``None``。
    """
    text = str(raw or "").strip().replace("：", ":").replace("．", ".").replace(".", ":")
    if not text:
        return None
    if ":" in text:
        head, _, tail = text.partition(":")
    elif len(text) in (3, 4) and text.isdigit():
        head, tail = text[:-2], text[-2:]
    else:
        return None
    try:
        hour, minute = int(head), int(tail)
    except (TypeError, ValueError):
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _chunks(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """按行把长文本切成若干段，避免超长消息发不出去。

    Args:
        text: 原始文本。
        limit: 单段字符上限。

    Returns:
        分段列表。
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def _group_id(event: AstrMessageEvent) -> str:
    """尽力取到当前群号，取不到返回空串。"""
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # noqa: BLE001 - 适配器差异不该影响指令
            return ""
    return ""


def _is_private(event: AstrMessageEvent) -> bool:
    """判断是否私聊会话。"""
    checker = getattr(event, "is_private_chat", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001, S110 - 个别适配器没实现，退回群号判断
            pass
    return not _group_id(event)


def _resolve_timezone(name: str) -> Any:
    """把时区名解析成 tzinfo，解析不了返回 ``None``。

    Args:
        name: 时区名，如 ``Asia/Shanghai``。

    Returns:
        tzinfo 对象或 ``None``（``None`` 表示用运行环境本地时区）。
    """
    text = str(name or "").strip()
    if not text:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(text)
    except Exception as exc:  # noqa: BLE001 - 时区数据缺失时降级
        logger.warning("[库街区] 时区 %s 不可用（%s），改用运行环境本地时区", text, exc)
        return None


@register(PLUGIN_NAME, "deadheartQ", "库街区（鸣潮 / 战双帕弥什）每日自动签到", PLUGIN_VERSION)
class KuroSignPlugin(Star):
    """库街区自动签到插件。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        """初始化插件。

        Args:
            context: AstrBot 插件上下文。
            config: 面板配置，来自 ``_conf_schema.json``。
        """
        super().__init__(context)
        self.config: dict[str, Any] = config if isinstance(config, dict) else {}
        self.store = Store()
        self.scheduler: Any = None
        self._job_lock = asyncio.Lock()
        # 短信登录流程里暂存的手机号：user_key -> (手机号, 过期时间戳)。
        # 只在内存里放几分钟，不落盘，也不写进日志。
        self._pending_sms: dict[str, tuple[str, float]] = {}
        # 游戏菜单的等待状态：user_key -> 过期时间戳。
        # 弹了菜单之后用户可以直接回「1」，不必再打一次 kj游戏。
        self._pending_games: dict[str, float] = {}

    # ----------------------------------------------------------------- 配置

    def _cfg(self, key: str, default: Any = None) -> Any:
        """读取配置项，兼容扁平与分组（object）两种 schema 写法。

        Args:
            key: 配置键名。
            default: 缺省值。

        Returns:
            配置值。
        """
        config = self.config
        if not isinstance(config, dict):
            return default
        if key in config:
            return config[key]
        for value in config.values():
            if isinstance(value, dict) and key in value:
                return value[key]
        return default

    def _cfg_str(self, key: str, default: str = "") -> str:
        """读取字符串配置。"""
        value = self._cfg(key, default)
        if value is None:
            return default
        text = str(value).strip()
        return text if text else default

    def _cfg_int(self, key: str, default: int = 0) -> int:
        """读取整数配置，容忍面板里被填成字符串。"""
        value = self._cfg(key, default)
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        """读取布尔配置，容忍 true/false、1/0、开/关 等写法。"""
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in ("true", "1", "yes", "y", "on", "是", "开", "开启"):
            return True
        if text in ("false", "0", "no", "n", "off", "否", "关", "关闭"):
            return False
        return default

    def _settings(self) -> dict[str, Any]:
        """一次性读出全部配置，便于多个调用点共用同一份快照。"""
        source = self._cfg_str("default_source", "android").lower()
        return {
            "sign_time": self._cfg_str("sign_time", "00:05"),
            "timezone": self._cfg_str("timezone", "Asia/Shanghai"),
            "max_users": self._cfg_int("max_users", 10),
            "request_timeout": self._cfg_int("request_timeout", 15),
            "delay_seconds": self._cfg_int("delay_seconds", 3),
            "default_token": self._cfg_str("default_token", ""),
            "default_source": source if source in ("android", "h5") else "android",
            "sign_game": self._cfg_str("sign_game", "两个都签"),
            "enable_sms_login": self._cfg_bool("enable_sms_login", True),
            "auto_send_code": self._cfg_bool("auto_send_code", True),
            "notify_umo": self._cfg_str("notify_umo", ""),
            "enable_bbs_tasks": self._cfg_bool("enable_bbs_tasks", False),
            "bbs_sign": self._cfg_bool("bbs_sign", True),
            "look_post": self._cfg_bool("look_post", True),
            "like_post": self._cfg_bool("like_post", True),
            "share_post": self._cfg_bool("share_post", True),
        }

    def _bbs_options(self, settings: dict[str, Any]) -> BbsOptions:
        """按配置组装社区任务开关。"""
        return BbsOptions(
            bbs_sign=bool(settings["bbs_sign"]),
            look_post=bool(settings["look_post"]),
            like_post=bool(settings["like_post"]),
            share_post=bool(settings["share_post"]),
        )

    def _make_client(self, token: str, source: str) -> KuroClient:
        """按当前超时配置建一个客户端。"""
        return KuroClient(
            token,
            source,
            timeout=float(self._cfg_int("request_timeout", 15) or 15),
        )

    # ----------------------------------------------------------- 生命周期

    async def initialize(self) -> None:
        """启动每日自动签到定时任务。"""
        settings = self._settings()
        parsed = _parse_sign_time(settings["sign_time"])
        if parsed is None:
            logger.warning("[库街区] sign_time=%r 解析失败，回退到 00:05", settings["sign_time"])
            parsed = (0, 5)
        hour, minute = parsed
        tz_name = str(settings["timezone"] or "")
        tz = _resolve_timezone(tz_name)

        logger.info(
            "[库街区] 插件已加载：签到时间 %02d:%02d（%s），社区任务=%s，已绑定 %d 个账号",
            hour,
            minute,
            tz_name or "本地时区",
            settings["enable_bbs_tasks"],
            await self.store.count_users(),
        )

        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError as exc:
            logger.error("[库街区] 缺少 APScheduler，自动签到未启用：%s", exc)
            self.scheduler = None
            return

        try:
            scheduler = AsyncIOScheduler(timezone=tz)
            scheduler.add_job(
                self._auto_sign_job,
                CronTrigger(hour=hour, minute=minute),
                id="kuro_auto_sign",
                replace_existing=True,
            )
            scheduler.start()
            self.scheduler = scheduler
            logger.info("[库街区] 定时任务已启动：每天 %02d:%02d 执行自动签到", hour, minute)
        except Exception as exc:  # noqa: BLE001 - 定时任务失败不能挡住插件加载
            logger.error("[库街区] 启动定时任务失败：%s", exc)
            self.scheduler = None

    async def terminate(self) -> None:
        """停止定时任务。"""
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception as exc:  # noqa: BLE001 - 关闭失败无需上报
                logger.debug("[库街区] 关闭定时任务失败：%s", exc)
            self.scheduler = None

    # -------------------------------------------------------------- 签到核心

    async def _sign_target(self, target: _Target, options: BbsOptions) -> _Result:
        """给一个账号跑完整套签到，并落库。

        Args:
            target: 待签到账号。
            options: 社区任务开关。

        Returns:
            签到结果。
        """
        client = self._make_client(target.token, target.source)
        try:
            report = await sign_account(client, target.games, bbs_tasks=target.bbs, options=options)
        finally:
            await client.close()
        if report.auth_error:
            await self.store.mark_invalid(target.key)
        await self.store.save_result(target.key, report.lines, report.ok)
        return _Result(
            key=target.key,
            label=target.label,
            umo=target.umo,
            lines=list(report.lines),
            auth_error=report.auth_error,
        )

    async def _collect_targets(self, settings: dict[str, Any]) -> list[_Target]:
        """汇总所有需要签到的账号（用户绑定的 + 配置里的默认 Token）。"""
        default_games = parse_games(settings["sign_game"], DEFAULT_GAMES)
        users = await self.store.all_users()
        targets: list[_Target] = []

        default_token = str(settings["default_token"] or "")
        if default_token:
            targets.append(
                _Target(
                    key=GLOBAL_KEY,
                    label="默认账号",
                    umo=str(settings["notify_umo"] or ""),
                    token=default_token,
                    source=str(settings["default_source"]),
                    games=list(default_games),
                    bbs=bool(settings["enable_bbs_tasks"]),
                )
            )

        for key in sorted(users):
            user = users[key] or {}
            token = str(user.get("token") or "")
            if not token:
                continue
            source = str(user.get("source") or settings["default_source"])
            targets.append(
                _Target(
                    key=str(key),
                    label=str(user.get("nickname") or key),
                    umo=str(user.get("umo") or ""),
                    token=token,
                    source=source if source in ("android", "h5") else "android",
                    games=parse_games(user.get("games"), default_games) or list(default_games),
                    bbs=bool(user.get("bbs_tasks", True)) and bool(settings["enable_bbs_tasks"]),
                )
            )

        max_users = int(settings["max_users"] or 0)
        if max_users and len(targets) > max_users:
            logger.warning(
                "[库街区] 待签到账号 %d 个，超过 max_users=%d，只处理前 %d 个",
                len(targets),
                max_users,
                max_users,
            )
            targets = targets[:max_users]
        return targets

    async def _run_signs(self, scheduled: bool) -> list[_Result]:
        """给所有账号签到。

        Args:
            scheduled: 定时任务发起的运行；此时当天已签到的账号会跳过。

        Returns:
            每个账号的结果。
        """
        settings = self._settings()
        options = self._bbs_options(settings)
        targets = await self._collect_targets(settings)
        if not targets:
            logger.info("[库街区] 没有待签到的账号，跳过")
            return []

        users = await self.store.all_users()
        delay = max(0, int(settings["delay_seconds"] or 0))
        stamp = today()
        results: list[_Result] = []

        for index, target in enumerate(targets):
            if scheduled:
                record = users.get(target.key) or {}
                if str(record.get("last_sign") or "") == stamp:
                    results.append(
                        _Result(
                            key=target.key,
                            label=target.label,
                            umo=target.umo,
                            lines=["今日已签到，跳过"],
                        )
                    )
                    continue
            if index and delay:
                await asyncio.sleep(delay)
            try:
                result = await self._sign_target(target, options)
            except Exception as exc:  # noqa: BLE001 - 单个账号失败不能中断整轮
                logger.exception("[库街区] 账号 %s 签到异常", target.key)
                result = _Result(
                    key=target.key,
                    label=target.label,
                    umo=target.umo,
                    lines=[f"签到异常：{exc}"],
                )
            results.append(result)
            logger.info("[库街区] %s 签到结果：%s", target.label, " / ".join(result.lines))
        return results

    async def _deliver(self, results: list[_Result], settings: dict[str, Any]) -> None:
        """把签到结果推送给订阅的群或账号主人。

        有群订阅时结果只进群；但 Token 失效这类必须让本人知道的问题
        仍然额外私聊一份，否则用户会一直以为签到正常。
        """
        if not results:
            return
        header = f"库街区自动签到结果（{time.strftime('%Y-%m-%d')}）"
        multi = len(results) > 1
        blocks = [
            "\n".join(([f"【{item.label}】"] if multi else []) + item.lines) for item in results
        ]
        whole = "\n".join([header, *blocks])

        groups = await self.store.list_sign_groups()
        delivered: set[str] = set()
        if groups:
            for group in groups:
                umo = str(group.get("umo") or "")
                if umo:
                    delivered.add(umo)
                    for part in _chunks(whole):
                        await self._notify(umo, part)
        else:
            for item in results:
                if not item.umo:
                    continue
                delivered.add(item.umo)
                text = "\n".join(item.lines)
                for part in _chunks(f"{header}\n【{item.label}】\n{text}"):
                    await self._notify(item.umo, part)

        extra = str(settings["notify_umo"] or "")
        if extra and extra not in delivered:
            delivered.add(extra)
            for part in _chunks(whole):
                await self._notify(extra, part)

        for item in results:
            if not item.auth_error or not item.umo or item.umo in delivered:
                continue
            delivered.add(item.umo)
            await self._notify(
                item.umo,
                f"【{item.label}】库街区 Token 已失效，自动签到停了。"
                "请私聊发送 kj绑定 <新Token> 重新绑定。",
            )

    async def _notify(self, umo: str, text: str) -> None:
        """主动向指定会话发消息。

        Args:
            umo: 目标会话标识。
            text: 消息文本。
        """
        if not umo:
            return
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:  # noqa: BLE001 - 推送失败不能影响签到本身
            logger.warning("[库街区] 向 %s 发送消息失败：%s", umo, exc)

    async def _auto_sign_job(self) -> None:
        """定时任务入口：签到所有账号并推送结果。"""
        if self._job_lock.locked():
            logger.warning("[库街区] 上一轮自动签到尚未结束，本轮跳过")
            return
        async with self._job_lock:
            settings = self._settings()
            try:
                results = await self._run_signs(scheduled=True)
            except Exception as exc:  # noqa: BLE001 - 定时任务必须自己吞掉异常
                logger.exception("[库街区] 自动签到失败")
                await self._notify(str(settings["notify_umo"] or ""), f"库街区自动签到失败：{exc}")
                return
            await self._deliver(results, settings)

    # ---------------------------------------------------------------- 指令

    @filter.command("kj帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看库街区签到插件用法。"""
        yield event.plain_result(HELP_TEXT)

    @filter.command("kj绑定")
    async def cmd_bind(self, event: AstrMessageEvent):
        """私聊绑定库街区 Token。"""
        args = _command_args(event.message_str, "kj绑定")
        if not args:
            yield event.plain_result(
                "用法：kj绑定 <Token> [android|h5]\n"
                "Token 通过库街区 APP 抓包获取（只私聊发送，不要发群里）。"
            )
            return
        if not _is_private(event):
            yield event.plain_result(
                "Token 是账号凭据，请私聊机器人发送本指令。群里发等于把号送人。"
            )
            return

        settings = self._settings()
        token = args[0].strip().strip('"').strip("'")
        if not token:
            yield event.plain_result("Token 为空，请重新发送。")
            return
        source = args[1].strip().lower() if len(args) > 1 else settings["default_source"]
        if source not in ("android", "h5"):
            source = "android"

        client = self._make_client(token, source)
        warn = ""
        try:
            roles = await self._probe_roles(client)
        except KuroAuthError as exc:
            # 网页版（h5）与 APP（android）的 Token 不通用，用错 source 是最常见的失败原因。
            # 注意：这里**不能**把 token 回显出来，聊天记录会留存完整凭据。
            other = "h5" if source == "android" else "android"
            yield event.plain_result(
                f"Token 校验不通过：{exc}\n"
                f"当前是按「{source}」校验的。网页版（电脑浏览器）取的 Token 要填 h5，"
                f"手机 APP 抓包的填 android，两者不通用。\n"
                f"换个来源再试：kj绑定 <Token> {other}"
            )
            return
        except KuroError as exc:
            roles = []
            warn = f"\n注意：本次没能连上库街区（{exc}），Token 已保存但未验证。"
        finally:
            await client.close()

        user_key = str(event.get_sender_id())
        await self.store.upsert_user(
            user_key,
            token=token,
            source=source,
            umo=str(event.unified_msg_origin or ""),
        )
        detail = "、".join(roles) if roles else "未查到游戏角色"
        default_games = parse_games(settings["sign_game"], DEFAULT_GAMES)
        self._mark_game_pending(user_key)
        yield event.plain_result(
            f"绑定成功。\n可用角色：{detail}\n"
            f"签到游戏：{games_text(default_games)}\n"
            f"{self._game_menu()}\n"
            f"也可以直接发送 kj签到 先签一次。{warn}"
        )

    @filter.command("kj登录")
    async def cmd_sms_login(self, event: AstrMessageEvent):
        """用手机号 + 短信验证码登录库街区（不用抓包）。"""
        settings = self._settings()
        if not settings["enable_sms_login"]:
            yield event.plain_result("短信登录已在插件配置里关闭，请改用 kj绑定 <Token>。")
            return
        if not _is_private(event):
            yield event.plain_result("登录涉及验证码和账号凭据，请私聊机器人操作。")
            return

        args = _command_args(event.message_str, "kj登录")
        if not args:
            yield event.plain_result(SMS_HELP_TEXT)
            return

        user_key = str(event.get_sender_id())
        mobile = ""
        code = ""
        if len(args) >= 2:
            mobile, code = args[0], args[1]
        elif _MOBILE_RE.match(args[0]):
            mobile = args[0]
        elif _CODE_RE.match(args[0]):
            code = args[0]
        else:
            yield event.plain_result(f"看不懂「{args[0]}」。\n\n{SMS_HELP_TEXT}")
            return

        if mobile and not _MOBILE_RE.match(mobile):
            yield event.plain_result("手机号格式不正确，应该形如 13800138000。")
            return
        if code and not _CODE_RE.match(code):
            yield event.plain_result("验证码格式不正确，应该是一串数字。")
            return

        if not code:
            # 只给了手机号：先记下来，再尽力帮用户发码
            self._pending_sms[user_key] = (mobile, time.time() + SMS_PENDING_TTL)
            hint = await self._send_sms_code(mobile, settings)
            yield event.plain_result(
                f"{hint}\n\n收到验证码后回复：kj登录 验证码\n"
                f"（{SMS_PENDING_TTL // 60} 分钟内有效）\n\n"
                "⚠️ 库街区是单设备登录，登录成功后手机 APP 上的登录态可能被顶掉，"
                "需要重新登录一次。介意就改用 kj绑定 <Token>。"
            )
            return

        if not mobile:
            mobile = self._pop_pending_mobile(user_key)
            if not mobile:
                yield event.plain_result(
                    "没记着你的手机号（可能已经超过 5 分钟）。请先发：kj登录 手机号\n\n"
                    f"{SMS_HELP_TEXT}"
                )
                return

        login_client = self._make_client("", str(settings["default_source"]))
        try:
            result = await login_client.login_with_sms(mobile, code)
        except KuroBusinessError as exc:
            yield event.plain_result(
                f"登录失败：{exc}\n请确认验证码是否正确、是否已经过期，然后重发「kj登录 手机号」。"
            )
            return
        except KuroError as exc:
            yield event.plain_result(f"登录时接口异常：{exc}\n稍后再试一次。")
            return
        finally:
            await login_client.close()

        token = str(result["token"])
        verify_client = self._make_client(token, str(settings["default_source"]))
        warn = ""
        try:
            roles = await self._probe_roles(verify_client)
        except KuroAuthError as exc:
            yield event.plain_result(f"登录接口通了，但拿到的凭据校验不通过：{exc}\n请重试。")
            return
        except KuroError as exc:
            roles = []
            warn = f"\n注意：这次没能连上库街区（{exc}），凭据已保存但未验证。"
        finally:
            await verify_client.close()

        self._pending_sms.pop(user_key, None)
        await self.store.upsert_user(
            user_key,
            token=token,
            source=str(settings["default_source"]),
            umo=str(event.unified_msg_origin or ""),
        )
        detail = "、".join(roles) if roles else "未查到游戏角色"
        default_games = parse_games(settings["sign_game"], DEFAULT_GAMES)
        logger.info("[库街区] %s 用短信登录完成绑定", user_key)
        self._mark_game_pending(user_key)
        yield event.plain_result(
            f"登录成功，已绑定 {_mask_mobile(mobile)}。\n可用角色：{detail}\n"
            f"签到游戏：{games_text(default_games)}\n"
            f"{self._game_menu()}\n"
            f"也可以直接发送 kj签到 先签一次。{warn}"
        )

    @filter.command("kj状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看自己的绑定与签到状态。"""
        user_key = str(event.get_sender_id())
        user = await self.store.get_user(user_key)
        settings = self._settings()
        lines = [
            f"自动签到时间：{settings['sign_time']}（{settings['timezone'] or '本地时区'}）",
            f"社区任务：{'开启' if settings['enable_bbs_tasks'] else '关闭'}",
        ]
        if not user:
            lines.append("你还没有绑定 Token，私聊发送 kj绑定 <Token> 开始使用。")
            yield event.plain_result("\n".join(lines))
            return

        games = parse_games(user.get("games"), parse_games(settings["sign_game"], DEFAULT_GAMES))
        own_task = "开启" if user.get("bbs_tasks", True) else "关闭"
        master = "开" if settings["enable_bbs_tasks"] else "关"
        source = user.get("source") or "android"
        tail = str(user.get("token") or "")[-4:]
        lines.extend(
            [
                f"Token：已绑定（{source}，尾号 {tail}）",
                f"签到游戏：{games_text(games)}",
                f"本账号社区任务：{own_task}（总开关{master}）",
                f"最近签到：{user.get('last_sign') or '暂无记录'}",
                f"最近结果：{user.get('last_result') or '暂无'}",
            ]
        )
        if user.get("invalid"):
            lines.append("状态：Token 疑似失效，请重新绑定。")

        group_id = _group_id(event)
        if group_id:
            groups = {str(item.get("group_id")) for item in await self.store.list_sign_groups()}
            lines.append(
                f"本群签到通知：{'已开启' if group_id in groups else '未开启（发送 kj订阅 开启）'}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("kj签到")
    async def cmd_sign(self, event: AstrMessageEvent):
        """立即签到自己的账号。"""
        user_key = str(event.get_sender_id())
        user = await self.store.get_user(user_key)
        if not user or not user.get("token"):
            yield event.plain_result("你还没有绑定 Token，私聊发送 kj绑定 <Token>。")
            return

        settings = self._settings()
        games = parse_games(user.get("games"), parse_games(settings["sign_game"], DEFAULT_GAMES))
        target = _Target(
            key=user_key,
            label=str(user.get("nickname") or user_key),
            umo=str(event.unified_msg_origin or ""),
            token=str(user["token"]),
            source=str(user.get("source") or settings["default_source"]),
            games=games,
            bbs=bool(user.get("bbs_tasks", True)) and bool(settings["enable_bbs_tasks"]),
        )
        try:
            result = await self._sign_target(target, self._bbs_options(settings))
        except Exception as exc:  # noqa: BLE001 - 指令不能把异常抛回管线
            logger.exception("[库街区] 手动签到异常")
            yield event.plain_result(f"签到失败：{exc}")
            return
        body = "\n".join(result.lines)
        if result.auth_error:
            body += "\nToken 已失效，请私聊发送 kj绑定 <新Token> 重新绑定。"
        yield event.plain_result(body)

    @filter.command("kj游戏")
    async def cmd_games(self, event: AstrMessageEvent):
        """设置要签到的游戏。"""
        user_key = str(event.get_sender_id())
        user = await self.store.get_user(user_key)
        if not user:
            yield event.plain_result("你还没有绑定 Token，私聊发送 kj绑定 <Token>。")
            return
        args = _command_args(event.message_str, "kj游戏")
        settings = self._settings()
        if not args:
            games = parse_games(
                user.get("games"), parse_games(settings["sign_game"], DEFAULT_GAMES)
            )
            # 弹出菜单就进入等待：用户接下来直接回「1」也算数，不用再打一次命令。
            self._mark_game_pending(user_key)
            yield event.plain_result(f"当前签到游戏：{games_text(games)}\n{self._game_menu()}")
            return

        games = parse_games(" ".join(args), default=[])
        if not games:
            self._mark_game_pending(user_key)
            yield event.plain_result(
                f"没解析出游戏「{' '.join(args)}」，请回复序号或游戏名：\n{game_options_text()}"
            )
            return
        await self.store.set_games(user_key, games)
        yield event.plain_result(f"已设置签到游戏：{games_text(games)}")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """接住「对着游戏菜单直接回序号」的那句话。

        这个监听挂在所有私聊消息上，所以条件卡得很紧：只有在菜单等待期内、
        且内容明确是游戏选择时才接管，其余一律放行。
        """
        user_key = str(event.get_sender_id())
        if not self._game_pending(user_key):
            return
        text = (event.message_str or "").strip()
        # 仍然是命令就交给命令处理器，这里不抢，也不消费等待状态。
        if text.startswith("kj"):
            return
        # 只等一句：无论有没有解析成功都结束等待，
        # 免得十分钟内用户随口打的每个数字都被当成选择。
        games = resolve_game_choice(text)
        self._clear_game_pending(user_key)
        if not games:
            return
        await self.store.set_games(user_key, games)
        event.stop_event()
        yield event.plain_result(f"已设置签到游戏：{games_text(games)}")

    @filter.command("kj任务")
    async def cmd_tasks(self, event: AstrMessageEvent):
        """开关自己的库街区社区任务。"""
        user_key = str(event.get_sender_id())
        user = await self.store.get_user(user_key)
        if not user:
            yield event.plain_result("你还没有绑定 Token，私聊发送 kj绑定 <Token>。")
            return
        args = _command_args(event.message_str, "kj任务")
        settings = self._settings()
        if not args:
            state = "开启" if user.get("bbs_tasks", True) else "关闭"
            hint = (
                "" if settings["enable_bbs_tasks"] else "（插件总开关当前为关闭，需在配置里开启）"
            )
            yield event.plain_result(f"当前社区任务：{state}{hint}\n用法：kj任务 on / kj任务 off")
            return

        flag = args[0].strip().lower()
        if flag in ("on", "开", "开启", "1", "true"):
            enabled = True
        elif flag in ("off", "关", "关闭", "0", "false"):
            enabled = False
        else:
            yield event.plain_result("参数无效，用法：kj任务 on / kj任务 off")
            return
        await self.store.set_bbs_tasks(user_key, enabled)
        suffix = (
            "" if settings["enable_bbs_tasks"] else "（注意：插件总开关当前关闭，实际不会执行）"
        )
        yield event.plain_result(f"已{'开启' if enabled else '关闭'}社区任务。{suffix}")

    @filter.command("kj订阅")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """在群里开关本群的自动签到结果通知。"""
        group_id = _group_id(event)
        if not group_id:
            yield event.plain_result(
                "私聊无需订阅：签到结果会自动私聊给你。\n"
                "本指令用于在群里开启或关闭自动签到结果通知。"
            )
            return
        groups = {str(item.get("group_id")) for item in await self.store.list_sign_groups()}
        enabled = group_id not in groups
        await self.store.set_sign_group(group_id, str(event.unified_msg_origin or ""), enabled)
        yield event.plain_result(
            f"本群自动签到结果通知已{'开启' if enabled else '关闭'}。"
            + ("" if enabled else "（绑定账号的用户仍会收到私聊提醒）")
        )

    @filter.command("kj解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """解除绑定并删除本地保存的 Token。"""
        user_key = str(event.get_sender_id())
        if await self.store.remove_user(user_key):
            yield event.plain_result("已解绑，本地保存的 Token 已删除。")
        else:
            yield event.plain_result("你还没有绑定 Token。")

    # ---------------------------------------------------------------- 工具

    async def _probe_roles(self, client: KuroClient) -> list[str]:
        """探测 Token 是否可用，并列出已绑定的角色。

        Args:
            client: 待校验的客户端。

        Returns:
            形如 ``鸣潮·天空岛`` 的角色描述列表；一个都查不到时抛异常。

        Raises:
            KuroAuthError: Token 失效。
            KuroError: 两个游戏都取不到数据（多为网络或风控）。
        """
        found: list[str] = []
        last_error: KuroError | None = None
        for game_id in ("2", "3"):
            try:
                roles = await client.get_role_list(game_id)
            except KuroAuthError:
                raise
            except KuroError as exc:
                last_error = exc
                continue
            for role in roles:
                found.append(f"{game_name(game_id)}·{role.get('serverName') or '默认服'}")
        if not found and last_error is not None:
            raise last_error
        return found

    def _pop_pending_mobile(self, user_key: str) -> str:
        """取出并清掉暂存的手机号，过期则返回空串。

        Args:
            user_key: 平台用户标识。

        Returns:
            仍在有效期内的手机号，否则空串。
        """
        item = self._pending_sms.get(user_key)
        if not item:
            return ""
        mobile, expire_at = item
        self._pending_sms.pop(user_key, None)
        if time.time() > expire_at:
            return ""
        return mobile

    def _mark_game_pending(self, user_key: str) -> None:
        """标记「这个用户刚看过游戏菜单」，等他直接回一句。"""
        self._pending_games[user_key] = time.time() + GAME_PENDING_TTL

    def _game_pending(self, user_key: str) -> bool:
        """是否还在等这个用户回一句菜单选择（顺便清掉过期的）。"""
        if time.time() > self._pending_games.get(user_key, 0.0):
            self._pending_games.pop(user_key, None)
            return False
        return True

    def _clear_game_pending(self, user_key: str) -> None:
        """结束菜单等待。菜单只等一句，清掉后不再拦截用户的话。"""
        self._pending_games.pop(user_key, None)

    def _game_menu(self) -> str:
        """渲染一段可直接回复的游戏菜单。"""
        minutes = GAME_PENDING_TTL // 60
        return f"回复序号或游戏名即可切换（{minutes} 分钟内直接发就行）：\n{game_options_text()}"

    async def _send_sms_code(self, mobile: str, settings: dict[str, Any]) -> str:
        """尽力让插件自己发验证码，失败则引导用户去登录页自己取码。

        库街区登录页带极验风控，这个接口不一定放行；所以失败**不是错误**，
        而是切换成「用户自己点发送验证码」的路径。

        Args:
            mobile: 手机号。
            settings: 当前配置快照。

        Returns:
            给用户看的一句话。手机号只以打码形式出现。
        """
        masked = _mask_mobile(mobile)
        if not settings["auto_send_code"]:
            return f"请到库街区网页或 APP 的登录页点「发送验证码」（发给 {masked}）。"
        client = self._make_client("", str(settings["default_source"]))
        try:
            message = await client.send_sms_code(mobile)
        except KuroError as exc:
            logger.warning("[库街区] 自动发送验证码失败（%s）：%s", masked, exc)
            return (
                f"自动发送验证码失败（{exc}）。\n"
                "请到库街区网页或 APP 的登录页（kurobbs.com/login-page）自己点「发送验证码」。"
            )
        finally:
            await client.close()
        return f"验证码已发送到 {masked}（{message}）。"
