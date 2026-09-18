# Builds the Local Scribe Windows installer (.exe) via Inno Setup.
#
# Run on Windows (or a windows-latest GitHub Actions runner), with Inno
# Setup's compiler (iscc.exe) on PATH - https://jrsoftware.org/isinfo.php.
#
# What gets bundled: the app's own source (pure Python, small) and a
# self-contained CPython interpreter (python-build-standalone, the same
# builds bootstrap.py itself knows how to fetch at first run - here
# fetched once at build time and baked in instead). The heavy ASR packages
# (ctranslate2, mlx-whisper is Mac-only, ...) and the Whisper model itself
# are NOT bundled: they stay a one-time download on first launch, exactly
# as they already are for a from-source install - see the README's "one
# deliberate network request" privacy note. Baking them in would make this
# a multi-gigabyte download for no real benefit.
#
# Unsigned by default - Windows will show the SmartScreen "Windows
# protected your PC" prompt on first open (documented in the README).
# Signing (Microsoft Trusted Signing is the recommended modern option) can
# be added as an additional step in CI later without changing this script.

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir "..\..")
$Build = Join-Path $ScriptDir "build"
$Version = (Get-Content (Join-Path $Root "VERSION")).Trim()

Write-Host "==> Building Local Scribe installer ($Version, Windows x86_64)"
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory -Force -Path "$Build\app" | Out-Null

Write-Host "==> Copying app source"
foreach ($item in @("app", "bootstrap.py", "requirements.txt", "VERSION", "LICENSE")) {
    Copy-Item -Recurse -Force (Join-Path $Root $item) (Join-Path "$Build\app" $item)
}

Write-Host "==> Fetching a self-contained Python interpreter (Windows x86_64)"
# Authenticated when a token is available (CI sets GITHUB_TOKEN): GitHub
# Actions runners share a pool of outbound IPs that can exhaust the
# unauthenticated API rate limit fast; unset locally, this is simply an
# empty (no-op) header set.
$GhHeaders = @{}
if ($env:GITHUB_TOKEN) { $GhHeaders["Authorization"] = "Bearer $env:GITHUB_TOKEN" }
$Release = Invoke-RestMethod `
    -Uri "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" `
    -Headers $GhHeaders -TimeoutSec 20

$Asset = $null
foreach ($PyVer in @("3.13", "3.12", "3.11")) {
    $Asset = $Release.assets | Where-Object {
        $_.name -like "cpython-$PyVer.*-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
    } | Select-Object -First 1
    if ($Asset) { break }
}
if (-not $Asset) { throw "Could not find a matching Python build for Windows x86_64" }

$Archive = Join-Path $env:TEMP "localscribe-build-python.tar.gz"
Invoke-WebRequest -Uri $Asset.browser_download_url -OutFile $Archive -TimeoutSec 180
# tar.exe ships with Windows 10 1803+/Windows 11 and extracts .tar.gz natively.
tar -xzf $Archive -C "$Build\app"
Remove-Item -Force $Archive
# python-build-standalone extracts to a top-level "python\" directory.
Move-Item "$Build\app\python" "$Build\python"

Write-Host "==> Writing launcher"
# Sets the per-user data directory (so projects/models/settings survive an
# uninstall/reinstall, since they never live under Program Files) and
# starts the app with pythonw.exe so no console window flashes up.
@"
@echo off
set LOCALSCRIBE_DATA_DIR=%LOCALAPPDATA%\Local Scribe
start "" "%~dp0python\pythonw.exe" "%~dp0app\bootstrap.py" --desktop
"@ | Set-Content -Path "$Build\LocalScribe.bat" -Encoding ASCII

Write-Host "==> Compiling installer"
$DistDir = Join-Path $Root "dist\windows"
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
iscc.exe /DAppVersion="$Version" /DBuildDir="$Build" /DOutputDir="$DistDir" `
    (Join-Path $ScriptDir "installer.iss")

Write-Host "==> Done: see $DistDir"
Write-Host "    Unsigned - opening it for the first time shows SmartScreen's"
Write-Host "    'Windows protected your PC' prompt, documented in the README,"
Write-Host "    until code signing is set up."
