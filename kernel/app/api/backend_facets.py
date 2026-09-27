"""一条 Backend 的「它是按哪个预设接的」（批次二十五第 5 件）。

为什么是延迟绑定而不是领域字段
------------------------------
``preset`` 与 ``quirks`` 是 **Driver 的装配事实**，不是领域事实：同一个
``backend:<key>`` 换一份 ``dashboard-config.json`` 就换了预设，而领域库里那一行
Backend 记的是「这种接入类型现在什么状态」。把它写进
:class:`~app.projects.models.Backend` 会开出第二本账——领域库里存着一个上次启动
时的预设名，配置里写着另一个，谁也说不清哪个是真的。

所以这里沿用 :mod:`app.api.binding_status` 立下的那条做法：只读领域路由装配时
手上没有 Driver Registry（它比会话运行时先起来），留一个进程级取数面，会话
运行时装好之后登记进来，路由**每次请求时**去问一次。没登记 = 这两个键整个不
出现（前端按缺字段静默不渲染，AD-71），而不是报错，也不是给一个 null。

只读、只出名字
--------------
返回的东西只有：预设 id、标签、登录模型、``env_keys_hint``（**只有变量名**，
AD-10）、``auth_method_ids``（**只有 id**，AD-156：给「先在终端登录：…」那句话用）、
以及怪癖表的七个布尔位。命令 argv 不上 wire——那是启动信息，属于
配置文件那一侧；``GET /api/backends`` 是给界面看「这条能力从哪来」的，不是给
界面复述一遍怎么拉起进程。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

#: 取数面：给一个 backend id，返回它的 ``{preset, quirks}``（或 ``None``）。
FacetProvider = Callable[[str], "Mapping[str, Any] | None"]

_PROVIDER: FacetProvider | None = None


def set_facet_provider(provider: FacetProvider) -> None:
    """登记取数面（装配期调用一次）。后登记的覆盖先登记的。"""
    global _PROVIDER
    _PROVIDER = provider


def clear_facet_provider() -> None:
    """摘掉取数面。测试用；生产上只有进程退出会走到这一步。"""
    global _PROVIDER
    _PROVIDER = None


def facet_provider() -> FacetProvider | None:
    return _PROVIDER


def backend_facets(backend_id: str) -> dict[str, Any]:
    """这条 Backend 的只读附加字段。没登记取数面、或这条不是预设接的 → ``{}``。"""
    provider = facet_provider()
    if provider is None:
        return {}
    try:
        facets = provider(backend_id)
    except Exception:  # noqa: BLE001 - 一个附加字段不该把整页换成 500
        return {}
    return dict(facets) if facets else {}


def facets_from_driver(driver: Any) -> dict[str, Any] | None:
    """一个 Driver → ``{preset, quirks}``。没有预设的 Driver 返回 ``None``。

    用 ``getattr`` 而不是 ``isinstance``：这一层是公共层，不 import 任何具体
    Driver（那正是纯净性扫描守的那条线）。「有没有 preset 这个属性」就是判据。
    """
    preset = getattr(driver, "preset", None)
    if preset is None:
        return None
    quirks = getattr(driver, "quirks", None)
    payload: dict[str, Any] = {"preset": preset.to_wire()}
    if quirks is not None:
        payload["quirks"] = quirks.to_wire()
    warnings = getattr(driver, "warnings", None)
    if warnings:
        payload["driverWarnings"] = list(warnings)
    return payload


__all__ = [
    "FacetProvider",
    "backend_facets",
    "clear_facet_provider",
    "facet_provider",
    "facets_from_driver",
    "set_facet_provider",
]
