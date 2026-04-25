param(
    [ValidateSet("Full", "ExportMergeOnly")]
    [string]$Mode = "Full",
    [Parameter(Mandatory = $true)]
    [string]$DesktopHost,
    [Parameter(Mandatory = $true)]
    [string]$DesktopUser,
    [Parameter(Mandatory = $true)]
    [string]$DesktopHostKey,
    [string]$RemoteScriptDir = "",
    [string]$RemoteStateSyncDir = "",
    [string]$DesktopPassword = "",
    [string]$CredentialPath = "",
    [switch]$UseStageOffsets,
    [switch]$NoCloseLocalForTest,
    [switch]$SkipDesktopVisualValidation
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$oneDrive = $env:OneDriveConsumer
if (-not $oneDrive) { $oneDrive = $env:OneDrive }
if (-not $oneDrive) { throw "Neither OneDriveConsumer nor OneDrive is set." }

$runStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$runRoot = Join-Path $oneDrive "Codex\StateSync\two-way-runs\$runStamp"
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
$logPath = Join-Path $runRoot "run.log"
$summaryPath = Join-Path $runRoot "summary.json"
$lockPath = Join-Path $oneDrive "Codex\StateSync\two-way-sync.lock"

function Write-RunLog {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date).ToString("s"), $Message
    $line | Tee-Object -FilePath $logPath -Append
}

function Get-DesktopPassword {
    if ($DesktopPassword) { return $DesktopPassword }
    if ($CredentialPath -and (Test-Path $CredentialPath)) {
        $credential = Import-Clixml -Path $CredentialPath
        return $credential.GetNetworkCredential().Password
    }
    if ($env:CODEX_DESKTOP_SSH_PASSWORD) { return $env:CODEX_DESKTOP_SSH_PASSWORD }
    throw "Desktop password was not supplied. Pass -DesktopPassword, -CredentialPath, or set CODEX_DESKTOP_SSH_PASSWORD."
}

function Get-PuttyTool {
    param([string]$Name)
    $path = Join-Path $env:ProgramFiles "PuTTY\$Name"
    if (Test-Path $path) { return $path }
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Could not find $Name."
}

$plink = Get-PuttyTool "plink.exe"
$pscp = Get-PuttyTool "pscp.exe"
$password = Get-DesktopPassword
if (-not $RemoteScriptDir) {
    $RemoteScriptDir = "C:\Users\$DesktopUser\OneDrive\Codex\Python_scripts\codex_workspace_sync_setup"
}
if (-not $RemoteStateSyncDir) {
    $RemoteStateSyncDir = "C:\Users\$DesktopUser\OneDrive\Codex\StateSync"
}

function ConvertTo-PscpPath {
    param([string]$WindowsPath)
    if ($WindowsPath -match "^([A-Za-z]):\\(.*)$") {
        return "/$($Matches[1]):/$($Matches[2].Replace('\', '/'))"
    }
    return $WindowsPath.Replace('\', '/')
}

function Invoke-Desktop {
    param([string]$Command)
    $output = & $plink -ssh -batch -hostkey $DesktopHostKey -pw $password "$DesktopUser@$DesktopHost" $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Remote command failed with exit code $LASTEXITCODE`: $Command`n$output"
    }
    return $output
}

function Invoke-DesktopPowerShell {
    param([string]$Script)
    $wrapped = "`$ProgressPreference = 'SilentlyContinue'; `$ErrorActionPreference = 'Continue'; $Script"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($wrapped))
    Invoke-Desktop "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
}

function Copy-ToDesktop {
    param([string]$LocalPath, [string]$RemotePath)
    $remote = "{0}@{1}:{2}" -f $DesktopUser, $DesktopHost, (ConvertTo-PscpPath $RemotePath)
    $output = & $pscp -batch -hostkey $DesktopHostKey -pw $password -r $LocalPath $remote
    if ($LASTEXITCODE -ne 0) {
        throw "Copy to desktop failed with exit code $LASTEXITCODE`: $LocalPath -> $RemotePath`n$output"
    }
}

