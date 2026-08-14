param(
    [string]$OutputDir = (Join-Path $PSScriptRoot "dist")
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
$guiInstallerPy = (Resolve-Path (Join-Path $PSScriptRoot "gui_installer.py")).Path
$installPs1 = (Resolve-Path (Join-Path $PSScriptRoot "install.ps1")).Path
$iconPath = Join-Path $PSScriptRoot "printer-agent.ico"

if (-not (Test-Path $pythonExe)) {
    throw "Python executable was not found at $pythonExe"
}

& $pythonExe -m pip install --upgrade pyinstaller --no-input

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$workDir = Join-Path $PSScriptRoot "build"
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

if (Test-Path $iconPath) {
    & $pythonExe -m PyInstaller --noconfirm --clean --windowed --onefile --name printer-agent-installer-gui --distpath $OutputDir --workpath $workDir --specpath $workDir --icon $iconPath --add-data "$installPs1;." --add-data "$iconPath;." "$guiInstallerPy"
}
else {
    & $pythonExe -m PyInstaller --noconfirm --clean --windowed --onefile --name printer-agent-installer-gui --distpath $OutputDir --workpath $workDir --specpath $workDir --add-data "$installPs1;." "$guiInstallerPy"
}

Write-Host "Built GUI installer: $(Join-Path $OutputDir 'printer-agent-installer-gui.exe')"
