"""库街区（kurobbs）HTTP 接口客户端。

接口与请求参数移植自 Night-stars-1/Kuro-Sign 的 ``utils/``（MIT 协议），
改造点：

* ``requests`` 同步调用 → ``httpx`` 异步调用，避免阻塞 AstrBot 事件循环；
* 返回值直接取字段 → 统一的错误分类（鉴权失效 / 风控 / 业务拒绝），
  让上层能区分「该让用户重新绑定」和「稍后重试就好」；
* 增加退避重试与请求间隔。库街区风控时返回的是一段 HTTP 200 的非 JSON
  内容，只看状态码会误判成「接口废弃」，所以这里把「非 JSON」也当作可重试。
"""

from __future__ import annotations

import asyncio
import random
import string
import time
from typing import Any

import httpx
from astrbot.api import logger

API_BASE = "https://api.kurobbs.com"
APP_VERSION = "2.2.5"
APP_VERSION_CODE = "2250"
DEFAULT_SOURCE = "android"
DEFAULT_TIMEOUT = 15.0
MAX_RETRY = 3
# 退避基数与请求间隔是模块级常量，自测里会置 0 以免用例变慢。
RETRY_BACKOFF = 1.0
REQUEST_GAP = 0.6

VALID_SOURCES = ("android", "h5")

_AUTH_HINTS = (
    "未登录",
    "登录已",
    "登录失效",
    "登录过期",
    "重新登录",
    "token",
    "授权",
    "凭证",
    "无效",
)
_RISK_HINTS = ("频繁", "风控", "稍后", "限流", "繁忙", "busy", "too many", "系统错误")
_DONE_HINTS = ("已签到", "已经签到", "重复签到", "今日已", "已完成")


class KuroError(Exception):
    """库街区接口调用失败的基类。"""


class KuroAuthError(KuroError):
    """Token 失效或无权访问，需要用户重新绑定。"""


class KuroRiskError(KuroError):
    """疑似被限流 / 风控，退避后可重试。"""


class KuroBusinessError(KuroError):
    """接口明确拒绝了这次操作（如活动未开始），重试无用。"""


def new_dev_code(length: int = 40) -> str:
    """生成库街区要求的 devCode。

    Args:
        length: 随机串长度，库街区固定为 40。

    Returns:
        由大小写字母组成的随机字符串。
    """
    return "".join(random.choice(string.ascii_letters) for _ in range(length))


def current_month() -> str:
    """返回当前月份的两位数字串，签到请求需要它。"""
    return time.strftime("%m")


def is_already_done(text: Any) -> bool:
    """判断接口返回的提示是否表示「本次操作此前已完成」。

    Args:
        text: 接口 msg 字段。

    Returns:
        命中「已签到」一类措辞时为 ``True``。
    """
    message = str(text or "")
    return any(hint in message for hint in _DONE_HINTS)


def classify(code: Any, msg: str) -> KuroError:
    """把接口返回的 code / msg 归类成具体的异常。

    Args:
        code: 接口 code 字段。
        msg: 接口 msg 字段。

    Returns:
        已归类的异常实例，由调用方 raise。
    """
    text = str(msg or "").strip()
    lowered = text.lower()
    if any(hint in text or hint in lowered for hint in _AUTH_HINTS):
        return KuroAuthError(text or f"鉴权失败（code={code}）")
    if any(hint in text or hint in lowered for hint in _RISK_HINTS):
        return KuroRiskError(text or f"请求被限流（code={code}）")
    return KuroBusinessError(text or f"接口返回异常（code={code}）")


def pick_field(data: dict[str, Any], *names: str) -> str:
    """从接口返回里按候选字段名取第一个非空值。

    库街区不同接口对同一个东西的字段名不统一（``token`` / ``accessToken``），
    登录返回又是我们唯一没法用真账号验证的地方，所以这里按候选名依次尝试。

    Args:
        data: 接口返回的字典。
        *names: 候选字段名，按优先级排列。

    Returns:
        取到的字符串值；都没有时返回空串。
    """
    for name in names:
        value = data.get(name)
        if value:
            return str(value)
    return ""


