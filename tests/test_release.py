import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).parents[1]


def run_config(root: Path, *args, variables=None):
    env = {name: value for name, value in os.environ.items() if not name.startswith("HARNESS_")}
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/configure_canvas.py"), "--root", str(root), "--check-env", *args],
        env=env | (variables or {}), input="", capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def setup_project(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())
    (root / "mini-swe-agent").mkdir(exist_ok=True)


def test_existing_api_files_are_unchanged_and_explicit_change_updates_legacy_target(tmp_path):
    setup_project(tmp_path)
    files = [tmp_path / ".env", tmp_path / "mini-swe-agent/.env"]
    for index, file in enumerate(files):
        file.write_text(f"HARNESS_BASE_URL=https://models.example.test/v1\nHARNESS_MODEL=model-{index}\n"
                        f"HARNESS_API_KEY=private-test-credential-{index}\nHARNESS_CONTEXT_POLICY=memory\n", encoding="utf-8")
    original = [file.read_bytes() for file in files]
    result = run_config(tmp_path)
    assert result.returncode == 0, result.stderr
    assert [file.read_bytes() for file in files] == original
    assert "private-test-credential" not in result.stdout + result.stderr
    result = run_config(tmp_path, "--api-base-url", "https://models.example.test/other/v1", "--model", "other-model")
    assert result.returncode == 0, result.stderr
    assert files[0].read_bytes() == original[0]
    configured = dotenv_values(files[1], encoding="utf-8-sig")
    assert configured["HARNESS_API_KEY"] == "private-test-credential-1"
    assert configured["HARNESS_MODEL"] == "other-model" and configured["HARNESS_BASE_URL"] == "https://models.example.test/other/v1"
    assert configured["HARNESS_CONTEXT_POLICY"] == "memory"


def test_new_api_host_requires_its_own_key_and_preserves_old_file_on_failure(tmp_path):
    setup_project(tmp_path)
    file = tmp_path / ".env"
    file.write_text("HARNESS_BASE_URL=https://old.example.test/v1\nHARNESS_MODEL=old-model\nHARNESS_API_KEY=old-private-test-key\n")
    original = file.read_bytes()
    result = run_config(tmp_path, "--api-base-url", "https://new.example.test/v1", "--model", "new-model")
    assert result.returncode != 0 and "API key required" in result.stderr
    assert file.read_bytes() == original
    result = run_config(tmp_path, "--api-base-url", "https://new.example.test/v1", "--model", "new-model",
                        variables={"HARNESS_API_KEY": "new-private-test-key"})
    assert result.returncode == 0, result.stderr
    assert dotenv_values(file, encoding="utf-8-sig")["HARNESS_API_KEY"] == "new-private-test-key"
    assert "new-private-test-key" not in result.stdout + result.stderr


def test_new_api_configuration_supports_private_env_key_or_explicit_local_api(tmp_path):
    setup_project(tmp_path)
    credential = "test-only-credential-not-for-output"
    result = run_config(tmp_path, "--api-base-url", "https://models.example.test/v1/", "--model", "tool-model",
                        variables={"HARNESS_API_KEY": credential})
    assert result.returncode == 0, result.stderr
    configured = dotenv_values(tmp_path / ".env", encoding="utf-8-sig")
    assert configured["HARNESS_API_KEY"] == credential
    assert configured["HARNESS_BASE_URL"] == "https://models.example.test/v1"
    assert credential not in result.stdout + result.stderr
    result = run_config(tmp_path, "--api-base-url", "http://127.0.0.1:11434/v1", "--model", "local-tool-model", "--local-api")
    assert result.returncode == 0, result.stderr
    configured = dotenv_values(tmp_path / ".env", encoding="utf-8-sig")
    assert configured["HARNESS_API_KEY"] == "local" and configured["HARNESS_MODEL"] == "local-tool-model"


@pytest.mark.parametrize(("url",), [("https://user:password@example.test/v1",), ("file:///example",),
                                    ("https://example.test/v1?key=test",)])
def test_invalid_api_url_never_writes_configuration(tmp_path, url):
    setup_project(tmp_path)
    result = run_config(tmp_path, "--api-base-url", url, "--model", "test", "--local-api")
    assert result.returncode != 0
    assert not (tmp_path / ".env").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell startup")
