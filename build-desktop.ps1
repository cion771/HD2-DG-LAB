$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $venv = Join-Path $PSScriptRoot 'build/desktop-venv'
    $python = Join-Path $venv 'Scripts/python.exe'
    if (-not (Test-Path $python)) {
        py -3 -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create build environment. Install Python 3.10+.' }
    }
    & $python -m pip install -r requirements-desktop.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $python -m PyInstaller --noconfirm --clean desktop.spec
    if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed.' }
    Get-FileHash 'dist/HD2-DG-LAB.exe' -Algorithm SHA256
    Write-Host 'Created dist/HD2-DG-LAB.exe. No config or runtime logs are bundled.'
} finally {
    Pop-Location
}
