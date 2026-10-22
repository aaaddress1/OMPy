"""One short member request, explicitly invoked after login. Never uses the .env API."""

import json
from contextlib import closing

import httpx

from minisweagent.models.member_models import SELECTION, ChatGPTMemberModel


def main():
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    provider = selection["provider"]
    if provider != "chatgpt":
        raise ValueError("請先登入並選取會員連線；此測試不使用原本 API。")
    with httpx.Client() as http, closing(ChatGPTMemberModel(http_client=http, model_name=selection["model"],
        context_policy="append", context_window=8192, max_output_tokens=512, request_limit=1)) as model:
        response = model.complete({"model": selection["model"], "reasoning_effort": "low", "messages": [
            {"role": "system", "content": "Reply briefly. Do not use tools."},
            {"role": "user", "content": "Reply with exactly OMPy OK"}]})
        print(json.dumps({"provider": provider, "model": selection["model"],
            "text": response.choices[0].message.content, "usage": response.usage.model_dump()}, ensure_ascii=False))
        if response.choices[0].message.content.strip() != "OMPy OK":
            raise ValueError("會員測試未取得預期回覆。")


if __name__ == "__main__":
    main()
