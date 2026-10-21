"""Collect Chat Completions chunks while forwarding visible text and reasoning."""

import time
from collections.abc import Callable, Iterable

from openai.types.chat import ChatCompletion, ChatCompletionChunk


def collect_completion(chunks: Iterable[ChatCompletionChunk], notify: Callable[[str, str], None]) -> ChatCompletion:
    result = {"id": "", "object": "chat.completion", "created": 0, "model": "", "usage": None}
    text = {"content": "", "reasoning_content": ""}
    pending = dict.fromkeys(text, "")
    emitted = dict.fromkeys(text, 0.0)
    tools: dict[int, dict] = {}
    finish_reason = "stop"
    for chunk in chunks:
        result.update(id=chunk.id, created=chunk.created, model=chunk.model)
        if chunk.usage:
            result["usage"] = chunk.usage.model_dump()
        for choice in chunk.choices:
            if choice.index != 0:
                continue
            finish_reason = choice.finish_reason or finish_reason
            delta = choice.delta.model_dump(exclude_none=True)
            for field, value in {
                "reasoning_content": delta.get("reasoning_content") or delta.get("reasoning"),
                "content": delta.get("content"),
            }.items():
                if not isinstance(value, str) or not value:
                    continue
                text[field] += value
                pending[field] += value
                if time.monotonic() - emitted[field] >= 0.05 or len(pending[field]) >= 256:
                    notify(field, pending[field])
                    pending[field] = ""
                    emitted[field] = time.monotonic()
            for call in delta.get("tool_calls", []):
                tool = tools.setdefault(call["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    tool["id"] = call["id"]
                for field in ("name", "arguments"):
                    tool["function"][field] += call.get("function", {}).get(field) or ""
    for field in ("reasoning_content", "content"):
        if pending[field]:
            notify(field, pending[field])
    message = {"role": "assistant", "content": text["content"] or None}
    if text["reasoning_content"]:
        message["reasoning_content"] = text["reasoning_content"]
    if tools:
        message["tool_calls"] = [tools[index] for index in sorted(tools)]
    return ChatCompletion.model_validate(result | {"choices": [{"index": 0, "finish_reason": finish_reason, "message": message}]})
