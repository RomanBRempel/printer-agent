param(
    [string]$OutputDir = (Join-Path $PSScriptRoot "dist")
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
$uninstallLauncherPy = (Resolve-Path (Join-Path $PSScriptRoot "uninstall_launcher.py")).Path
$uninstallPs1 = (Resolve-Path (Join-Path $PSScriptRoot "uninstall.ps1")).Path
$iconPath = Join-Path $PSScriptRoot "printer-agent.ico"

if (-not (Test-Path $pythonExe)) {
    throw "Python executable was not found at $pythonExe"
}

& $pythonExe -m pip install --upgrade pyinstaller --no-input

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$workDir = Join-Path $PSScriptRoot "build"
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

# Console subsystem on purpose: removal is a scripted operation with no window
# of its own. The install path is a real GUI (build-gui-installer-exe.ps1).
$arguments = @(
    "--noconfirm", "--clean", "--onefile",
    "--name", "printer-agent-uninstaller",
    "--distpath", $OutputDir,
    "--workpath", $workDir,
    "--specpath", $workDir,
    "--add-data", "$uninstallPs1;."
)
if (Test-Path $iconPath) {
    $arguments += @("--icon", $iconPath, "--add-data", "$iconPath;.")
}

& $pythonExe -m PyInstaller @arguments "$uninstallLauncherPy"

Write-Host "Built uninstaller: $(Join-Path $OutputDir 'printer-agent-uninstaller.exe')"
