"""Group coordinator configuration and validated, task-bound control protocol."""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.collaboration.room import RoomMember, resolve_display_names, resolve_everyone

COORDINATOR_ID = "coordinator"
CONFIG_KEY = "coordinator"
DEFAULTS_KEY = "group.coordinator.default"


class WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class CoordinatorConfig(WireModel):
    binding_id: str = Field(min_length=1)
    model_id: str | None = None
    provider_id: str | None = None
    reasoning_mode: str | None = None
    execution_mode: str | None = Field(default=None, max_length=256)
    approval_mode: Literal["ask", "auto", "bypass", "read_only", "plan"] | None = None
    workspace_root: str | None = None
    instructions: str = Field(default="", max_length=12000)
    call_cap: int = Field(default=40, ge=2, le=200)
    time_limit_minutes: int = Field(default=30, ge=1, le=240)
    revision: int = Field(default=1, ge=1)
    source_revision: int | None = None

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class Assignment(WireModel):
    member_id: str
    task: str = Field(min_length=1, max_length=12000)


class Control(WireModel):
    task_id: str
    nonce: str
    action: Literal["delegate", "finish", "need_user", "request_review", "answer_done", "read_evidence"]
    mode: Literal["independent", "collaborative"] = "independent"
    assignments: list[Assignment] = Field(default_factory=list, max_length=50)
    reason: str = Field(default="", max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    offset: int = Field(default=0, ge=0, le=10000000)


def extract_control(text: str, *, task_id: str, nonce: str, coordinator: bool,
                    allowed_members: set[str], evidence_ids: set[str]) -> tuple[str, Control | None]:
    """Only a complete final fenced control block may schedule work."""
    match = re.search(r"(?:\n|^)```kaus-control\s*\n(.*?)\n```\s*$", text, re.S)
    if match is None:
        if coordinator:
            raise ValueError("组长未返回完整的任务动作")
        return text.strip(), None
    body = text[:match.start()].strip()
    try:
        control = Control.model_validate(json.loads(match.group(1)))
    except (ValueError, TypeError) as exc:
        raise ValueError("任务动作格式无效") from exc
    if control.task_id != task_id or control.nonce != nonce:
        raise ValueError("任务动作不属于本次调用")
    if not coordinator and control.action not in {"answer_done", "request_review", "read_evidence"}:
        raise ValueError("成员没有调度其他成员的权限")
    targets = [a.member_id for a in control.assignments]
    if len(targets) != len(set(targets)) or not set(targets) <= allowed_members:
        raise ValueError("任务动作包含重复或范围外的成员")
    if control.action == "delegate" and (not targets or not control.reason.strip()):
        raise ValueError("继续协作需要具体任务和原因")
    if control.action != "delegate" and targets:
        raise ValueError("非分工动作不能包含执行名单")
    if control.action == "read_evidence" and not control.evidence_ids:
        raise ValueError("读取原文需要指定记录")
    if control.action == "request_review" and not control.reason.strip():
        raise ValueError("请求评议需要说明具体分歧或缺口")
    if not set(control.evidence_ids) <= evidence_ids:
        raise ValueError("任务动作引用了当前材料之外的记录")
    return body, control


def directed_targets(text: str, members: list[RoomMember]) -> tuple[str, ...] | None:
    """Recognize addresses, not every mention of a name. None asks the LLM."""
    names = resolve_display_names(members)
    active = [m for m in members if m.listed]
    if resolve_everyone(text):
        return tuple(m.member_id for m in active)
    clean = re.sub(r"```.*?```|[“「].*?[”」]", "", text, flags=re.S).strip()
    explicit = []
    for m in active:
        name = names[m.member_id]
        if re.search(r"(?<![\w@])@" + re.escape(name) + r"(?=$|[\s，,。.!！?？、:：])", clean):
            explicit.append(m.member_id)
    if explicit:
        # Negated/mixed instructions require semantic interpretation.
        addressing = re.sub(r"(?:组长|主持).{0,5}(?:不要|不用|无需|别).{0,3}(?:参与|总结|回答)|(?:不要|不用|无需|别让).{0,3}(?:组长|主持)", "", clean)
        if re.search(r"不要|别让|不用|而非|除了|不是让", addressing):
            return None
        return tuple(explicit)
    choices = sorted(((names[m.member_id], m.member_id) for m in active), key=lambda x: -len(x[0]))
    remaining = re.sub(r"^(?:请|麻烦|让)\s*", "", clean)
    targets = []
    while choices:
        found = next(((n, i) for n, i in choices if remaining.startswith(n)), None)
        if found is None:
            break
        name, mid = found
        remaining = remaining[len(name):]
        targets.append(mid)
        join = re.match(r"\s*(?:和|与|及|、|&|以及)\s*", remaining)
        if join:
            remaining = remaining[join.end():]
            continue
        if re.match(r"\s*(?:[，,:：]\s*)?(?:你|请|帮|来|解释|回答|分析|讨论|辩论|看看|说说|各自|分别)", remaining):
            return tuple(targets)
        return None
    return None


def collaboration_requested(text: str) -> bool:
    return bool(re.search(r"辩论|讨论|共同|联合|统一结论|形成共识|达成共识|综合.*(?:意见|观点)|debate|discuss|collaborat", text, re.I))


def coordinator_excluded(text: str) -> bool:
    return bool(re.search(r"(?:组长|主持).{0,5}(?:不要|不用|无需|别).{0,3}(?:参与|总结|回答)|(?:不要|不用|无需|别让).{0,3}(?:组长|主持)", text))


def public_body(text: str) -> str:
    """Control text is never a public answer, including broken/partial frames."""
    return re.split(r"(?:^|\n)```kaus-control", text, maxsplit=1)[0].strip()
