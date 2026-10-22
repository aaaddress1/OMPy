param(
    [switch]$SetupOnly,
    [switch]$ConfigureApi,
    [string]$ApiBaseUrl,
    [Alias('ApiModel')][string]$Model,
    [switch]$LocalApi,
    [switch]$Console,
    [ValidateSet('chatgpt')][string]$MemberLogin,
    [switch]$NoBrowser,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$root = $PSScriptRoot
. (Join-Path $root 'scripts\project-env.ps1') -ProjectRoot $root
$versions = Get-Content -LiteralPath (Join-Path $root 'config\runtime.json') -Raw | ConvertFrom-Json
$tools = Join-Path $root '.tools'
$downloads = Join-Path $tools 'downloads'
New-Item -ItemType Directory -Force -Path $downloads | Out-Null

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Program" }
}

function Get-VerifiedArchive {
    param([string]$Url, [string]$ChecksumUrl, [string]$FileName)
    $archive = Join-Path $downloads $FileName
    $checksum = "$archive.sha256"
    Invoke-WebRequest -UseBasicParsing -Uri $ChecksumUrl -OutFile $checksum
    $line = Get-Content -LiteralPath $checksum | Where-Object { $_ -match [regex]::Escape($FileName) } | Select-Object -First 1
    if (-not $line) { throw "No SHA256 checksum found for $FileName" }
    $expected = ($line.Trim() -split '\s+')[0]
    if ($expected -notmatch '^[0-9a-fA-F]{64}$') { throw "Invalid SHA256 for $FileName" }
    if (-not (Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $archive
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) { throw "SHA256 mismatch: $FileName" }
    return $archive
}

$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
if ($arch -notin @('AMD64', 'ARM64')) { throw 'Windows x64 or ARM64 is required.' }
$nodeArch = if ($arch -eq 'ARM64') { 'arm64' } else { 'x64' }
$uvArch = if ($arch -eq 'ARM64') { 'aarch64' } else { 'x86_64' }
$nodeHome = Join-Path $tools "node-v$($versions.node)-win-$nodeArch"
$node = Join-Path $nodeHome 'node.exe'
if (-not $Console -and -not (Test-Path -LiteralPath $node)) {
    Write-Host '[1/5] Downloading project-local Node.js...'
    $file = "node-v$($versions.node)-win-$nodeArch.zip"
    $base = "https://nodejs.org/download/release/v$($versions.node)"
    $archive = Get-VerifiedArchive "$base/$file" "$base/SHASUMS256.txt" $file
    Expand-Archive -LiteralPath $archive -DestinationPath $tools -Force
}
$uvHome = Join-Path $tools "uv-$($versions.uv)-$uvArch"
$uv = Join-Path $uvHome 'uv.exe'
if (-not (Test-Path -LiteralPath $uv)) {
    Write-Host '[2/5] Downloading project-local uv...'
    $file = "uv-$uvArch-pc-windows-msvc.zip"
    $base = "https://releases.astral.sh/github/uv/releases/download/$($versions.uv)"
    $archive = Get-VerifiedArchive "$base/$file" "$base/$file.sha256" $file
    Expand-Archive -LiteralPath $archive -DestinationPath $uvHome -Force
}
$env:PATH = "$nodeHome;$uvHome;$env:PATH"

Write-Host '[3/5] Preparing project-local Python and Oh My PiPy (OMPy)...'
Invoke-Checked $uv @('python', 'install', '--no-bin', '--no-registry', $versions.python)
$managedPython = (& $uv python find --managed-python $versions.python | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Project Python was not found.' }
$env:UV_PYTHON = $managedPython
$agentEnv = Join-Path $tools 'agent-env'
$agentPython = Join-Path $agentEnv 'Scripts\python.exe'
$marker = Join-Path $agentEnv '.project-root'
if (-not (Test-Path -LiteralPath $agentPython) -or -not (Test-Path -LiteralPath $marker) -or
    (Get-Content -LiteralPath $marker -Raw).Trim() -ne $root) {
    # Only this generated environment is rebuilt when the project changes location.
    Invoke-Checked $uv @('venv', '--clear', '--python', $managedPython, $agentEnv)
    Set-Content -LiteralPath $marker -Value $root -Encoding UTF8
}
Invoke-Checked $uv @('pip', 'install', '--python', $agentPython, '--require-hashes', '-r', (Join-Path $root 'config\agent-requirements.lock'))

if (-not $Console) {
    Write-Host '[4/5] Preparing OpenHands Agent Canvas...'
    $canvas = Join-Path $tools 'canvas-runtime'
    New-Item -ItemType Directory -Force -Path $canvas | Out-Null
    $sourceLock = Join-Path $root 'config\canvas\package-lock.json'
    $canvasMarker = Join-Path $canvas '.lock-sha256'
    $lockHash = (Get-FileHash -LiteralPath $sourceLock -Algorithm SHA256).Hash
    $canvasEntry = Join-Path $canvas 'node_modules\@openhands\agent-canvas\bin\agent-canvas.mjs'
    if (-not (Test-Path -LiteralPath $canvasEntry) -or -not (Test-Path -LiteralPath $canvasMarker) -or
        (Get-Content -LiteralPath $canvasMarker -Raw).Trim() -ne $lockHash) {
        Copy-Item -LiteralPath (Join-Path $root 'config\canvas\package.json'), $sourceLock -Destination $canvas -Force
        Push-Location $canvas
        try {
            for ($attempt = 1; $attempt -le 3; $attempt++) {
                try {
                    Invoke-Checked $node @((Join-Path $nodeHome 'node_modules\npm\bin\npm-cli.js'), 'ci', '--no-audit', '--no-fund',
                        '--maxsockets=4', '--fetch-retries=5', '--fetch-retry-mintimeout=2000', '--fetch-timeout=600000')
                    break
                } catch {
                    if ($attempt -eq 3) { throw }
                    Write-Host 'Retrying the Canvas dependency download...'
                    Start-Sleep -Seconds 2
                }
            }
        }
        finally { Pop-Location }
        Set-Content -LiteralPath $canvasMarker -Value $lockHash -Encoding UTF8
    }
    $env:OH_AGENT_SERVER_VERSION = $versions.agentServer
    $env:OH_AUTOMATION_VERSION = $versions.automation
    Invoke-Checked $agentPython @((Join-Path $root 'scripts\configure_canvas_runtime.py'))
    Invoke-Checked $agentPython @((Join-Path $root 'scripts\configure_canvas.py'), '--python', $agentPython)
    $backendMarker = Join-Path $tools '.backend-versions'
    $backendVersions = "$root|$managedPython|$($versions.agentServer)|$($versions.automation)"
    if (-not (Test-Path -LiteralPath $backendMarker) -or
        (Get-Content -LiteralPath $backendMarker -Raw).Trim() -ne $backendVersions) {
        # Download before launching: Canvas's normal readiness deadline is only 60 seconds.
        Write-Host 'Preparing project-local OpenHands backend dependencies (first launch)...'
        $uvx = Join-Path $uvHome 'uvx.exe'
        Invoke-Checked $uvx @('--from', "openhands-agent-server==$($versions.agentServer)",
            '--with', "openhands-sdk==$($versions.agentServer)", '--with', "openhands-tools==$($versions.agentServer)",
            '--with', "openhands-workspace==$($versions.agentServer)", '--with', 'posthog>=6,<7', 'agent-server', '--help') | Out-Null
        Invoke-Checked $uvx @('--from', "openhands-automation==$($versions.automation)", 'uvicorn', '--help') | Out-Null
        Set-Content -LiteralPath $backendMarker -Value $backendVersions -Encoding UTF8
    }
    # Backend warmup can initialize settings; apply the project's default after it finishes too.
    Invoke-Checked $agentPython @((Join-Path $root 'scripts\configure_canvas.py'), '--python', $agentPython)
}
if ($MemberLogin) {
    Invoke-Checked $agentPython @('-m', 'minisweagent.run.member_login', $MemberLogin)
    return
}
$memberProvider = $false
$providerPath = Join-Path $root '.runtime\provider.json'
if (Test-Path -LiteralPath $providerPath) {
    $memberProvider = (Get-Content -LiteralPath $providerPath -Raw | ConvertFrom-Json).provider -eq 'chatgpt'
}
$apiArgs = @((Join-Path $root 'scripts\configure_canvas.py'), '--check-env')
if ($ConfigureApi) { $apiArgs += '--configure-api' }
if ($PSBoundParameters.ContainsKey('ApiBaseUrl')) {
    $apiArgs += @('--api-base-url', $ApiBaseUrl)
    $env:HARNESS_BASE_URL = $ApiBaseUrl
}
if ($PSBoundParameters.ContainsKey('Model')) {
    $apiArgs += @('--model', $Model)
    $env:HARNESS_MODEL = $Model
}
if ($LocalApi) { $apiArgs += '--local-api'; $env:HARNESS_API_KEY = 'local' }
if ((-not $SetupOnly -and -not $memberProvider) -or $ConfigureApi -or $ApiBaseUrl -or $Model -or $LocalApi) {
    Invoke-Checked $agentPython $apiArgs
}
if ($SetupOnly) { Write-Host 'Setup complete. Run start.cmd for WebUI or start-console.cmd for the terminal.'; return }
if ($Console) {
    Invoke-Checked $agentPython @('-m', 'minisweagent.run.windows_console', '--cwd', $root)
    return
}
$usedPorts = @([System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() | ForEach-Object { $_.Port })
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
if ($PSBoundParameters.ContainsKey('Port') -and $Port -in $usedPorts) { throw "Port $Port is already in use." }
while ($Port -in $usedPorts) { $Port++ }
if (-not $env:OH_CANVAS_SAFE_BACKEND_PORT) {
    $backendPort = 18000
    while ($backendPort -in $usedPorts -or ($backendPort + 1) -in $usedPorts -or ($backendPort + 1000) -in $usedPorts) { $backendPort += 2 }
    $env:OH_CANVAS_SAFE_BACKEND_PORT = "$backendPort"
    $env:OH_CANVAS_SAFE_AUTOMATION_PORT = "$($backendPort + 1)"
}
if (-not $env:OH_CANVAS_SAFE_VITE_PORT) {
    $frontendPort = 3001
    while ($frontendPort -in $usedPorts) { $frontendPort++ }
    $env:OH_CANVAS_SAFE_VITE_PORT = "$frontendPort"
}
Write-Host "[5/5] Opening Oh My PiPy (OMPy): http://127.0.0.1:$Port/canvas"
if (-not $NoBrowser) {
    $browserScript = Join-Path $root 'scripts\open-browser.ps1'
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $browserScript + '"'), '-Port', $Port)
}
Push-Location $root
try { Invoke-Checked $node @($canvasEntry, '--host', '127.0.0.1', '--port', "$Port") }
finally { Pop-Location }