function Copy-FromDesktop {
    param([string]$RemotePath, [string]$LocalPath)
    $remote = "{0}@{1}:{2}" -f $DesktopUser, $DesktopHost, (ConvertTo-PscpPath $RemotePath)
    $output = & $pscp -batch -hostkey $DesktopHostKey -pw $password -r $remote $LocalPath
    if ($LASTEXITCODE -ne 0) {
        throw "Copy from desktop failed with exit code $LASTEXITCODE`: $RemotePath -> $LocalPath`n$output"
    }
}

function Invoke-LocalPyJson {
    param([string[]]$PyArgs)
    $output = & py @PyArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE`: py $PyArgs`n$output"
    }
    return (($output -join [Environment]::NewLine) | ConvertFrom-Json)
}

function Invoke-RemotePyJson {
    param([string]$PyArgs)
    $output = Invoke-DesktopPowerShell "Set-Location '$remoteScriptDir'; py .\codex_state_sync.py $PyArgs"
    return (($output -join [Environment]::NewLine) | ConvertFrom-Json)
}

function Wait-Stage {
    param([string]$Name, [int]$OffsetMinutes)
    if ($UseStageOffsets) {
        $target = (Get-Date).Date.AddHours(3).AddMinutes($OffsetMinutes)
        if ((Get-Date) -gt $target.AddMinutes(2)) {
            $target = $target.AddDays(1)
        }
        Write-RunLog "Waiting for stage '$Name' until $($target.ToString('s'))."
        while ((Get-Date) -lt $target) {
            Start-Sleep -Seconds ([Math]::Min(60, [Math]::Max(1, [int]($target - (Get-Date)).TotalSeconds)))
        }
    }
    Write-RunLog "Starting stage '$Name'."
}

if (Test-Path $lockPath) {
    $existing = Get-Content -Path $lockPath -Raw -ErrorAction SilentlyContinue
    throw "Sync lock already exists: $lockPath`n$existing"
}

$summary = [ordered]@{
    run_stamp = $runStamp
    mode = $Mode
    run_root = $runRoot
    stages = @()
    pass = $false
}

