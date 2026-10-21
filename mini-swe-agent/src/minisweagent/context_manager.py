"""Editable context policy; complete turns and requests remain on disk.

Policies (the context manager C of a fixed Transformer system):
- sliding: drop the oldest turns/tool groups to fit the window.
- append: never drop anything; exceeding the window is an error.
- memory: sliding, plus the model's notes (external read/write memory) are injected into every window.
"""

import json
import re
from pathlib import Path

_DIGIT = re.compile(r"[0-9]")
_WIDE = re.compile(r"[⺀-\U0010ffff]")


class ContextWindowExceeded(ValueError):
    """The assembled input does not fit the harness context window."""


def estimate_tokens(text: str) -> float:
    """Rough Qwen-style count: ASCII digits and CJK characters ~1 token each, other text ~3.5 chars/token."""
    narrow = len(_DIGIT.findall(text))
    wide = len(_WIDE.findall(text))
    return narrow + wide + (len(text) - narrow - wide) / 3.5


class ContextManager:
    def __init__(self, *, context_window: int = 1048576, max_output_tokens: int = 8192, policy: str = "sliding"):
        if policy not in {"sliding", "append", "memory"}:
            raise ValueError(f"未知的上下文策略：{policy}")
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.input_budget = context_window - max_output_tokens - 1024
        self.policy = policy
        self.turns: list[list[dict]] = []
        self.scale = 1.0
        """Server-reported prompt tokens per estimated token, recalibrated after every request."""
        self.last_estimate = 0.0

    @staticmethod
    def normalize(messages: list[dict]) -> list[dict]:
        return [
            {
                **{k: v for k, v in message.items() if k != "extra"},
                "role": "assistant" if message["role"] == "exit" else message["role"],
            }
            for message in messages
        ]

    @staticmethod
    def raw_estimate(messages: list[dict], tools: list[dict]) -> float:
        return estimate_tokens(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False))

    def count(self, messages: list[dict], tools: list[dict]) -> int:
        return round(self.raw_estimate(messages, tools) * self.scale)

    def observe(self, prompt_tokens: int) -> None:
        """Calibrate with the server's prompt_tokens for the request most recently returned by prepare()."""
        if self.last_estimate:
            self.scale = prompt_tokens / self.last_estimate

    def prepare(
        self, messages: list[dict], tools: list[dict], *, archive: Path, notes: list[str] | None = None
    ) -> list[dict]:
        """Change selection/injection here. Budget units are calibrated tokens."""
        current = self.normalize(messages)
        previous = list(self.turns)
        if self.policy == "memory" and notes:
            current[0] = dict(current[0])
            current[0]["content"] += "\n\n## 筆記（外部記憶，每一步都會顯示）\n" + "\n".join(f"- {n}" for n in notes)

        def assemble() -> list[dict]:
            return current[:1] + [message for turn in previous for message in turn] + current[1:]

        def size() -> int:
            return self.count(assemble(), tools)

        if size() > self.input_budget:
            if self.policy == "append":
                raise ContextWindowExceeded(
                    f"上下文爆窗口：輸入約 {size()} tokens，超過可用的 {self.input_budget}"
                    f"（視窗 {self.context_window} − 輸出上限 {self.max_output_tokens} − 保留 1024）。"
                    "append 策略不刪除任何紀錄。"
                )
            # Add the archive note before trimming so its size is budgeted too.
            current[0] = dict(current[0])
            current[0]["content"] += f"\n部分舊紀錄未載入，完整執行軌跡在：{archive}。需要細節時可用工具讀取。"
        while previous and size() > self.input_budget:
            previous.pop(0)
        while size() > self.input_budget:
            # Remove a whole old assistant/tool-result group, retaining the
            # task and newest group. Never leave orphaned tool results.
            groups = [i for i, message in enumerate(current) if message["role"] == "assistant"]
            if len(groups) < 2:
                raise ContextWindowExceeded(
                    f"本輪輸入約 {size()} tokens，即使只保留任務與最新一步仍超過可用的 {self.input_budget}；"
                    "請縮小任務或工具輸出。完整紀錄已保留。"
                )
            del current[groups[0] : groups[1]]
        self.last_estimate = self.raw_estimate(assemble(), tools)
        return assemble()

    def remember(self, messages: list[dict]) -> None:
        self.turns.append(self.normalize(messages)[1:])

    def clear(self) -> None:
        self.turns.clear()
