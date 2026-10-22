import base64
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from minisweagent.context_compaction import ContextCompactionError
from minisweagent.models.chatgpt_auth import ISSUER, CredentialStore, LoginAttempt, validate_identity
from minisweagent.models.member_models import (
    ChatGPTMemberModel,
    collect_responses,
    responses_request,
)
from minisweagent.models.windows_host import POWERSHELL_TOOL


def test_pkce_registration_and_returning_account_are_bound_to_state_client_and_callback():
    attempt = LoginAttempt("urn:uuid:test")
    query = parse_qs(urlsplit(attempt.url("http://127.0.0.1:54321/auth/callback")).query)
    assert query["client_id"] == ["dynamic_agent_client"] and query["agent_name_hint"] == ["Oh My PiPy"]
    assert query["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest()).rstrip(b"=").decode()]
    callback = {"state": [attempt.state], "code": ["test-code"], "client_id": ["oaiapp_test"]}
    grant = attempt.callback(callback)
    assert grant["redirect_uri"] == query["redirect_uri"][0] and grant["client_id"] == "oaiapp_test"
    for bad in ({"state": ["wrong"]}, {"code": ["a", "b"]}, {"client_id": ["dynamic_agent_client"]}, {"error": ["access_denied"]}):
        with pytest.raises(ValueError):
            attempt.callback(callback | bad)
    returning = LoginAttempt("urn:uuid:test", {"client_id": "oaiapp_test", "id_token": "retained-test-hint"})
    query = parse_qs(urlsplit(returning.url(grant["redirect_uri"])).query)
    assert "agent_name_hint" not in query and query["id_token_hint"] == ["retained-test-hint"]
    with pytest.raises(ValueError):
        returning.callback(callback | {"state": [returning.state], "client_id": ["oaiapp_other"]})


def test_real_signed_identity_rejects_nonce_audience_issuer_expiry_and_bad_signature():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True) | {"kid": "test"}
    claims = {"sub": "account", "aud": "oaiapp_test", "iss": ISSUER, "exp": time.time() + 60, "nonce": "nonce"}
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})
    assert validate_identity(token, {"keys": [public]}, "oaiapp_test", "nonce")["sub"] == "account"
    for change in ({"nonce": "other"}, {"aud": "other"}, {"iss": "https://example.com"}, {"exp": 1}):
        with pytest.raises((ValueError, jwt.PyJWTError)):
            validate_identity(jwt.encode(claims | change, key, algorithm="RS256", headers={"kid": "test"}),
                {"keys": [public]}, "oaiapp_test", "nonce")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        validate_identity(jwt.encode(claims, other, algorithm="RS256", headers={"kid": "test"}),
            {"keys": [public]}, "oaiapp_test", "nonce")


def test_credentials_are_encrypted_and_account_records_survive_reopen(tmp_path: Path):
    store = CredentialStore(tmp_path)
    with store.locked():
        state = store.read()
        state["accounts"] = {"one": {"access_token": "private-test-access"}, "two": {"access_token": "other-test-access"}}
        state["active"] = "one"
        store.write(state)
    assert b"private-test-access" not in store.path.read_bytes()
    with CredentialStore(tmp_path).locked():
        assert CredentialStore(tmp_path).read() == state
    assert not list(tmp_path.glob("*.tmp"))


def test_responses_keeps_ompy_tool_roundtrip_and_omits_unsupported_member_fields():
    messages = [{"role": "system", "content": "general assistant"}, {"role": "user", "content": "task"},
        {"role": "assistant", "content": "checking", "tool_calls": [{"id": "call1", "type": "function",
            "function": {"name": "powershell", "arguments": '{"command":"Get-Date"}'}}]},
        {"role": "tool", "tool_call_id": "call1", "content": "tool-result"}]
    payload = responses_request({"model": "test-model", "messages": messages, "tools": [POWERSHELL_TOOL],
        "max_completion_tokens": 100, "reasoning_effort": "low"})
    assert payload["store"] is False and payload["stream"] is True
    assert "max_output_tokens" not in payload and "max_completion_tokens" not in payload
    assert payload["instructions"] == "general assistant" and all(m.get("role") != "system" for m in payload["input"])
    assert payload["input"][-2]["namespace"] == "ompy" and payload["input"][-1]["call_id"] == "call1"
    assert payload["tools"][0]["tools"][0]["name"] == "powershell"
    events = [{"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "function_call", "call_id": "call2", "namespace": "ompy", "name": "powershell",
         "arguments": '{"command":"Get-Date"}'}], "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}}]
    response = collect_responses(events, "test-model")
    with httpx.Client() as http:
        model = ChatGPTMemberModel(http_client=http, context_policy="append", request_limit=1)
        assert model.parse_actions(response.choices[0].message.tool_calls)[0]["command"] == "Get-Date"
        model.reserve_request()
        with pytest.raises(ContextCompactionError):
            model.reserve_request()
        model.close()
    assert response.usage.total_tokens == 110


def test_stream_interrupts_and_unknown_namespace_never_become_success():
    for events in ([], [{"type": "response.failed"}], [{"type": "response.incomplete"}],
                   [{"type": "response.output_text.delta", "delta": "partial"}]):
        with pytest.raises(ContextCompactionError):
            collect_responses(events, "test-model")
    with pytest.raises(ValueError):
        collect_responses([{"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "function_call", "namespace": "other"}]}}], "test-model")


def test_stream_reconstructs_output_when_completed_event_omits_items():
    message = {"type": "message", "content": [{"type": "output_text", "text": "OMPy OK"}]}
    done = {"type": "response.completed", "response": {"status": "completed", "output": []}}
    deltas = [{"type": "response.output_text.delta", "delta": text} for text in ("OMPy", " OK")]
    for events in (deltas + [done], deltas + [{"type": "response.output_item.done", "output_index": 0, "item": message}, done],
                   deltas + [{"type": "response.completed", "response": {"status": "completed", "output": [message]}}]):
        assert collect_responses(events, "test").choices[0].message.content == "OMPy OK"
    tool = {"type": "function_call", "namespace": "ompy", "name": "context", "call_id": "call1", "arguments": "{}"}
    assert collect_responses([{"type": "response.output_item.done", "output_index": 0, "item": tool}, done],
        "test").choices[0].message.tool_calls[0].function.name == "context"
    with pytest.raises(ContextCompactionError):
        collect_responses([done], "test")
