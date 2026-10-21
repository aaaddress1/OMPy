"""Project-local ACP thought relay for the pinned OpenHands SDK 1.56.0."""

from importlib.metadata import version


def enable_live_thoughts() -> None:
    from acp.schema import AgentThoughtChunk, TextContentBlock
    from openhands.sdk.agent.acp_agent import _OpenHandsACPBridge
    from openhands.sdk.agent.stream_context import StreamContext
    from openhands.sdk.llm.streaming import LLMStreamChunk

    if version("openhands-sdk") != "1.56.0":
        raise RuntimeError("Review the ACP thought relay before upgrading OpenHands SDK.")
    original = _OpenHandsACPBridge.session_update
    if getattr(original, "_ompy_thoughts", False) or getattr(original, "_tiny_harness_thoughts", False):
        return
    original_open = StreamContext.open.__func__

    @classmethod
    def open_stream(cls, conversation, on_token):
        stream = original_open(cls, conversation, on_token)
        # Resolve the locked state on the run thread, before ACP callbacks use the portal thread.
        stream._mask = conversation.state.secret_registry.compile_stream_mask
        return stream

    StreamContext.open = open_stream

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        await original(self, session_id, update, **kwargs)
        if (isinstance(update, AgentThoughtChunk) and isinstance(update.content, TextContentBlock)
                and self.on_token and session_id != self._fork_session_id):
            self.on_token(LLMStreamChunk.model_validate({
                "id": "acp-reasoning", "object": "chat.completion.chunk", "created": 0, "model": "acp",
                "choices": [{"index": 0, "delta": {"reasoning_content": self._mask_value(update.content.text)}}],
            }))

    session_update._ompy_thoughts = True
    _OpenHandsACPBridge.session_update = session_update
