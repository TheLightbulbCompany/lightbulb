from typing import Literal

from pydantic import BaseModel, Field, field_validator

TEAM_SIZES = ("just_me", "2", "3-5", "6-15", "16+")
AGENTS = ("claude_code", "codex", "chatgpt", "cursor", "other")
# "How did you first hear about us?" — the list on the form, in its order.
SOURCES = (
    "hacker_news",
    "reddit",
    "x",
    "linkedin",
    "product_hunt",
    "shared_session",
    "plugin",
    "friend",
    "founder_email",
    "search",
    "ai_assistant",
    "other",
)


class SetupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    company: str = Field(min_length=1, max_length=120)
    team_size: Literal[TEAM_SIZES]  # type: ignore[valid-type]
    agents: list[Literal[AGENTS]] = Field(min_length=1, max_length=len(AGENTS))  # type: ignore[valid-type]
    agents_other: str = Field(default="", max_length=200)
    first_handoff: str = Field(min_length=1, max_length=2000)
    source: Literal[SOURCES]  # type: ignore[valid-type]
    source_other: str = Field(default="", max_length=200)
    thread_link: str = Field(default="", max_length=500)
    # Attribution the page read in the browser: first-touch UTM / referrer /
    # landing path, `?source=` on the page URL, and the PostHog anonymous id.
    attribution: dict[str, str] = Field(default_factory=dict, max_length=40)
    source_param: str = Field(default="", max_length=100)
    anonymous_id: str = Field(default="", max_length=200)
    headline_variant: str = Field(default="", max_length=20)

    @field_validator(
        "name", "email", "company", "agents_other", "first_handoff", "source_other", "thread_link", mode="before"
    )
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("name", "email", "company", "agents_other", "source_other", "thread_link")
    @classmethod
    def _one_line(cls, v: str) -> str:
        # Single-line fields reach mail headers (the subject carries the
        # company); a CR/LF would make the send fail silently after a 201.
        if "\r" in v or "\n" in v:
            raise ValueError("must be a single line")
        return v

    @field_validator("attribution")
    @classmethod
    def _bounded_values(cls, v: dict[str, str]) -> dict[str, str]:
        return {str(k)[:60]: str(val)[:500] for k, val in v.items()}
