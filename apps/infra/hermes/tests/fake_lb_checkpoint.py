"""Lane H's F1 `Decision` (fork branch `lightbulb`, agent/lb_checkpoint.py): the shape the bridge's effects
callback returns. Only the value type is copied; install/before_tool/tool_committed live in the image."""
from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True)
class Decision:
    kind: Literal["proceed", "refuse", "replay"]
    message: Optional[str] = None
    result: Optional[str] = None

    @classmethod
    def proceed(cls) -> "Decision":
        return cls("proceed")

    @classmethod
    def refuse(cls, message: str) -> "Decision":
        return cls("refuse", message=message)

    @classmethod
    def replay(cls, result: str) -> "Decision":
        return cls("replay", result=result)


def install(agent, callback, *, timeout_s: float = 10.0) -> None:
    agent._lb_checkpoint = callback


def installed(agent) -> bool:
    return getattr(agent, "_lb_checkpoint", None) is not None
