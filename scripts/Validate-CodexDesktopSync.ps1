param(
    [switch]$CloseCodex,
    [switch]$RunSync,
    [switch]$RunRepair,
    [switch]$LaunchCodex,
    [int]$WaitSeconds = 25,
    [switch]$CaptureScreenshot,
    [switch]$StateOnly,
    [string[]]$TargetProject = @()
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$oneDrive = $env:OneDriveConsumer
if (-not $oneDrive) { $oneDrive = $env:OneDrive }
if (-not $oneDrive) { throw "Neither OneDriveConsumer nor OneDrive is set." }

$outputDir = Join-Path $oneDrive "Codex\StateSync\desktop-ui-checks"
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
$summaryPath = Join-Path $outputDir "codex-validation-summary.json"
$screenshotPath = Join-Path $outputDir "codex-sidebar.png"

function Stop-CodexDesktop {
    Get-Process Codex,codex -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 3
}

function Find-CodexAppId {
    $app = Get-StartApps | Where-Object { $_.Name -eq "Codex" -or $_.AppID -like "OpenAI.Codex*!App" } | Select-Object -First 1
    if ($app -and $app.AppID) { return $app.AppID }
    throw "Could not find the Codex desktop AppID in Get-StartApps."
}

function Invoke-InteractiveTask {
    param(
        [string]$TaskName,
        [string]$Command
    )
    $startTime = (Get-Date).AddMinutes(1).ToString("HH:mm")
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    schtasks.exe /Delete /TN $TaskName /F *> $null
    $ErrorActionPreference = $previousErrorActionPreference
    $create = schtasks.exe /Create /TN $TaskName /TR $Command /SC ONCE /ST $startTime /RL LIMITED /IT /F
    if ($LASTEXITCODE -ne 0) { throw "Failed to create GUI launch task: $create" }
    $run = schtasks.exe /Run /TN $TaskName
    if ($LASTEXITCODE -ne 0) { throw "Failed to run GUI launch task: $run" }
}

function Start-CodexInGui {
    $appId = Find-CodexAppId
    Invoke-InteractiveTask -TaskName "CodexDesktopSyncLaunch" -Command "explorer.exe shell:AppsFolder\$appId"
    Start-Sleep -Seconds $WaitSeconds
    return $appId
}

function Capture-ScreenPng {
    param([string]$Path)
    $captureScript = Join-Path $outputDir "capture-codex-sidebar.ps1"
    @"
`$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition '
using System;
using System.Runtime.InteropServices;
public class Win32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
}
'
`$shell = New-Object -ComObject Shell.Application
`$shell.MinimizeAll()
Start-Sleep -Milliseconds 500
`$codex = Get-Process -ErrorAction SilentlyContinue | Where-Object { `$_.ProcessName -match "Codex|codex" -and `$_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (`$codex) {
    [Win32]::ShowWindowAsync(`$codex.MainWindowHandle, 3) | Out-Null
    [Win32]::SetForegroundWindow(`$codex.MainWindowHandle) | Out-Null
    Start-Sleep -Seconds 2
}
`$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
`$bitmap = New-Object System.Drawing.Bitmap `$bounds.Width, `$bounds.Height
`$graphics = [System.Drawing.Graphics]::FromImage(`$bitmap)
try {
    `$graphics.CopyFromScreen(`$bounds.Location, [System.Drawing.Point]::Empty, `$bounds.Size)
    `$bitmap.Save("$Path", [System.Drawing.Imaging.ImageFormat]::Png)
}
finally {
    `$graphics.Dispose()
    `$bitmap.Dispose()
}
"@ | Set-Content -Path $captureScript -Encoding UTF8

    if (Test-Path $Path) { Remove-Item -LiteralPath $Path -Force }
    Invoke-InteractiveTask -TaskName "CodexDesktopSyncScreenshot" -Command "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$captureScript`""
    $deadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $deadline) {
        if ((Test-Path $Path) -and ((Get-Item $Path).Length -gt 10000)) {
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "Screenshot was not captured to $Path."
}

$actions = [ordered]@{
    closed = $false
    sync_ran = $false
    repair_ran = $false
    launched = $false
    codex_exe = $null
    screenshot_captured = $false
}

if ($CloseCodex) {
    Stop-CodexDesktop
    $actions.closed = $true
}

if ($RunSync) {
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scriptDir "Sync-CodexDesktopFromLaptop.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Sync-CodexDesktopFromLaptop.ps1 failed with exit code $LASTEXITCODE." }
    $actions.sync_ran = $true
}

if ($RunRepair) {
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scriptDir "Repair-CodexDesktopFromLaptop.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Repair-CodexDesktopFromLaptop.ps1 failed with exit code $LASTEXITCODE." }
    $actions.repair_ran = $true
}

if ($LaunchCodex -and -not $StateOnly) {
    $actions.codex_exe = Start-CodexInGui
    $actions.launched = $true
}

if ($CaptureScreenshot -and -not $StateOnly) {
    Start-Sleep -Seconds 5
    Capture-ScreenPng -Path $screenshotPath
    $actions.screenshot_captured = $true
}

$validatorArgs = @((Join-Path $scriptDir "codex_desktop_validate.py"), "--output-dir", $outputDir)
foreach ($project in $TargetProject) {
    $validatorArgs += @("--target-project", $project)
}
$validatorOutput = & py @validatorArgs
$validatorExit = $LASTEXITCODE
$stateReportPath = Join-Path $outputDir "codex-state-report.json"
$stateReport = Get-Content -Path $stateReportPath -Raw | ConvertFrom-Json

$summary = [ordered]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    actions = $actions
    output_dir = $outputDir
    screenshot = $screenshotPath
    state_report = $stateReportPath
    simulated_sidebar_report = (Join-Path $outputDir "codex-sidebar-simulated.txt")
    validator_exit = $validatorExit
    validator_output = ($validatorOutput -join [Environment]::NewLine)
    pass = $stateReport.pass
    target_projects = $stateReport.target_projects
}

$summary | ConvertTo-Json -Depth 12 | Set-Content -Path $summaryPath -Encoding UTF8
$summary | ConvertTo-Json -Depth 12

if (-not $stateReport.pass.overall_without_visual_ocr) {
    exit 2
}
