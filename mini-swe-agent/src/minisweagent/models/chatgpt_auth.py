"""Project-local Sign in with ChatGPT. Never reads another application's credentials."""

import base64
import ctypes
import hashlib
import json
import os
import secrets
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
RUNTIME = Path(__file__).resolve().parents[4] / ".runtime"


def protect(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("此版本的會員憑證使用 Windows DPAPI，請在 Windows 啟動。")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data)
    source, result = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class CredentialStore:
    def __init__(self, directory: Path = RUNTIME):
        self.directory = directory
        self.path = directory / "chatgpt.auth"

    @contextmanager
    def locked(self):
        import msvcrt

        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "chatgpt.lock").open("a+b") as lock:
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def read(self) -> dict:
        return json.loads(protect(self.path.read_bytes(), decrypt=True)) if self.path.exists() else {
            "host_id": "urn:uuid:" + str(uuid4()), "accounts": {}, "active": None}

    def write(self, state: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / (uuid4().hex + ".tmp")
        temporary.write_bytes(protect(json.dumps(state).encode()))
        temporary.replace(self.path)


def validate_identity(token: str, keys: dict, client_id: str, nonce: str | None = None) -> dict:
    header = jwt.get_unverified_header(token)
    key = next((k for k in keys["keys"] if k["kid"] == header.get("kid")), None)
    if key is None:
        raise ValueError("登入簽章使用未知金鑰。")
    identity = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=["RS256"], issuer=ISSUER,
        audience=client_id, options={"require": ["exp", "iss", "aud", "sub"]})
    if nonce is not None and not secrets.compare_digest(identity.get("nonce", ""), nonce):
        raise ValueError("登入 nonce 不符，請重新登入。")
    return identity


