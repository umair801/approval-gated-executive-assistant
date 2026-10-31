"""Tool registry. Read tools run. Write tools are registered only so that any attempt to call
one is blocked and audited. Side effects have exactly one path: ApprovalGate.approve()."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from ..store import Store


class WriteToolBlocked(Exception):
    """Raised when something tries to call a send, book or pay tool directly."""


@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable[..., Any] | None
    side_effect: Literal["read", "write"]


class ToolRegistry:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._tools: dict[str, Tool] = {}

    def read(self, name: str, fn: Callable[..., Any]) -> None:
        self._tools[name] = Tool(name, fn, "read")

    def write_placeholder(self, name: str) -> None:
        self._tools[name] = Tool(name, None, "write")

    def names(self) -> dict[str, str]:
        return {n: t.side_effect for n, t in self._tools.items()}

    def call(self, user_id: str, name: str, **kwargs: Any) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool {name!r}")
        if tool.side_effect != "read" or tool.fn is None:
            self.store.append_audit(user_id, "write_blocked", "agent", None, {"tool": name})
            raise WriteToolBlocked(f"{name} is a write tool. Propose it through the approval gate.")
        result = tool.fn(user_id, **kwargs)
        n = len(result) if hasattr(result, "__len__") else 1
        self.store.append_audit(user_id, "tool_read", "agent", None, {"tool": name, "items": n})
        return result
