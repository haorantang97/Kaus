"""Origin / CSWSH 白名单（公共层的纯函数，D-17 / AD-66）。

为什么单独一个模块
------------------
宿主里已经有一份等价规则（旧 WebSocket 路由的 CSWSH 防护）。会话类端点要「口径
与它一致或更严」，最稳妥的做法不是再手写一遍，而是把规则本身提出来成为**一处
定义**：本模块只做字符串判断，不认识 HTTP 框架、不认识任何 Backend、不读配置。
宿主那份暂时保持原样（等接口层统一收敛时再指向这里），因此本模块是「第二处
实现、同一份口径」——两边的取值表写在同一个常量清单里，改口径时只改这里。

口径（与宿主 WebSocket 那份逐条对齐）
--------------------------------------
1. **没有 ``Origin`` 头 → 放行。** 浏览器发跨站请求时一定带 ``Origin``；不带的是
   本机的非浏览器客户端（curl / 脚本 / 测试），它们没法被受害者的浏览器当跳板。
   放行**只是放行 Origin 这一关**——token 那关照样要过（见 :mod:`app.api.session_auth`）。
2. **带 ``Origin`` → 必须落在白名单里**：请求自身的 ``Host``（http/https 两种
   scheme），加上本机开发地址 ``localhost`` / ``127.0.0.1`` 的两个端口。
3. 白名单是**精确串比较**，不做前缀匹配、不做通配。``http://localhost:8877.evil``
   之类的东西只有靠精确比较才挡得住。

刻意不做的事
------------
- 不解析 URL（``urlsplit`` 会把 ``http://localhost:8877/`` 这种带尾斜杠的串
  归一成合法值，反而放宽了口径）；
- 不接受 ``null`` Origin（沙箱 iframe / 某些重定向会发它，不能当同源）；
- 不看 ``Referer``（可被剥掉，不是安全边界）。
"""

from __future__ import annotations

from typing import Iterable

#: 本机开发时会出现的两个端口：后端自身与前端 dev server。
DEFAULT_LOCAL_PORTS: tuple[int, ...] = (8877, 5174)

#: 指向本机的两个主机名。刻意不含 ``0.0.0.0`` / ``[::1]``——服务只绑 127.0.0.1。
DEFAULT_LOCAL_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1")

#: ``Sec-Fetch-Site`` 中允许换取 token 的取值。``none`` = 用户直接在地址栏打开。
SAME_SITE_FETCH_VALUES: frozenset[str] = frozenset({"same-origin", "none"})


def parse_local_ports(raw: object) -> tuple[int, ...]:
    """把一份「本机端口」配置解析成端口元组。**不合法一律 ``ValueError``。**

    两种写法都收：逗号分隔的串（``"8877,5174,5175"``，环境变量只能是串）与
    数字列表（JSON 配置里更自然）。本函数是纯的：不读环境变量、不读文件、
    不打日志、不吞异常——「取哪一份配置、坏了怎么办」是接入层的判断，
    放在这里就等于让白名单模块认识配置来源（见模块开头「刻意不做的事」）。

    拒绝的东西：空集合（把白名单收窄成只剩 Host 自己不像是有人想要的结果，
    更像是配置写错了）、非整数、以及 1..65535 之外的值。
    """
    if isinstance(raw, str):
        items: list[object] = [chunk for chunk in raw.split(",") if chunk.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ValueError(f"端口配置只接受逗号分隔的串或列表，收到 {type(raw).__name__}")
    ports: list[int] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise ValueError(f"端口不是整数：{item!r}")
        try:
            port = int(str(item).strip())
        except ValueError as exc:
            raise ValueError(f"端口不是整数：{item!r}") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"端口越界：{port}")
        if port not in ports:
            ports.append(port)
    if not ports:
        raise ValueError("端口配置是空的")
    return tuple(ports)


def allowed_origins(
    host: str | None,
    *,
    local_hosts: Iterable[str] = DEFAULT_LOCAL_HOSTS,
    local_ports: Iterable[int] = DEFAULT_LOCAL_PORTS,
) -> frozenset[str]:
    """白名单全集。``host`` 是请求自己的 ``Host`` 头（可能带端口）。"""
    origins: set[str] = set()
    cleaned = (host or "").strip()
    if cleaned:
        origins.add(f"http://{cleaned}")
        origins.add(f"https://{cleaned}")
    for name in local_hosts:
        for port in local_ports:
            origins.add(f"http://{name}:{port}")
    return frozenset(origins)


def origin_allowed(
    origin: str | None,
    *,
    host: str | None,
    local_hosts: Iterable[str] = DEFAULT_LOCAL_HOSTS,
    local_ports: Iterable[int] = DEFAULT_LOCAL_PORTS,
) -> bool:
    """带 ``Origin`` 时是否放行。**不带 Origin 一律 True**（见模块文档第 1 条）。"""
    if not origin:
        return True
    return origin in allowed_origins(
        host, local_hosts=local_hosts, local_ports=local_ports
    )


def is_same_site_fetch(sec_fetch_site: str | None) -> bool:
    """``Sec-Fetch-Site`` 是否表示「同源发起」。

    头不存在 → ``True``：非浏览器客户端不发这个头，而它们本来就不在 CSWSH 的
    威胁模型里。存在时必须是 ``same-origin`` / ``none``——浏览器自己填的这一栏
    页面脚本改不了，所以它比 ``Origin`` 更难伪造，用作第二道闸。
    """
    if sec_fetch_site is None:
        return True
    return sec_fetch_site.strip().lower() in SAME_SITE_FETCH_VALUES


__all__ = [
    "DEFAULT_LOCAL_HOSTS",
    "DEFAULT_LOCAL_PORTS",
    "SAME_SITE_FETCH_VALUES",
    "allowed_origins",
    "is_same_site_fetch",
    "parse_local_ports",
    "origin_allowed",
]
