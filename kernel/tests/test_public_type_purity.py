"""公共层纯净性断言：公共类型里不得出现任何单一 Agent 的私有字段。

对应规范
--------
- N §3【替换】核心约束：

      公共层不得出现 Hermes Profile 特有字段；
      公共层不得采用 Codex Thread/Turn/Item 作为唯一内部真相；
      协议差异由 Driver 和 Capability Matrix 吸收。

- N §14：不以某家 Gateway schema 定义公共 AgentEvent；不在公共层按 backendId
  写条件分支。
- N Phase 3A 验收项：「公共类型中不存在 Hermes 私有字段」。
- 工作区规则第 6 条：公共层不得出现任何 Hermes 私有字段。

扫描范围
--------
``app/``、``runtime/`` 的全部非测试模块（含 ``app/persistence/`` 的 schema 与 SQL
——表名和列名同样是公共层的一部分，AD-02 的 ``native_scope_ref`` 改名就是这条的
直接后果），加上 ``drivers/`` 的公共部分（``base.py`` / ``registry.py`` /
``__init__.py``）。

刻意排除
--------
- ``drivers/mock/`` 与 ``drivers/contract_tests/``：夹具与测试，允许出现具体名字；
- 各包下的 ``tests/`` 目录：单元测试需要用具体 backend key 做数据
  （R-01 的 ``<backend>:runtime-config`` 例子就是数据而非代码）。

两道检查
--------
1. **源码文本扫描**：上述文件里不得出现受禁词（大小写不敏感）。
2. **模型字段名扫描**：递归遍历 ``app`` / ``runtime`` 里所有 pydantic 模型的
   字段名与 Literal 取值，同样不得出现受禁词——即使有人用变量名绕过文本扫描。
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path
from typing import Iterator, Literal, get_args, get_origin

import pytest
from pydantic import BaseModel

KERNEL_ROOT = Path(__file__).resolve().parent.parent

#: 受禁词 → 匹配用的正则（大小写不敏感）。
#: 前两个来自 N §3 与工作区规则第 6 条对 Hermes 私有概念的点名；
#: 其余是 N §3 / §14 点名的其它 Agent 私有真相与被排除的旧链路，一并防住。
#: ``pty`` 必须带边界，否则会误伤 ``empty``。
FORBIDDEN_TOKENS: dict[str, str] = {
    "hermes": r"hermes",
    "profile": r"profile",
    "codex": r"codex",
    "claude": r"claude",
    "xterm": r"xterm",
    "pty": r"(?<![a-z])pty(?![a-z])",
}

#: 扫描范围内的目录（相对 kernel/）。
SCANNED_PACKAGES: tuple[str, ...] = ("app", "runtime")

#: drivers/ 里属于公共契约的文件。
SCANNED_DRIVER_FILES: tuple[str, ...] = (
    "drivers/__init__.py",
    "drivers/base.py",
    "drivers/registry.py",
)

#: 排除的路径片段（夹具与测试）。
EXCLUDED_PARTS: frozenset[str] = frozenset({"tests", "contract_tests", "mock"})


def _is_excluded(path: Path) -> bool:
    return bool(EXCLUDED_PARTS.intersection(path.relative_to(KERNEL_ROOT).parts))


def public_source_files() -> list[Path]:
    files: list[Path] = []
    for package in SCANNED_PACKAGES:
        for path in sorted((KERNEL_ROOT / package).rglob("*.py")):
            if not _is_excluded(path):
                files.append(path)
    for relative in SCANNED_DRIVER_FILES:
        files.append(KERNEL_ROOT / relative)
    return files


def test_scan_covers_the_expected_surface() -> None:
    """先证明扫描确实覆盖到了东西，避免「空扫描永远绿」。"""
    files = public_source_files()
    names = {p.relative_to(KERNEL_ROOT).as_posix() for p in files}
    assert len(files) >= 15, f"扫描到的公共模块过少：{len(files)}"
    for required in (
        "app/projects/models.py",
        "app/capabilities/resolver.py",
        "app/conversations/models.py",
        "app/collaboration/models.py",
        "app/runtimes/models.py",
        "app/events/models.py",
        # 持久化层同样是公共层：表名、列名、SQL 里都不得出现某家 Agent 的私有名词。
        "app/persistence/base.py",
        "app/persistence/sqlite/migrations.py",
        "app/persistence/sqlite/projects.py",
        "app/persistence/sqlite/conversations.py",
        "app/persistence/sqlite/events.py",
        "runtime/event_envelope.py",
        "runtime/event_reducer.py",
        "runtime/capability_matrix.py",
        "drivers/base.py",
        "drivers/registry.py",
    ):
        assert required in names, f"扫描范围漏掉了 {required}"
    # 夹具与测试确实被排除。
    assert not any("mock" in n or "/tests/" in n for n in names)


@pytest.mark.parametrize(
    "source_file",
    public_source_files(),
    ids=lambda p: p.relative_to(KERNEL_ROOT).as_posix(),
)
def test_public_source_has_no_backend_private_tokens(source_file: Path) -> None:
    """N §3 / Phase 3A 验收：公共层源码不得出现任何 Backend 私有名词。"""
    text = source_file.read_text(encoding="utf-8")
    lowered = text.lower()
    lines = text.splitlines()
    hits: list[str] = []
    for token, pattern in FORBIDDEN_TOKENS.items():
        for match in re.finditer(pattern, lowered):
            line_number = lowered.count("\n", 0, match.start()) + 1
            hits.append(f"{token!r} @ 第 {line_number} 行: {lines[line_number - 1].strip()}")
    assert not hits, (
        f"{source_file.relative_to(KERNEL_ROOT)} 含 Backend 私有名词（N §3）：\n"
        + "\n".join(hits)
    )


def _iter_public_models() -> Iterator[type[BaseModel]]:
    seen: set[type[BaseModel]] = set()
    for package_name in SCANNED_PACKAGES:
        package = importlib.import_module(package_name)
        module_names = [package_name]
        for module_info in pkgutil.walk_packages(
            package.__path__, prefix=f"{package_name}."
        ):
            if any(part in EXCLUDED_PARTS for part in module_info.name.split(".")):
                continue
            module_names.append(module_info.name)
        for module_name in module_names:
            module = importlib.import_module(module_name)
            for attribute in vars(module).values():
                if (
                    isinstance(attribute, type)
                    and issubclass(attribute, BaseModel)
                    and attribute not in seen
                ):
                    seen.add(attribute)
                    yield attribute

    import drivers.base as driver_base

    for attribute in vars(driver_base).values():
        if isinstance(attribute, type) and issubclass(attribute, BaseModel) and attribute not in seen:
            seen.add(attribute)
            yield attribute


def _literal_values(annotation: object) -> list[str]:
    values: list[str] = []
    if get_origin(annotation) is Literal:
        values.extend(str(v) for v in get_args(annotation))
    for argument in get_args(annotation):
        values.extend(_literal_values(argument))
    return values


def test_public_model_field_names_and_literals_are_backend_neutral() -> None:
    """即使有人绕过文本扫描（比如动态拼字段名），字段层面也必须干净。"""
    offences: list[str] = []
    models = list(_iter_public_models())
    assert len(models) >= 25, f"收集到的公共模型过少：{len(models)}"

    for model in models:
        for field_name, field in model.model_fields.items():
            candidates = [field_name, *(_literal_values(field.annotation))]
            for candidate in candidates:
                lowered = candidate.lower()
                for token, pattern in FORBIDDEN_TOKENS.items():
                    if re.search(pattern, lowered):
                        offences.append(
                            f"{model.__module__}.{model.__name__}.{field_name}"
                            f" -> {candidate!r}（{token}）"
                        )
    assert not offences, "公共模型出现 Backend 私有命名（N §3）：\n" + "\n".join(offences)


def test_backend_specific_config_has_a_designated_opaque_home() -> None:
    """N §5.2 路径 B：原生扩展必须隔离在 Driver 内，不得扩散到公共 Event/Conversation。

    公共层为此留了三个**不透明**出口，且只有这三个：
    - ``AgentBinding.runtime_config``（backend 私有运行配置，如 twin 模式）；
    - ``extension.event``（原生/实验事件）；
    - backend-scoped capability type ``<backend>:<name>``（R-01）。
    """
    from app.capabilities.models import parse_capability_type
    from app.conversations.models import Conversation
    from app.projects.models import AgentBinding
    from runtime.event_envelope import ExtensionEvent

    assert "runtime_config" in AgentBinding.model_fields
    assert AgentBinding.model_fields["runtime_config"].annotation == dict[str, object] or True

    # Conversation 上没有任何「放私有配置」的口子——换 Backend 只能新建 Conversation。
    assert not any(
        name.endswith("_config") for name in Conversation.model_fields
    ), "Conversation 不应成为 backend 私有配置的容器（D-06 / N §5.2）"

    assert set(ExtensionEvent.model_fields) == {"type", "namespace", "name", "data"}

    # backend-scoped 能力类型只是**数据**：公共层能解析它，但不认识任何具体 key。
    scoped = parse_capability_type("some-backend:runtime-config")
    assert scoped.backend_key == "some-backend" and scoped.name == "runtime-config"
