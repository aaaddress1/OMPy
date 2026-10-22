param(
    [string]$RepositoryUrl,
    [string]$Ref = 'main',
    [string]$Destination = (Join-Path (Get-Location) 'OMPy'),
    [string]$ApiBaseUrl,
    [Alias('ApiModel')][string]$Model,
    [switch]$LocalApi,
    [switch]$Console,
    [ValidateSet('chatgpt')][string]$MemberLogin,
    [switch]$ConfigureApi,
    [switch]$SetupOnly,
    [switch]$NoBrowser,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Expand-ProjectArchive {
    param([string]$Archive, [string]$Target)
    $targetPath = [IO.Path]::GetFullPath($Target)
    if (Test-Path -LiteralPath $targetPath) {
        if ((Get-Item -LiteralPath $targetPath).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Destination must not be a symbolic link or junction.'
        }
        if (@(Get-ChildItem -LiteralPath $targetPath -Force | Where-Object { $_.Name -ne '.bootstrap' }).Count) {
            throw 'Destination is not empty. Existing files and API settings will not be overwritten.'
        }
    }
    $bootstrap = Join-Path $targetPath '.bootstrap'
    if ((Test-Path -LiteralPath $bootstrap) -and
        ((Get-Item -LiteralPath $bootstrap).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Bootstrap directory must not be a symbolic link or junction.'
    }
    $stage = Join-Path $bootstrap ('source-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead([IO.Path]::GetFullPath($Archive))
    try {
        foreach ($entry in $zip.Entries) {
            $resolved = [IO.Path]::GetFullPath((Join-Path $stage $entry.FullName))
            if (-not $resolved.StartsWith($stage + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Archive entry escapes its destination.'
            }
        }
    } finally { $zip.Dispose() }
    Expand-Archive -LiteralPath $Archive -DestinationPath $stage
    $folders = @(Get-ChildItem -LiteralPath $stage -Directory)
    if ($folders.Count -ne 1 -or -not (Test-Path -LiteralPath (Join-Path $folders[0].FullName 'config\runtime.json')) -or
        -not (Test-Path -LiteralPath (Join-Path $folders[0].FullName 'start-canvas.ps1'))) {
        throw 'Archive does not contain the expected OMPy project.'
    }
    Get-ChildItem -LiteralPath $folders[0].FullName -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $targetPath -Recurse
    }
    return $targetPath
}

if ($MyInvocation.InvocationName -eq '.') { return }

$projectRoot = $PSScriptRoot
if (-not $projectRoot -or -not (Test-Path -LiteralPath (Join-Path $projectRoot 'config\runtime.json'))) {
    $projectRoot = [IO.Path]::GetFullPath($Destination)
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'config\runtime.json'))) {
        if ($RepositoryUrl -notmatch '^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$') {
            throw 'Pass -RepositoryUrl https://github.com/OWNER/REPO, or run install.ps1 inside the downloaded project.'
        }
        $owner, $repo = $Matches[1], $Matches[2]
        if ((Test-Path -LiteralPath $projectRoot) -and @(Get-ChildItem -LiteralPath $projectRoot -Force).Count) {
            throw 'Destination is not empty. Choose a new directory.'
        }
        if ((Test-Path -LiteralPath $projectRoot) -and
            ((Get-Item -LiteralPath $projectRoot).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Destination must not be a symbolic link or junction.'
        }
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $bootstrap = Join-Path $projectRoot '.bootstrap'
        New-Item -ItemType Directory -Path $bootstrap -Force | Out-Null
        $archive = Join-Path $bootstrap 'source.zip'
        $url = 'https://codeload.github.com/' + $owner + '/' + $repo + '/zip/' + [uri]::EscapeDataString($Ref)
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive
        $projectRoot = Expand-ProjectArchive -Archive $archive -Target $projectRoot
    }
}
$forward = @{}
foreach ($name in @('ApiBaseUrl', 'Model', 'LocalApi', 'Console', 'MemberLogin', 'ConfigureApi', 'SetupOnly', 'NoBrowser', 'Port')) {
    if ($PSBoundParameters.ContainsKey($name)) { $forward[$name] = $PSBoundParameters[$name] }
}
& (Join-Path $projectRoot 'start-canvas.ps1') @forward