class LoginAttempt:
    def __init__(self, host_id: str, account: dict | None = None):
        self.account = account or {}
        self.state, self.nonce, self.verifier = (secrets.token_urlsafe(32) for _ in range(3))
        self.host_id = host_id

    def url(self, redirect: str) -> str:
        self.redirect = redirect
        parameters = {"client_id": self.account.get("client_id", "dynamic_agent_client"),
            "ext_agent_host_id": self.host_id, "response_type": "code", "redirect_uri": redirect,
            "scope": SCOPE, "resource": RESOURCE, "state": self.state, "nonce": self.nonce,
            "code_challenge_method": "S256", "code_challenge": base64.urlsafe_b64encode(
                hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()}
        if not self.account:
            parameters["agent_name_hint"] = "Oh My PiPy"
        elif self.account.get("id_token"):
            parameters["id_token_hint"] = self.account["id_token"]
        return ISSUER + "/api/accounts/authorize?" + urlencode(parameters)

    def callback(self, query: dict[str, list[str]]) -> dict:
        if any(len(v) != 1 for v in query.values()) or not secrets.compare_digest(query.get("state", [""])[0], self.state):
            raise ValueError("登入 state 不符。")
        if "error" in query:
            raise ValueError("登入未獲授權，原有連線保持不變。")
        client = query.get("client_id", [self.account.get("client_id", "")])[0]
        if not client.startswith("oaiapp_") or (self.account and client != self.account["client_id"]):
            raise ValueError("登入 client ID 不符或尚未完成註冊。")
        if not query.get("code", [""])[0]:
            raise ValueError("登入未回傳授權碼。")
        return {"grant_type": "authorization_code", "client_id": client, "code": query["code"][0],
            "code_verifier": self.verifier, "redirect_uri": self.redirect, "resource": RESOURCE}


class ChatGPTAuth:
    def __init__(self, store: CredentialStore | None = None):
        self.store = store or CredentialStore()

    def sign_in(self, client_id: str | None = None, *, timeout: int = 300) -> dict:
        with self.store.locked():
            state = self.store.read()
            account = state["accounts"][client_id] if client_id else None
            self.store.write(state)
        attempt = LoginAttempt(state["host_id"], account)
        received = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                valid = (parsed.path == "/auth/callback" and
                    query.get("state") == [attempt.state] and not received)
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(("已收到登入回覆，請回到 OMPy。" if valid else "無效的登入回覆。").encode())
                if valid:
                    received.update(query)

        with HTTPServer(("127.0.0.1", 0), Callback) as server:
            server.timeout = 0.5
            redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
            url = attempt.url(redirect)
            print("Continue with ChatGPT — 請在瀏覽器授權 OMPy 使用會員方案。", flush=True)
            if not webbrowser.open(url):
                raise RuntimeError("無法開啟瀏覽器，請在有桌面瀏覽器的 Windows 登入。")
            deadline = time.monotonic() + timeout
            while not received and time.monotonic() < deadline:
                server.handle_request()
            if not received:
                raise TimeoutError("登入逾時，原有連線保持不變。")
        grant = attempt.callback(received)
        with httpx.Client(trust_env=False, timeout=30) as http:
            tokens = http.post(ISSUER + "/api/accounts/oauth/token", data=grant).raise_for_status().json()
            keys = http.get(ISSUER + "/.well-known/jwks.json").raise_for_status().json()
        identity = validate_identity(tokens["id_token"], keys, grant["client_id"], attempt.nonce)
        if tokens.get("token_type", "").lower() != "bearer" or not all(tokens.get(k) for k in ("access_token", "refresh_token")):
            raise ValueError("登入缺少有效的會員憑證。")
        if account and identity["sub"] != account["subject"]:
            raise ValueError("登入帳號不符，原有連線保持不變。")
        if "chatgpt.tokens.use.direct" not in tokens.get("scope", "").split():
            raise ValueError("尚未授權使用 ChatGPT 方案，請重新登入並勾選會員用量權限。")
        record = tokens | {"client_id": grant["client_id"], "subject": identity["sub"],
            "email": identity.get("email", ""), "expires_at": time.time() + tokens["expires_in"]}
        with self.store.locked():
            state = self.store.read()
            state["accounts"][record["client_id"]] = record
            state["active"] = record["client_id"]
            self.store.write(state)
        return {k: record[k] for k in ("client_id", "email")}

    def token(self) -> str:
        with self.store.locked():
            state = self.store.read()
            account = state["accounts"].get(state["active"])
            if not account or not account.get("refresh_token"):
                raise ValueError("請先執行 ChatGPT 會員登入。")
            if account["expires_at"] < time.time() + 60:
                with httpx.Client(trust_env=False, timeout=30) as http:
                    tokens = http.post(ISSUER + "/api/accounts/oauth/token", data={"grant_type": "refresh_token",
                        "client_id": account["client_id"], "refresh_token": account["refresh_token"],
                        "resource": RESOURCE}).raise_for_status().json()
                    if tokens.get("id_token"):
                        keys = http.get(ISSUER + "/.well-known/jwks.json").raise_for_status().json()
                        if validate_identity(tokens["id_token"], keys, account["client_id"])["sub"] != account["subject"]:
                            raise ValueError("更新登入時帳號不符。")
                if "chatgpt.tokens.use.direct" not in tokens.get("scope", account["scope"]).split():
                    raise ValueError("會員用量授權已移除，請重新登入。")
                account.update(tokens | {"expires_at": time.time() + tokens["expires_in"]})
                self.store.write(state)
            return account["access_token"]

    def models(self) -> list[dict]:
        with httpx.Client(trust_env=False, timeout=30) as http:
            result = http.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + self.token()})
            return [m for m in result.raise_for_status().json()["models"] if m.get("visibility") == "list"]

    def sign_out(self, client_id: str) -> None:
        with self.store.locked():
            state = self.store.read()
            account = state["accounts"][client_id]
            with httpx.Client(trust_env=False, timeout=30) as http:
                http.post(ISSUER + "/api/accounts/oauth/revoke", data={"token": account["refresh_token"],
                    "token_type_hint": "refresh_token", "client_id": client_id}).raise_for_status()
            for key in ("access_token", "refresh_token", "id_token"):
                account.pop(key, None)
            if state["active"] == client_id:
                state["active"] = None
            self.store.write(state)