try {
    "pid=$PID`nstarted=$(Get-Date -Format o)`nrun_root=$runRoot" | Set-Content -Path $lockPath -Encoding UTF8

    Wait-Stage -Name "deploy-scripts" -OffsetMinutes 0
    foreach ($file in @(
        "codex_state_sync.py",
        "codex_desktop_validate.py",
        "Import-CodexMergedState.ps1",
        "Validate-CodexDesktopSync.ps1"
    )) {
        Copy-ToDesktop -LocalPath (Join-Path $scriptDir $file) -RemotePath $remoteScriptDir
    }
    $summary.stages += @{ name = "deploy-scripts"; pass = $true }

    Wait-Stage -Name "laptop-export" -OffsetMinutes 0
    if (-not $NoCloseLocalForTest) {
        Get-Process Codex,codex -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Seconds 3
    } else {
        Write-RunLog "Skipping local Codex close for interactive test run."
    }
    $laptopExport = Invoke-LocalPyJson -PyArgs @(".\codex_state_sync.py", "export", "--source-name", "laptop")
    $summary.stages += @{ name = "laptop-export"; pass = $true; package = $laptopExport.package }

    Wait-Stage -Name "desktop-export" -OffsetMinutes 10
    Invoke-DesktopPowerShell "taskkill.exe /IM Codex.exe /F /T 2>`$null; taskkill.exe /IM codex.exe /F /T 2>`$null; exit 0"
    Start-Sleep -Seconds 3
    $desktopExport = Invoke-RemotePyJson -PyArgs "export --source-name desktop"
    $summary.stages += @{ name = "desktop-export"; pass = $true; package = $desktopExport.package }

    Wait-Stage -Name "transfer-packages" -OffsetMinutes 15
    $remoteLaptopParent = Join-Path $RemoteStateSyncDir "source-laptop"
    Invoke-DesktopPowerShell "New-Item -ItemType Directory -Force -Path '$remoteLaptopParent' | Out-Null"
    Copy-ToDesktop -LocalPath $laptopExport.package -RemotePath $remoteLaptopParent

    $localDesktopParent = Join-Path $oneDrive "Codex\StateSync\source-desktop"
    New-Item -ItemType Directory -Force -Path $localDesktopParent | Out-Null
    Copy-FromDesktop -RemotePath $desktopExport.package -LocalPath $localDesktopParent
    $desktopPackageLocal = Join-Path $localDesktopParent (Split-Path -Leaf $desktopExport.package)
    $summary.stages += @{ name = "transfer-packages"; pass = $true; desktop_package_local = $desktopPackageLocal }

    Wait-Stage -Name "mediator-merge" -OffsetMinutes 20
    $merged = Invoke-LocalPyJson -PyArgs @(
        ".\codex_state_sync.py",
        "merge-packages",
        "--left-source", "laptop",
        "--right-source", "desktop",
        "--output-source", "merged",
        "--left-package", $laptopExport.package,
        "--right-package", $desktopPackageLocal
    )
    $summary.stages += @{
        name = "mediator-merge"
        pass = $true
        package = $merged.package
        selected_by_source = $merged.manifest.selected_by_source
        conflicts = @($merged.manifest.conflicts_resolved_latest_wins).Count
    }

    Wait-Stage -Name "transfer-merged" -OffsetMinutes 25
    $remoteMergedParent = Join-Path $RemoteStateSyncDir "source-merged"
    Invoke-DesktopPowerShell "New-Item -ItemType Directory -Force -Path '$remoteMergedParent' | Out-Null"
    Copy-ToDesktop -LocalPath $merged.package -RemotePath $remoteMergedParent
    $summary.stages += @{ name = "transfer-merged"; pass = $true }

    if ($Mode -eq "Full") {
        Wait-Stage -Name "laptop-import" -OffsetMinutes 30
        powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scriptDir "Import-CodexMergedState.ps1") -SourceName "merged"
        if ($LASTEXITCODE -ne 0) { throw "Laptop merged import failed with exit code $LASTEXITCODE." }
        $summary.stages += @{ name = "laptop-import"; pass = $true }

        Wait-Stage -Name "desktop-import-validate" -OffsetMinutes 45
        $remoteValidate = "& '$remoteScriptDir\Validate-CodexDesktopSync.ps1' -CloseCodex -LaunchCodex -WaitSeconds 55 -CaptureScreenshot"
        if ($SkipDesktopVisualValidation) {
            $remoteValidate = "& '$remoteScriptDir\Import-CodexMergedState.ps1' -SourceName merged"
        } else {
            $remoteValidate = "& '$remoteScriptDir\Import-CodexMergedState.ps1' -SourceName merged; & '$remoteScriptDir\Validate-CodexDesktopSync.ps1' -LaunchCodex -WaitSeconds 55 -CaptureScreenshot"
        }
        $desktopImportOutput = Invoke-DesktopPowerShell $remoteValidate
        $summary.stages += @{ name = "desktop-import-validate"; pass = $true; output_tail = ($desktopImportOutput | Select-Object -Last 20) }
    }

    $summary.pass = $true
    $summary.completed_at = (Get-Date).ToUniversalTime().ToString("o")
    $summary | ConvertTo-Json -Depth 12 | Set-Content -Path $summaryPath -Encoding UTF8
    Write-RunLog "Sync completed. Summary: $summaryPath"
    $summary | ConvertTo-Json -Depth 12
}
catch {
    $summary.pass = $false
    $summary.error = $_.Exception.Message
    $summary.completed_at = (Get-Date).ToUniversalTime().ToString("o")
    $summary | ConvertTo-Json -Depth 12 | Set-Content -Path $summaryPath -Encoding UTF8
    Write-RunLog "Sync failed: $($_.Exception.Message)"
    throw
}
finally {
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
}
