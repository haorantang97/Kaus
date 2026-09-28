"""Persist a selected policy; the driver validates it against this session's catalog."""
from typing import Annotated

from pydantic import Field

# Native choices retain their exact id instead of being forced into a broader
# policy. They are accepted only when the runtime advertises that choice.
ConversationApprovalMode = Annotated[
    str, Field(pattern=r"^(ask|auto|bypass|read_only|plan|host:ask|host:auto|native:[^\r\n]{1,200})$")
]
