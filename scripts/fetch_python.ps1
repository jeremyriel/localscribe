# Downloads a self-contained CPython build for Windows when the machine has
# no Python interpreter at all, so run.bat has something to run bootstrap.py
# with. bootstrap.py can find or download a supported Python on its own once
# *some* interpreter is running it - this script exists only for the case
# where there is none, which run.bat cannot solve with batch alone.
#
# Prints the resulting python.exe path to stdout (and nothing else) on
# success. Prints nothing and exits non-zero on failure, so run.bat can fall
# back to its own "install Python yourself" message.

$ErrorActionPreference = "Stop"

try {
    $root = Split-Path -Parent $PSScriptRoot
    $arch = if ([Environment]::Is64BitOperatingSystem -and
                ($env:PROCESSOR_ARCHITECTURE -eq "ARM64")) { "aarch64" } else { "x86_64" }
    $triple = "$arch-pc-windows-msvc"

    $release = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" `
        -TimeoutSec 20

    $asset = $null
    foreach ($pyver in @("3.13", "3.12", "3.11")) {
        $asset = $release.assets | Where-Object {
            $_.name -like "cpython-$pyver.*-$triple-install_only_stripped.tar.gz"
        } | Select-Object -First 1
        if ($asset) { break }
    }
    if (-not $asset) { exit 1 }

    $destName = $asset.name -replace "\.tar\.gz$", ""
    $destDir = Join-Path $root ".pyruntime\$destName"
    $pythonExe = Join-Path $destDir "python\python.exe"

    if (-not (Test-Path $pythonExe)) {
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null
        $archive = Join-Path $env:TEMP "localscribe-python.tar.gz"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive -TimeoutSec 180
        # tar.exe ships with Windows 10 1803+ / Windows 11 and extracts .tar.gz natively.
        tar -xzf $archive -C $destDir
        Remove-Item -Force $archive -ErrorAction SilentlyContinue
    }

    if (Test-Path $pythonExe) {
        Write-Output $pythonExe
    } else {
        exit 1
    }
} catch {
    exit 1
}
