from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class Principal(BaseModel):
    user: Any
    token: str


class NewConversation(BaseModel):
    pageContext: dict[str, Any] | None = None


class MessageIn(BaseModel):
    kind: Literal["message"]
    turnId: str = Field(min_length=3, max_length=100)
    text: str = Field(min_length=1, max_length=4000)


class ConfirmIn(BaseModel):
    kind: Literal["confirm"]
    turnId: str = Field(min_length=3, max_length=100)
    actionId: str
    decision: str = Field(pattern="^(approve|reject)$")


# One endpoint takes every turn; `kind` says which.
TurnIn = Annotated[MessageIn | ConfirmIn, Field(discriminator="kind")]
