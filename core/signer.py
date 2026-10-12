"""签到编排：把一个账号该做的事按顺序跑完，并汇总成人能读的文本。

流程对齐 Kuro-Sign 的 ``main.py``：

1. 逐个游戏拉角色列表 → 每个角色签到 → 查本次奖品；
2. 拉社区每日任务进度 → 按任务名分派到签到 / 浏览 / 点赞 / 分享。

与原脚本的两点差异：

* 原脚本用 ``forum_list[i]`` 取帖子，帖子列表比任务次数短时会 IndexError，
  这里改成取模，帖子不够就复用前面的；
* 接口失败不再直接 ``raise`` 打断整条流程——单个游戏签到失败不该让
  社区任务和别的游戏一起挂掉。只有鉴权失效才立刻中断并要求重新绑定。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .api import (
    KuroAuthError,
    KuroBusinessError,
    KuroClient,
    KuroError,
    is_already_done,
)

GAME_NAMES = {"2": "战双帕弥什", "3": "鸣潮"}

# 面板下拉与聊天菜单共用这一张表：序号 → 选项文案 → 游戏 ID。
# 序号 2 / 3 与游戏 ID 是同一个含义（战双 / 鸣潮），只有 1 是新增的「都要」，
# 所以老用户写 kj游戏 3 的行为不变，只是多了一条更省事的路径。
GAME_OPTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("1", "两个都签", ("2", "3")),
    ("2", "只签战双帕弥什", ("2",)),
    ("3", "只签鸣潮", ("3",)),
)

# 用户可能写的游戏名（小写）→ 游戏 ID。顺序敏感：先长后短，命中即止。
_GAME_ALIASES: tuple[tuple[str, str], ...] = (
    ("战双帕弥什", "2"),
    ("战双", "2"),
    ("帕弥什", "2"),
    ("pns", "2"),
    ("鸣潮", "3"),
    ("wuthering", "3"),
    ("mc", "3"),
)

_ALL_HINTS = ("全部", "都签", "都要", "两个都", "所有", "全签", "all")
_GAME_SEPARATORS = ("|", ",", "，", "、", ";", "；", "+", "＋", "/", " ")


@dataclass
class BbsOptions:
    """社区任务开关。"""

    bbs_sign: bool = True
    look_post: bool = True
    like_post: bool = True
    share_post: bool = True


@dataclass
class SignReport:
    """一个账号的签到结果。"""

    lines: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    auth_error: bool = False

    def as_text(self) -> str:
        """把明细拼成多行文本。"""
        return "\n".join(self.lines)


def game_name(game_id: Any) -> str:
    """把游戏 ID 转成可读名字。

    Args:
        game_id: 游戏 ID。

    Returns:
        游戏名；未知 ID 回退成「游戏N」。
    """
    key = str(game_id or "").strip()
    return GAME_NAMES.get(key, f"游戏{key}" if key else "游戏")


def parse_games(raw: Any, default: Sequence[str] = ("2", "3")) -> list[str]:
    """解析签到游戏配置 / 用户输入。

    同时吃四种写法，方便用户「选」而不是「记 ID」：

    * 序号：``1`` / ``2`` / ``3``（对应 :data:`GAME_OPTIONS`）
    * 选项文案：``两个都签`` / ``只签鸣潮``
    * 游戏名：``鸣潮`` / ``战双``
    * 原始 ID：``2|3``（老配置继续可用）

    Args:
        raw: 配置值或用户输入；支持 ``|``、``,``、顿号、加号、空格等分隔。
        default: 解析不出内容时的回退值。

    Returns:
        去重且保序的游戏 ID 列表。
    """
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        text = str(raw or "")
        for sep in _GAME_SEPARATORS:
            text = text.replace(sep, "|")
        items = [part for part in text.split("|") if part]
    result: list[str] = []
    for item in items:
        for game_id in _resolve_game_token(item):
            if game_id not in result:
                result.append(game_id)
    return result or list(default)


def _resolve_game_token(token: str) -> list[str]:
    """把单个词解析成游戏 ID 列表。

    Args:
        token: 用户写的一个词。

    Returns:
        游戏 ID 列表；看不懂时返回空列表（由调用方决定是报错还是回退默认值）。
    """
    text = token.strip().lower()
    if not text:
        return []
    for hint in _ALL_HINTS:
        if hint in text:
            return ["2", "3"]
    for index, label, games in GAME_OPTIONS:
        if text == index or text == label.lower():
            return list(games)
    for name, game_id in _GAME_ALIASES:
        if name in text:
            return [game_id]
    if text.isdigit():
        return [text]
    return []


def game_options_text() -> str:
    """渲染聊天里可直接回复的编号菜单。"""
    return "\n".join(f"{index}. {label}" for index, label, _ in GAME_OPTIONS)


def resolve_game_choice(text: str) -> list[str]:
    """严格判断一句话是不是「对着菜单做的选择」。

    与 :func:`parse_games` 的区别：这个给**私聊消息监听**用，
    看不懂就返回空列表，绝不含糊。理由很实际——监听挂在所有私聊消息上，
    误判会把用户本来想跟机器人说的话吞掉，比漏判严重得多。

    三道闸：长度、能否解析、解析结果是不是已知游戏 ID。

    * 长度闸放在最前面，是因为 :func:`parse_games` 会做子串匹配，
      一句「我想把鸣潮和战双都签上啊」里含「都签」就会被判成「两个都签」。
      菜单回复本来就很短（最长的「只签战双帕弥什」也才 7 个字），
      超过 8 字基本可以断定不是在选游戏。
    * 最后一道同样关键：`parse_games` 对任意数字都会原样收下
      （``2026`` → ``["2026"]``），不加这道闸，一串数字就会被当成游戏 ID 存进去。

    Args:
        text: 用户发来的原始消息。

    Returns:
        命中的游戏 ID 列表；没把握时返回空列表。
    """
    raw = str(text or "").strip()
    if not raw or len(raw) > 8:
        return []
    games = parse_games(raw, default=[])
    if not games or any(item not in GAME_NAMES for item in games):
        return []
    return games


def games_text(games: Sequence[str]) -> str:
    """把游戏 ID 列表渲染成可读文本。

    Args:
        games: 游戏 ID 列表。

    Returns:
        形如「战双帕弥什 + 鸣潮」的文本。
    """
    return " + ".join(game_name(item) for item in games) or "（未设置）"


async def _sign_role(client: KuroClient, role: dict[str, Any]) -> tuple[str, bool]:
    """给单个角色签到。

    Args:
        client: 已绑定 Token 的客户端。
        role: ``/gamer/role/list`` 返回的角色字典。

    Returns:
        ``(结果文本, 是否成功)``。
    """
    game_id = str(role.get("gameId") or "")
    server_id = str(role.get("serverId") or "")
    role_id = str(role.get("roleId") or "")
    label = f"{game_name(game_id)}·{role.get('serverName') or '默认服'}"
    try:
        message = await client.sign_game(game_id, server_id, role_id)
    except KuroBusinessError as exc:
        text = str(exc)
        # 「今日已签到」是业务拒绝而非失败，算成功，否则每天重跑都会报红。
        if is_already_done(text):
            return f"{label}：{text}", True
        return f"{label}：签到失败 - {text}", False
    prize = await client.query_prize(game_id, server_id, role_id)
    detail = f"{message}，获得 {prize}" if prize else message
    return f"{label}：{detail}", True


async def run_bbs_tasks(client: KuroClient, options: BbsOptions) -> tuple[list[str], bool]:
    """执行库街区社区每日任务。

    Args:
        client: 已绑定 Token 的客户端。
        options: 各类任务的开关。

    Returns:
        ``(明细行, 是否全部成功)``。
    """
    tasks = await client.get_task_list()
    if not tasks:
        return ["社区任务：未取到任务列表，已跳过"], True

    lines: list[str] = []
    ok = True
    forum_list: list[dict[str, Any]] | None = None

    async def post_at(index: int) -> dict[str, Any] | None:
        """按序号取一条帖子，必要时拉取列表。

        Args:
            index: 任务内的第几次操作。

        Returns:
            帖子字典；列表为空时返回 ``None``。
        """
        nonlocal forum_list
        if forum_list is None:
            forum_list = await client.get_forum_list()
        if not forum_list:
            return None
        return forum_list[index % len(forum_list)]

    for task in tasks:
        remark = str(task.get("remark") or "未命名任务")
        try:
            need = int(task.get("needActionTimes") or 0)
        except (TypeError, ValueError):
            need = 0
        try:
            done = int(task.get("completeTimes") or 0)
        except (TypeError, ValueError):
            done = 0
        remain = max(0, need - done)

        if remain == 0:
            lines.append(f"{remark}：已完成")
            continue

        if "签到" in remark:
            if not options.bbs_sign:
                lines.append(f"{remark}：未开启，跳过")
                continue
            try:
                message = await client.bbs_sign(2)
                lines.append(f"{remark}：{message}")
            except KuroError as exc:
                ok = False
                lines.append(f"{remark}：失败 - {exc}")
            continue

        if "浏" in remark or "阅读" in remark or "看帖" in remark:
            if not options.look_post:
                lines.append(f"{remark}：未开启，跳过")
                continue
            titles: list[str] = []
            for index in range(remain):
                post = await post_at(index)
                if not post:
                    break
                try:
                    titles.append(await client.get_forum_detail(str(post.get("postId") or "")))
                except KuroError as exc:
                    ok = False
                    titles.append(f"读取失败（{exc}）")
            lines.append(f"{remark}：已浏览 {'、'.join(titles) if titles else '无可用帖子'}")
            continue

        if "点赞" in remark:
            if not options.like_post:
                lines.append(f"{remark}：未开启，跳过")
                continue
            results: list[str] = []
            for index in range(remain):
                post = await post_at(index)
                if not post:
                    break
                try:
                    results.append(
                        await client.like(
                            str(post.get("postId") or ""),
                            str(post.get("userId") or ""),
                        )
                    )
                except KuroError as exc:
                    ok = False
                    results.append(f"失败（{exc}）")
            lines.append(f"{remark}：{'、'.join(results) if results else '无可用帖子'}")
            continue

        if "分享" in remark:
            if not options.share_post:
                lines.append(f"{remark}：未开启，跳过")
                continue
            try:
                message = await client.share()
                lines.append(f"{remark}：{message}")
            except KuroError as exc:
                ok = False
                lines.append(f"{remark}：失败 - {exc}")
            continue

        lines.append(f"{remark}：暂无对应动作，已跳过")

    return lines, ok


async def sign_account(
    client: KuroClient,
    games: Sequence[str],
    *,
    bbs_tasks: bool = False,
    options: BbsOptions | None = None,
) -> SignReport:
    """跑完一个账号的全部签到。

    Args:
        client: 已绑定 Token 的客户端。
        games: 要签到的游戏 ID 列表。
        bbs_tasks: 是否执行库街区社区任务。
        options: 社区任务细分开关。

    Returns:
        签到结果汇总。
    """
    report = SignReport()
    options = options or BbsOptions()

    for game_id in games:
        try:
            roles = await client.get_role_list(game_id)
        except KuroAuthError:
            report.ok = False
            report.auth_error = True
            report.error = "Token 已失效"
            report.lines.append("Token 已失效或过期，请重新绑定")
            return report
        except KuroError as exc:
            report.ok = False
            report.lines.append(f"{game_name(game_id)}：获取角色失败 - {exc}")
            continue

        if not roles:
            report.lines.append(f"{game_name(game_id)}：未查到角色")
            continue

        for role in roles:
            try:
                text, success = await _sign_role(client, role)
            except KuroAuthError:
                report.ok = False
                report.auth_error = True
                report.error = "Token 已失效"
                report.lines.append("Token 已失效或过期，请重新绑定")
                return report
            except KuroError as exc:
                label = f"{game_name(game_id)}·{role.get('serverName') or '默认服'}"
                text, success = f"{label}：签到失败 - {exc}", False
            report.lines.append(text)
            if not success:
                report.ok = False

    if bbs_tasks:
        try:
            lines, ok = await run_bbs_tasks(client, options)
        except KuroAuthError:
            report.ok = False
            report.auth_error = True
            report.error = "Token 已失效"
            report.lines.append("Token 已失效或过期，请重新绑定")
            return report
        except KuroError as exc:
            report.ok = False
            lines, ok = [f"社区任务：失败 - {exc}"], False
        report.lines.extend(lines)
        if not ok:
            report.ok = False

    if not report.lines:
        report.lines.append("没有需要签到的内容，请检查配置的签到游戏")
        report.ok = False

    return report
