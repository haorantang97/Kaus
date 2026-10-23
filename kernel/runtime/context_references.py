"""Exact projection of host-added context references back to visible user text."""
from hashlib import sha256


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def projection(visible, dispatched):
    return {"sha256": digest(dispatched), "visibleSha256": digest(visible), "length": len(visible)}


def normalize_history(history, events):
    projections = {}
    for row in events:
        event = row.envelope.event
        if event.type == "extension.event" and event.namespace == "kaus" and event.name == "user.message":
            value = event.data.get("nativeTextProjection")
            if isinstance(value, dict) and isinstance(value.get("length"), int) and value["length"] >= 0:
                projections[value.get("sha256")] = value
    if not projections:
        return history
    entries = []
    for entry in history.entries:
        value = projections.get(digest(entry.text)) if entry.kind == "message" and entry.role == "user" and entry.text else None
        if value and digest(entry.text[:value["length"]]) == value.get("visibleSha256"):
            entry = entry.model_copy(update={"text": entry.text[:value["length"]]})
        entries.append(entry)
    return history.model_copy(update={"entries": tuple(entries)})
