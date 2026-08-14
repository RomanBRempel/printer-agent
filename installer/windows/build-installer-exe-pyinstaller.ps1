param(
    [string]$OutputDir = (Join-Path $PSScriptRoot "dist")
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
$installLauncherPy = (Resolve-Path (Join-Path $PSScriptRoot "install_launcher.py")).Path
$uninstallLauncherPy = (Resolve-Path (Join-Path $PSScriptRoot "uninstall_launcher.py")).Path
$installPs1 = (Resolve-Path (Join-Path $PSScriptRoot "install.ps1")).Path
$uninstallPs1 = (Resolve-Path (Join-Path $PSScriptRoot "uninstall.ps1")).Path

if (-not (Test-Path $pythonExe)) {
    throw "Python executable was not found at $pythonExe"
}

& $pythonExe -m pip install --upgrade pyinstaller

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$workDir = Join-Path $PSScriptRoot "build"
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

& $pythonExe -m PyInstaller --noconfirm --clean --onefile --name printer-agent-installer --distpath $OutputDir --workpath $workDir --specpath $workDir --add-data "$installPs1;." "$installLauncherPy"
& $pythonExe -m PyInstaller --noconfirm --clean --onefile --name printer-agent-uninstaller --distpath $OutputDir --workpath $workDir --specpath $workDir --add-data "$uninstallPs1;." "$uninstallLauncherPy"

Write-Host "Built installer: $(Join-Path $OutputDir 'printer-agent-installer.exe')"
Write-Host "Built uninstaller: $(Join-Path $OutputDir 'printer-agent-uninstaller.exe')"