class KuroClient:
    """绑定单个 Token 的库街区客户端。

    一个实例对应一个账号：headers 里的 token 是构造时固定的，
    因此多账号签到要分别建实例，别复用。
    """

    def __init__(
        self,
        token: str,
        source: str = DEFAULT_SOURCE,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """初始化客户端。

        Args:
            token: 库街区 Token。
            source: Token 来源，``android`` 或 ``h5``。
            timeout: 单次请求超时秒数。
        """
        self.token = (token or "").strip()
        self.source = source if source in VALID_SOURCES else DEFAULT_SOURCE
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.dev_code = new_dev_code()
        self._client: httpx.AsyncClient | None = None

    @property
    def headers(self) -> dict[str, str]:
        """返回当前生效的请求头。"""
        return {
            "User-Agent": "okhttp/3.11.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "devCode": self.dev_code,
            "source": self.source,
            "version": APP_VERSION,
            "versionCode": APP_VERSION_CODE,
            "token": self.token,
        }

    async def _http(self) -> httpx.AsyncClient:
        """惰性创建并复用底层连接池。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        """关闭连接池，关闭失败不影响其它流程。"""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:  # noqa: BLE001 - 关闭失败无需向上报
                logger.debug("关闭 HTTP 连接池失败：%s", exc)
            self._client = None

    async def _post_once(self, url: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """发起一次 POST 并校验返回结构。

        Args:
            url: 完整地址。
            data: 表单参数。

        Returns:
            接口返回的完整 JSON 字典。

        Raises:
            KuroAuthError: Token 失效。
            KuroRiskError: 非 JSON 响应或命中风控措辞。
            KuroBusinessError: 接口以其它 code 拒绝。
        """
        client = await self._http()
        response = await client.post(url, data=data or {}, headers=self.headers)
        try:
            payload = response.json()
        except ValueError as exc:
            # 风控页是 HTTP 200 的 HTML，只按状态码判断会把它当成「接口废弃」。
            raise KuroRiskError(
                f"接口返回的不是 JSON（HTTP {response.status_code}），多半被风控挡了"
            ) from exc
        if not isinstance(payload, dict):
            raise KuroBusinessError("接口返回结构异常")
        code = payload.get("code")
        if code == 200:
            return payload
        msg = str(payload.get("msg") or payload.get("message") or "").strip()
        raise classify(code, msg)

    async def post(self, path: str, data: dict[str, Any] | None = None) -> dict:
        """带退避重试的 POST。

        鉴权失效与业务拒绝**不重试**（重试也不会成功），
        风控 / 网络错误才退避重试。

        Args:
            path: 以 ``/`` 开头的接口路径，或完整 URL。
            data: 表单参数。

        Returns:
            接口返回的完整 JSON 字典。
        """
        url = path if path.startswith("http") else API_BASE + path
        last: KuroError | None = None
        for attempt in range(1, MAX_RETRY + 1):
            if REQUEST_GAP:
                await asyncio.sleep(REQUEST_GAP)
            try:
                return await self._post_once(url, data)
            except (KuroAuthError, KuroBusinessError):
                raise
            except KuroError as exc:
                last = exc
            except (httpx.HTTPError, OSError) as exc:
                last = KuroError(f"网络错误：{exc}")
            if attempt < MAX_RETRY and RETRY_BACKOFF:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
        raise last or KuroError("请求失败")

    # ------------------------------------------------------------------ 账号

    async def get_role_list(self, game_id: str | int) -> list[dict[str, Any]]:
        """查询某个游戏下已绑定的角色列表。

        Args:
            game_id: 游戏 ID，战双 ``2`` / 鸣潮 ``3``。

        Returns:
            角色字典列表，无角色时为空列表。
        """
        payload = await self.post("/gamer/role/list", {"gameId": str(game_id)})
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def verify_token(self) -> bool:
        """校验 Token 是否仍可用。

        上层用它做绑定前的预校验：调用任何一个需要鉴权的接口，
        Token 失效时 :meth:`get_role_list` 会抛 :class:`KuroAuthError`。

        Returns:
            Token 有效时为 ``True``。
        """
        await self.get_role_list("2")
        return True

    async def send_sms_code(self, mobile: str) -> str:
        """请求库街区向该手机号发送登录验证码。

        注意：库街区网页登录页加载了极验（Geetest）风控，这个接口**可能**要求
        额外的验证参数而失败；失败时上层会引导用户去登录页自己取码，所以这里
        只把接口原话带回去，不做重试。

        Args:
            mobile: 手机号。

        Returns:
            接口返回的提示文本。
        """
        payload = await self.post("/user/getSmsCode", {"mobile": mobile})
        return str(payload.get("msg") or "验证码已发送")

    async def login_with_sms(self, mobile: str, code: str) -> dict[str, Any]:
        """用手机号 + 短信验证码换取 Token。

        这个接口**不需要**已登录的 Token，所以用空 Token 的客户端调用即可。

        Args:
            mobile: 手机号。
            code: 短信验证码。

        Returns:
            归一化字典，键为 ``token`` / ``user_id`` / ``raw``。

        Raises:
            KuroBusinessError: 返回里没有 token（验证码错、风控等）。
        """
        payload = await self.post("/user/sdkLogin", {"mobile": mobile, "code": code})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise KuroBusinessError("登录接口没有返回账号数据")
        token = pick_field(data, "token", "accessToken", "access_token") or pick_field(
            payload, "token", "accessToken", "access_token"
        )
        if not token:
            raise KuroBusinessError("登录接口没有返回 token")
        return {
            "token": token,
            "user_id": pick_field(data, "userId", "user_id", "uid"),
            "raw": data,
        }

    # ------------------------------------------------------------ 游戏内签到

    async def sign_game(
        self,
        game_id: str | int,
        server_id: str,
        role_id: str,
        month: str | None = None,
    ) -> str:
        """执行一次游戏内签到。

        Args:
            game_id: 游戏 ID。
            server_id: 区服 ID。
            role_id: 角色 ID（库街区要求 userId 传 roleId）。
            month: 两位月份，缺省取当前月。

        Returns:
            接口返回的提示文本。
        """
        payload = await self.post(
            "/encourage/signIn/v2",
            {
                "gameId": str(game_id),
                "serverId": str(server_id),
                "roleId": str(role_id),
                "userId": str(role_id),
                "reqMonth": month or current_month(),
            },
        )
        return str(payload.get("msg") or "签到成功")

    async def query_prize(self, game_id: str | int, server_id: str, role_id: str) -> str:
        """查询本次签到拿到的奖品名。

        Args:
            game_id: 游戏 ID。
            server_id: 区服 ID。
            role_id: 角色 ID。

        Returns:
            奖品名；查询失败时返回空串（不影响签到结论）。
        """
        try:
            payload = await self.post(
                "/encourage/signIn/queryRecordV2",
                {
                    "gameId": str(game_id),
                    "serverId": str(server_id),
                    "roleId": str(role_id),
                    "userId": str(role_id),
                },
            )
        except KuroError:
            return ""
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return str(data[0].get("goodsName") or "")
        return ""

    # ---------------------------------------------------------- 库街区社区

    async def bbs_sign(self, game_id: str | int = "2") -> str:
        """库街区用户签到（社区签到任务）。

        Args:
            game_id: 原脚本固定传 ``2``。

        Returns:
            接口返回的提示文本。
        """
        payload = await self.post("/user/signIn", {"gameId": str(game_id)})
        return str(payload.get("msg") or "签到成功")

    async def get_task_list(self) -> list[dict[str, Any]]:
        """查询社区每日任务进度。"""
        payload = await self.post("/encourage/level/getTaskProcess", {"gameId": 0})
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        tasks = data.get("dailyTask")
        if not isinstance(tasks, list):
            return []
        return [item for item in tasks if isinstance(item, dict)]

    async def get_forum_list(self) -> list[dict[str, Any]]:
        """拉取一批帖子，用于完成浏览 / 点赞任务。"""
        payload = await self.post(
            "/forum/list",
            {
                "forumId": "9",
                "gameId": "3",
                "pageIndex": "1",
                "pageSize": "20",
                "searchType": "3",
                "timeType": "0",
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        posts = data.get("postList")
        if not isinstance(posts, list):
            return []
        return [item for item in posts if isinstance(item, dict)]

    async def get_forum_detail(self, post_id: str) -> str:
        """读取帖子详情，用于完成「浏览帖子」任务。

        Args:
            post_id: 帖子 ID。

        Returns:
            帖子标题。
        """
        payload = await self.post(
            "/forum/getPostDetail",
            {"isOnlyPublisher": 0, "postId": str(post_id), "showOrderTyper": 2},
        )
        data = payload.get("data")
        detail = data.get("postDetail") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            return str(detail.get("postTitle") or "（无标题）")
        return "（无标题）"

    async def like(self, post_id: str, user_id: str) -> str:
        """点赞帖子。

        Args:
            post_id: 帖子 ID。
            user_id: 帖子作者的 userId。

        Returns:
            结果文本。
        """
        try:
            payload = await self.post(
                "/forum/like",
                {
                    "forumId": 11,
                    "gameId": 3,
                    "likeType": 1,
                    "operateType": 1,
                    "postCommentId": "",
                    "postCommentReplyId": "",
                    "postId": str(post_id),
                    "postType": 1,
                    "toUserId": str(user_id),
                },
            )
        except KuroBusinessError as exc:
            return str(exc)
        return str(payload.get("msg") or "点赞成功")

    async def share(self) -> str:
        """完成「分享帖子」任务。"""
        try:
            payload = await self.post("/encourage/level/shareTask", {"gameId": 3})
        except KuroBusinessError as exc:
            return str(exc)
        return str(payload.get("msg") or "分享成功")
