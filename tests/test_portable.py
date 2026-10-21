import importlib.util
import asyncio
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from zipfile import ZipFile


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_profiles_relocate_and_preserve_ui_settings(tmp_path):
    configure = load_script("configure_canvas").configure
    roots = [tmp_path / "original", tmp_path / "搬家 O'Brien project"]
    for root in roots:
        if root != roots[0]:
            shutil.copytree(roots[0] / ".runtime", root / ".runtime")
        python = root / ".tools" / "agent-env" / "Scripts" / "python.exe"
        configure(root, python)
        state = root / ".runtime" / "openhands"
        ids = []
        for file in (state / "agent-profiles").glob("*.json"):
            profile = json.loads(file.read_text(encoding="utf-8"))
            assert shlex.split(profile["acp_command"]) == [python.as_posix()]
            assert profile["acp_args"][:2] == ["-m", "minisweagent.run.windows_acp"]
            assert roots[0].as_posix() not in profile["acp_command"] or root == roots[0]
            ids.append(profile["id"])
        assert len(set(ids)) == 4
        settings_path = state / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings["theme"] = "dark"
        settings["agent_settings"] = {"agent_kind": "openhands", "llm": {"model": "unrelated-model"}}
        settings["active_agent_profile_id"] = "unrelated-agent"
        settings["active_profile"] = "unrelated-llm"
        settings["active_meta_profile"] = "unrelated-meta-llm"
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        configure(root, python)
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["theme"] == "dark"
        assert settings["active_agent_profile_id"] in ids
        assert settings["agent_settings"]["acp_command"] == [python.as_posix()]
        assert settings["agent_settings"]["agent_kind"] == "acp"
        assert settings["active_profile"] is None and settings["active_meta_profile"] is None
        workspace = json.loads((state / "workspaces.json").read_text(encoding="utf-8"))
        assert [w["path"] for w in workspace["workspaces"]] == [str(root)]


def test_source_zip_includes_vendored_source_without_secrets_or_gitlinks(tmp_path):
    root = tmp_path / "source"
    files = ["start.cmd", ".env.example", "config/runtime.json", "mini-swe-agent/src/minisweagent/__init__.py",
             "mini-swe-agent/LICENSE.md", ".env", ".env.backup", "mini-swe-agent/.env",
             "mini-swe-agent/.git/config", "mini-swe-agent/.runs/session/request.json",
             "mini-swe-agent/.venv/secret.txt", "mini-swe-agent/__pycache__/cached.pyc",
             ".runtime/openhands/agent-canvas/api-key.txt", ".tools/cache/secret.txt"]
    for file in files:
        path = root / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file)
    output = tmp_path / "source.zip"
    load_script("package_source").package(root, output)
    with ZipFile(output) as archive:
        names = set(archive.namelist())
        assert names == {"OMPy/" + name for name in files[:5]}


def test_managed_profile_rename_preserves_id_and_migrates_old_filename(tmp_path):
    configure = load_script("configure_canvas").configure
    python = tmp_path / ".tools/agent-env/Scripts/python.exe"
    configure(tmp_path, python)
    profiles = tmp_path / ".runtime/openhands/agent-profiles"
    profile = json.loads((profiles / "OMPy.json").read_text(encoding="utf-8"))
    profile["name"] = "mini-swe-agent"
    (profiles / "mini-swe-agent.json").write_text(json.dumps(profile), encoding="utf-8")
    (profiles / "OMPy.json").unlink()
    configure(tmp_path, python)
    renamed = json.loads((profiles / "OMPy.json").read_text(encoding="utf-8"))
    assert renamed["id"] == profile["id"] and renamed["name"] == "OMPy"
    assert not (profiles / "mini-swe-agent.json").exists()
    assert len(list(profiles.glob("*.json"))) == 4


def test_canvas_branding_and_legacy_thought_hook_migration_are_idempotent(tmp_path):
    module = load_script("configure_canvas_runtime")
    canvas = tmp_path / "node_modules/@openhands/agent-canvas"
    (canvas / "tools").mkdir(parents=True)
    (canvas / "build").mkdir()
    entry = canvas / "tools/canvas_ui_tool.py"
    entry.write_text("# tiny_harness: live ACP thoughts\nfrom minisweagent.run.openhands_compat import enable_live_thoughts\nenable_live_thoughts()\n", encoding="utf-8")
    index = canvas / "build/index.html"
    index.write_text("<html><head><title>OpenHands</title></head><body></body></html>", encoding="utf-8")
    module.configure(tmp_path)
    first = entry.read_text(encoding="utf-8"), index.read_text(encoding="utf-8")
    module.configure(tmp_path)
    assert first == (entry.read_text(encoding="utf-8"), index.read_text(encoding="utf-8"))
    assert first[0].count(module.MARKER) == 1 and "tiny_harness" not in first[0]
    assert first[1].count("data-ompy-brand") == 1 and "Oh My PiPy (OMPy)" in first[1]


def test_real_acp_process_handshake_session_and_commands(tmp_path):
    from acp import PROTOCOL_VERSION

    async def run():
        source = Path(__file__).parents[1] / "mini-swe-agent" / "src"
        env = os.environ | {
            "PYTHONPATH": str(source), "MSWEA_SILENT_STARTUP": "1", "PYTHONUTF8": "1",
            "MSWEA_GLOBAL_CONFIG_DIR": str(tmp_path / "config"),
            "HARNESS_API_KEY": "integration-test-no-network",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "minisweagent.run.windows_acp", env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        notifications = []

        async def request(id, method, params):
            process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params}) + "\n").encode())
            await process.stdin.drain()
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=90 if method == "initialize" else 30)
                assert line, (await process.stderr.read()).decode()
                message = json.loads(line)
                if message.get("id") == id:
                    assert "error" not in message, message
                    return message["result"]
                notifications.append(message)

        try:
            initialized = await request(1, "initialize", {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}})
            assert initialized["agentInfo"]["name"] == "ompy"
            assert initialized["agentInfo"]["title"] == "Oh My PiPy (OMPy)"
            session = await request(2, "session/new", {"cwd": str(tmp_path), "mcpServers": []})
            for id, command in [(3, "/reasoning high"), (4, "/new")]:
                result = await request(id, "session/prompt", {"sessionId": session["sessionId"], "prompt": [{"type": "text", "text": command}]})
                assert result["stopReason"] == "end_turn"
            updates = json.dumps(notifications, ensure_ascii=False)
            assert "推理程度：high" in updates
            assert "已清除目前對話" in updates
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.terminate()
                await process.wait()

    asyncio.run(run())
