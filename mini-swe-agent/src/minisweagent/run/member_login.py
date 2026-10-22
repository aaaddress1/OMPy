"""Optional member login and provider selection, kept separate from .env."""

import json

import typer

from minisweagent.models.chatgpt_auth import ChatGPTAuth
from minisweagent.models.member_models import SELECTION

app = typer.Typer(pretty_exceptions_enable=False)


def select(provider: str, model: str) -> None:
    SELECTION.parent.mkdir(parents=True, exist_ok=True)
    temporary = SELECTION.with_suffix(".tmp")
    temporary.write_text(json.dumps({"provider": provider, "model": model, "request_limit": 3}), encoding="utf-8")
    temporary.replace(SELECTION)
    print(f"已選擇 {provider} / {model}。原 API .env 保留。")
    if provider == "chatgpt":
        print("每次啟動最多 3 次會員請求（含摘要）。")


@app.command()
def chatgpt(account: str | None = None, model: str = "gpt-6.1-sol"):
    auth = ChatGPTAuth()
    identity = auth.sign_in(account)
    models = auth.models()
    if model not in {m["slug"] for m in models}:
        print("已登入，但指定模型不在帳號清單中。可用模型：" + ", ".join(m["slug"] for m in models))
        raise typer.Exit(1)
    select("chatgpt", model)
    print(f"ChatGPT 帳號：{identity['email']} ({identity['client_id']})")
    print("用量與每週上限：https://chatgpt.com/settings/usage")


@app.command()
def use(provider: str, model: str | None = None):
    if provider == "api":
        select("api", "原本 .env")
    elif provider == "chatgpt":
        auth = ChatGPTAuth()
        model = model or "gpt-6.1-sol"
        if model not in {m["slug"] for m in auth.models()}:
            raise ValueError("此帳號未列出指定的 ChatGPT 模型。")
        select(provider, model)
    else:
        raise ValueError("可選 api 或 chatgpt。")


@app.command()
def status():
    print(SELECTION.read_text(encoding="utf-8") if SELECTION.exists() else "目前使用原本 API 配置。")
    auth = ChatGPTAuth()
    with auth.store.locked():
        state = auth.store.read()
        for client, record in state["accounts"].items():
            print(f"{'*' if client == state['active'] else ' '} {client} {record.get('email', '')}")


@app.command()
def logout(provider: str, account: str | None = None):
    if provider == "chatgpt":
        auth = ChatGPTAuth()
        with auth.store.locked():
            state = auth.store.read()
            selected = account or state["active"]
        if selected:
            auth.sign_out(selected)
    else:
        raise ValueError("可登出 chatgpt。")
    if SELECTION.exists() and json.loads(SELECTION.read_text(encoding="utf-8"))["provider"] == provider:
        select("api", "原本 .env")


if __name__ == "__main__":
    app()