def test_project_environment_disables_analytics_without_touching_model_credentials(tmp_path):
    script = tmp_path / "check.ps1"
    script.write_text(
        "param($EnvScript, $Root)\n. $EnvScript -ProjectRoot $Root\n"
        "@{dnt=$env:DO_NOT_TRACK; vite=$env:VITE_DO_NOT_TRACK; canvas=$env:AGENT_CANVAS_DISABLE_TELEMETRY; "
        "modelKey=$env:HARNESS_API_KEY; python=$env:HARNESS_PYTHON; "
        "analytics=@($env:VITE_POSTHOG_API_KEY,$env:AUTOMATION_POSTHOG_API_KEY,$env:OH_TELEMETRY_HTTP_TOKEN)} | ConvertTo-Json\n",
        encoding="utf-8-sig",
    )
    env = os.environ | {name: "analytics-test-value" for name in (
        "VITE_POSTHOG_API_KEY", "AUTOMATION_POSTHOG_API_KEY", "OH_TELEMETRY_HTTP_TOKEN"
    )} | {"HARNESS_API_KEY": "model-key-must-stay"}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                             str(ROOT / "scripts/project-env.ps1"), str(tmp_path)],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["dnt"] == value["vite"] == value["canvas"] == "1"
    assert not any(value["analytics"]) and value["modelKey"] == "model-key-must-stay"
    assert Path(value["python"]) == tmp_path / ".tools/agent-env/Scripts/python.exe"


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell installer")
def test_bootstrap_extracts_portable_source_and_rejects_overwrite_and_traversal(tmp_path):
    script = tmp_path / "extract.ps1"
    script.write_text("param($Installer,$Archive,$Target)\n. $Installer\nExpand-ProjectArchive -Archive $Archive -Target $Target\n",
                      encoding="utf-8-sig")
    archive = tmp_path / "project.zip"
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("project-ref/config/runtime.json", "{}")
        bundle.writestr("project-ref/start-canvas.ps1", "Write-Output ready")
        bundle.writestr("project-ref/.gitignore", ".env\n")
    target = tmp_path / "搬家 O'Brien"

    def extract(file, destination):
        return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                               str(ROOT / "install.ps1"), str(file), str(destination)],
                              capture_output=True, text=True, encoding="utf-8", timeout=30)

    result = extract(archive, target)
    assert result.returncode == 0, result.stderr
    assert (target / "config/runtime.json").is_file() and (target / ".gitignore").read_text() == ".env\n"
    (target / ".env").write_text("existing-private-configuration")
    assert extract(archive, target).returncode != 0
    assert (target / ".env").read_text() == "existing-private-configuration"
    unsafe = tmp_path / "unsafe.zip"
    with ZipFile(unsafe, "w") as bundle:
        bundle.writestr("../escaped.txt", "must not escape")
    assert extract(unsafe, tmp_path / "unsafe-target").returncode != 0
    assert not (tmp_path / "escaped.txt").exists()


def test_release_audit_finds_private_source_values_without_printing_them(tmp_path):
    setup_project(tmp_path)
    credential = "a-private-release-test-credential"
    (tmp_path / ".env").write_text(f"HARNESS_API_KEY={credential}\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    result = subprocess.run([sys.executable, str(ROOT / "scripts/check_release.py"), "--root", str(tmp_path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    private_host = ".".join(["10", "43", "21", "7"])
    private_path = "C:" + "/Users/" + "private-person/project"
    (scripts / "leak.py").write_text(f"key={credential}\nurl=http://{private_host}/v1\npath={private_path}\n")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/check_release.py"), "--root", str(tmp_path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode != 0
    assert "scripts/leak.py" in result.stdout and "current local credential" in result.stdout
    assert "private network address" in result.stdout and "personal Windows path" in result.stdout
    assert credential not in result.stdout + result.stderr
    assert private_host not in result.stdout + result.stderr and private_path not in result.stdout + result.stderr


@pytest.mark.skipif(not shutil.which("git"), reason="Git index audit")
def test_release_audit_rejects_force_tracked_env_even_when_packaging_excludes_it(tmp_path):
    setup_project(tmp_path)
    credential = "private-git-index-test-credential"
    (tmp_path / ".env").write_text(f"HARNESS_API_KEY={credential}\n")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", ".env"], check=True, capture_output=True)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/check_release.py"), "--root", str(tmp_path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode != 0 and "private runtime/config path" in result.stdout
    assert credential not in result.stdout + result.stderr
