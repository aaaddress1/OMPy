from openai.types.chat import ChatCompletionChunk

from minisweagent.models.utils.chat_stream import collect_completion


def test_stream_forwards_reasoning_before_completion_and_preserves_tool_fragments():
    updates = []

    def chunks():
        yield ChatCompletionChunk.model_validate({
            "id": "stream-test", "object": "chat.completion.chunk", "created": 1, "model": "local-model",
            "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "先檢查輸入。"}, "finish_reason": None}],
        })
        assert updates == [("reasoning_content", "先檢查輸入。")]
        for delta, finish in [
            ({"reasoning_content": "再驗證結果。", "content": "執行檢查", "tool_calls": [
                {"index": 0, "id": "call-1", "type": "function", "function": {"name": "powershell", "arguments": '{"command":"Write-'}}
            ]}, None),
            ({"tool_calls": [{"index": 0, "function": {"arguments": 'Output 323"}'}}]}, "tool_calls"),
        ]:
            yield ChatCompletionChunk.model_validate({
                "id": "stream-test", "object": "chat.completion.chunk", "created": 1, "model": "local-model",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            })
        yield ChatCompletionChunk.model_validate({
            "id": "stream-test", "object": "chat.completion.chunk", "created": 1, "model": "local-model",
            "choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 23, "total_tokens": 40},
        })

    response = collect_completion(chunks(), lambda field, text: updates.append((field, text)))
    assert "".join(text for field, text in updates if field == "reasoning_content") == "先檢查輸入。再驗證結果。"
    assert "".join(text for field, text in updates if field == "content") == "執行檢查"
    assert response.usage.prompt_tokens == 17
    assert response.choices[0].finish_reason == "tool_calls"
    assert response.choices[0].message.tool_calls[0].function.arguments == '{"command":"Write-Output 323"}'
    assert response.choices[0].message.model_dump()["reasoning_content"] == "先檢查輸入。再驗證結果。"


def test_stream_without_reasoning_only_emits_the_answer():
    chunk = ChatCompletionChunk.model_validate({
        "id": "answer-test", "object": "chat.completion.chunk", "created": 1, "model": "local-model",
        "choices": [{"index": 0, "delta": {"content": "323"}, "finish_reason": "stop"}],
    })
    updates = []
    response = collect_completion([chunk], lambda field, text: updates.append((field, text)))
    assert updates == [("content", "323")]
    assert response.choices[0].message.content == "323"
    assert "reasoning_content" not in response.choices[0].message.model_dump()
