param([Parameter(Mandatory = $true)][string]$ProjectRoot)

$env:OH_PERSISTENCE_DIR = Join-Path $ProjectRoot '.runtime\openhands'
$env:OH_CANVAS_SAFE_STATE_DIR = Join-Path $env:OH_PERSISTENCE_DIR 'agent-canvas'
$env:OH_SESSION_API_KEY_PATH = Join-Path $env:OH_CANVAS_SAFE_STATE_DIR 'api-key.txt'
$env:OH_SECRET_KEY_PATH = Join-Path $env:OH_CANVAS_SAFE_STATE_DIR 'secret-key.txt'
$env:MSWEA_GLOBAL_CONFIG_DIR = Join-Path $ProjectRoot 'mini-swe-agent\.config'
$env:PYTHONPATH = Join-Path $ProjectRoot 'mini-swe-agent\src'
$env:UV_CACHE_DIR = Join-Path $ProjectRoot '.tools\uv-cache'
$env:UV_TOOL_DIR = Join-Path $ProjectRoot '.tools\uv-tools'
$env:UV_TOOL_BIN_DIR = Join-Path $ProjectRoot '.tools\uv-bin'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $ProjectRoot '.tools\python'
$env:UV_PYTHON_BIN_DIR = Join-Path $ProjectRoot '.tools\python-bin'
$env:UV_NO_MODIFY_PATH = '1'
$env:UV_NO_CONFIG = '1'
$env:npm_config_cache = Join-Path $ProjectRoot '.tools\npm-cache'
$env:npm_config_prefix = Join-Path $ProjectRoot '.tools\npm-global'
$env:MSWEA_SILENT_STARTUP = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:OPENHANDS_SUPPRESS_BANNER = '1'
$env:HARNESS_PYTHON = Join-Path $ProjectRoot '.tools\agent-env\Scripts\python.exe'
$env:DO_NOT_TRACK = '1'
$env:VITE_DO_NOT_TRACK = '1'
$env:AGENT_CANVAS_DISABLE_TELEMETRY = '1'
foreach ($name in @('OH_TELEMETRY_EXPORTER', 'OH_TELEMETRY_POSTHOG_API_KEY', 'OH_TELEMETRY_POSTHOG_HOST',
    'OH_TELEMETRY_HTTP_ENDPOINT', 'OH_TELEMETRY_HTTP_TOKEN', 'VITE_POSTHOG_API_KEY', 'VITE_POSTHOG_HOST',
    'AUTOMATION_POSTHOG_API_KEY', 'AUTOMATION_POSTHOG_HOST')) {
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}
$env:OH_TELEMETRY_EXPORTER = 'none'
$env:ANONYMIZED_TELEMETRY = 'false'
$env:BROWSER_USE_CLOUD_SYNC = 'false'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:OTEL_SDK_DISABLED = 'true'
