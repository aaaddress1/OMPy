"""Exercise the live-thought adapter against the real pinned OpenHands SDK."""

import asyncio

from acp.helpers import update_agent_message_text, update_agent_thought_text
from openhands.sdk.agent.acp_agent import _OpenHandsACPBridge
from openhands.sdk.agent.stream_context import _split_chunk

from minisweagent.run.openhands_compat import enable_live_thoughts


async def check() -> None:
    enable_live_thoughts()
    installed = _OpenHandsACPBridge.session_update
    enable_live_thoughts()
    assert _OpenHandsACPBridge.session_update is installed
    bridge = _OpenHandsACPBridge()
    chunks = []
    bridge.on_token = chunks.append
    bridge.mask = lambda text: text.replace("test-credential", "[redacted]")
    await bridge.session_update("test-session", update_agent_thought_text("檢查 test-credential 的設定。"))
    assert bridge.accumulated_thoughts == ["檢查 [redacted] 的設定。"]
    assert _split_chunk(chunks[0])[0][:2] == ("reasoning", "檢查 [redacted] 的設定。")
    await bridge.session_update("test-session", update_agent_message_text("完成"))
    assert _split_chunk(chunks[1])[0][:2] == ("text", "完成")
    assert bridge.accumulated_text == ["完成"]
    print("Pinned OpenHands ACP live thoughts, text forwarding and masking: passed.")


if __name__ == "__main__":
    asyncio.run(check())
