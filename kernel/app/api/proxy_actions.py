"""Actions a main agent performs for the user through the Kaus control MCP.

The control MCP marks such requests with ``proxy="main_agent"``. Kaus cannot
verify that the user agreed (the main agent asks in its own chat); what it does
guarantee is that a proxied answer only picks what the card offers, and that
every proxied action leaves a ``kaus/proxy.action`` row in the conversation's
timeline, replayed like any other event.
"""

from __future__ import annotations

from typing import Any, Literal

from drivers.base import InteractionResponse
from runtime.event_envelope import ExtensionEvent
from runtime.event_reducer import InteractionItem, TimelineState

ProxyActor = Literal["main_agent"]

PROXY_NAMESPACE = "kaus"
PROXY_NAME = "proxy.action"
SUBJECT_LIMIT = 80

#: Option ids/kinds read as a refusal (the same vocabulary as the permission card).
_DENIED = frozenset({"deny", "denied", "reject", "rejected", "no", "cancel"})

_SETTING_TEXT = {
    "model": "主 Agent 已将模型改为：{}",
    "reasoning": "主 Agent 已将推理强度改为：{}",
    "approval": "主 Agent 已将权限改为：{}",
}
_ACTION_TEXT = {
    "approved": "主 Agent 已代为批准：{}",
    "denied": "主 Agent 已拒绝：{}",
    "answered": "主 Agent 已代为回答：{}",
    "cancelled": "主 Agent 已取消：{}",
}


def _short(value: str | None) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= SUBJECT_LIMIT else text[: SUBJECT_LIMIT - 1] + "…"


def pending_item(state: TimelineState | None, interaction_id: str) -> InteractionItem | None:
    if state is None:
        return None
    key = f"interaction:{interaction_id}"
    return next((item for item in state.items if isinstance(item, InteractionItem)
                 and item.item_id == key and item.status == "pending"), None)


def offered_choice(item: InteractionItem, response: InteractionResponse) -> tuple[str, str | None]:
    """Return ``(action, choice label)``; ``ValueError`` when the card does not offer it.

    The rules match what the conversation card renders: a permission or
    authentication card offers its buttons only; a question offers its options,
    multi-select when ``allowMultiple``, free text when ``allowFreeText``, and Cancel.
    """
    if response.kind != item.interaction_kind:
        raise ValueError(f"this item is a {item.interaction_kind} request")
    if item.interaction_kind == "permission":
        options = item.permission.options if item.permission else ()
        option = next((o for o in options if o.option_id == response.option_id), None)
        if option is None or response.cancelled or response.option_ids or response.text:
            raise ValueError("choose one of the offered options")
        kind = (option.kind or "").lower()
        denied = kind.startswith("reject") or kind in _DENIED or option.option_id.lower() in _DENIED
        return ("denied" if denied else "approved"), option.label
    if item.interaction_kind == "authentication":
        methods = item.authentication.methods if item.authentication else ()
        if response.option_id not in methods or response.cancelled or response.option_ids or response.text:
            raise ValueError("choose one of the offered methods")
        return "approved", response.option_id
    question = item.question
    if question is None:
        raise ValueError("question details are unavailable")
    labels = {o.option_id: o.label for o in question.options}
    if response.cancelled:
        if response.option_id or response.option_ids or response.text:
            raise ValueError("cancel takes no other answer")
        return "cancelled", None
    given = sum(bool(x) for x in (response.option_id, response.option_ids, response.text))
    if given != 1:
        raise ValueError("give exactly one of option, options or text")
    if response.option_id:
        if response.option_id not in labels:
            raise ValueError("choose one of the offered options")
        return "answered", labels[response.option_id]
    if response.option_ids:
        if not question.allow_multiple or any(o not in labels for o in response.option_ids):
            raise ValueError("this question does not take these options")
        return "answered", "、".join(labels[o] for o in response.option_ids)
    if not question.allow_free_text:
        raise ValueError("this question does not take free text")
    return "answered", _short(response.text)


def interaction_event(item: InteractionItem, response: InteractionResponse, *,
                      action: str, choice: str | None, actor: ProxyActor) -> ExtensionEvent:
    if item.interaction_kind == "permission":
        subject = item.permission.title if item.permission else ""
    elif item.interaction_kind == "authentication":
        subject = item.authentication.message if item.authentication else ""
    else:
        subject = item.question.prompt if item.question else ""
    subject = _short(subject)
    data: dict[str, Any] = {
        "proxy": actor, "action": action, "subject": subject,
        "interactionId": item.item_id.split(":", 1)[-1], "interactionKind": item.interaction_kind,
        "text": _ACTION_TEXT[action].format(subject),
    }
    if choice:
        data["choice"] = choice
    return ExtensionEvent(namespace=PROXY_NAMESPACE, name=PROXY_NAME, data=data)


def setting_event(setting: Literal["model", "reasoning", "approval"], value: str, label: str | None,
                  *, actor: ProxyActor) -> ExtensionEvent:
    shown = label or value
    data: dict[str, Any] = {"proxy": actor, "action": "setting", "setting": setting, "value": value,
                            "text": _SETTING_TEXT[setting].format(shown)}
    if label and label != value:
        data["label"] = label
    return ExtensionEvent(namespace=PROXY_NAMESPACE, name=PROXY_NAME, data=data)


__all__ = ["PROXY_NAME", "PROXY_NAMESPACE", "ProxyActor", "interaction_event", "offered_choice",
           "pending_item", "setting_event"]
