param(
    [string]$OutputDir = (Join-Path $PSScriptRoot "dist")
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
$sourceRoot = Join-Path $repoRoot "src"
$guiInstallerPy = (Resolve-Path (Join-Path $PSScriptRoot "gui_installer.py")).Path
$installPs1 = (Resolve-Path (Join-Path $PSScriptRoot "install.ps1")).Path
$iconPath = Join-Path $PSScriptRoot "printer-agent.ico"

if (-not (Test-Path $pythonExe)) {
    throw "Python executable was not found at $pythonExe"
}

& $pythonExe -m pip install --upgrade pyinstaller --no-input
# The installer window is built on the same Qt toolkit as the desktop app.
& $pythonExe -m pip install "PySide6-Essentials>=6.7,<7" --no-input
& $pythonExe -m pip install --upgrade build --no-input

# Build the wheel and ship it inside the installer. Without this the installer
# falls back to the update feed, whose latest release is older than the code
# being packaged here — which is how an install ends up running the previous UI.
$wheelDir = Join-Path $repoRoot "dist"
Get-ChildItem -Path $wheelDir -Filter "printer_agent-*.whl" -ErrorAction SilentlyContinue | Remove-Item -Force
& $pythonExe -m build --wheel --outdir $wheelDir
$wheel = Get-ChildItem -Path $wheelDir -Filter "printer_agent-*.whl" |
Sort-Object LastWriteTime -Descending |
Select-Object -First 1
if ($null -eq $wheel) {
    throw "Wheel build produced no printer_agent-*.whl in $wheelDir"
}
Write-Host "Bundling wheel: $($wheel.Name)"

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$workDir = Join-Path $PSScriptRoot "build"
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

# Qt subsystems the installer never touches. Left in, they roughly double the
# bundle for no benefit.
$excluded = @(
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore",
    "PySide6.QtMultimedia", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtOpenGL", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtPdf"
)
$excludeArgs = @()
foreach ($module in $excluded) {
    $excludeArgs += @("--exclude-module", $module)
}

$commonArgs = @(
    "--noconfirm", "--clean", "--windowed", "--onefile",
    "--name", "printer-agent-installer",
    "--distpath", $OutputDir,
    "--workpath", $workDir,
    "--specpath", $workDir,
    # gui_installer imports the desktop app's theme so both look identical.
    "--paths", $sourceRoot,
    "--add-data", "$installPs1;.",
    "--add-data", "$($wheel.FullName);."
)

if (Test-Path $iconPath) {
    $commonArgs += @("--icon", $iconPath, "--add-data", "$iconPath;.")
}

& $pythonExe -m PyInstaller @commonArgs @excludeArgs "$guiInstallerPy"

Write-Host "Built installer: $(Join-Path $OutputDir 'printer-agent-installer.exe')"
