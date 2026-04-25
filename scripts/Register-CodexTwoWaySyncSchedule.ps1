param(
    [Parameter(Mandatory = $true)]
    [string]$DesktopHost,
    [Parameter(Mandatory = $true)]
    [string]$DesktopUser,
    [Parameter(Mandatory = $true)]
    [string]$DesktopHostKey,
    [string]$RemoteScriptDir = "",
    [string]$RemoteStateSyncDir = "",
    [string]$DesktopPassword = "",
    [string]$TaskName = "Codex Two-Way State Sync",
    [string]$StartTime = "03:00"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$oneDrive = $env:OneDriveConsumer
if (-not $oneDrive) { $oneDrive = $env:OneDrive }
if (-not $oneDrive) { throw "Neither OneDriveConsumer nor OneDrive is set." }

$credentialDir = Join-Path $oneDrive "Codex\StateSync\credentials"
New-Item -ItemType Directory -Force -Path $credentialDir | Out-Null
$credentialPath = Join-Path $credentialDir "desktop-ssh.credential.xml"

if ($DesktopPassword) {
    $secure = ConvertTo-SecureString $DesktopPassword -AsPlainText -Force
    $credential = [pscredential]::new($DesktopUser, $secure)
    $credential | Export-Clixml -Path $credentialPath
} elseif (-not (Test-Path $credentialPath)) {
    throw "No desktop credential exists yet. Re-run with -DesktopPassword once to create $credentialPath."
}

$syncScript = Join-Path $scriptDir "Invoke-CodexTwoWaySync.ps1"
$argument = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$syncScript`"",
    "-Mode", "Full",
    "-DesktopHost", $DesktopHost,
    "-DesktopUser", $DesktopUser,
    "-DesktopHostKey", $DesktopHostKey,
    "-CredentialPath", "`"$credentialPath`"",
    "-UseStageOffsets"
) -join " "

if ($RemoteScriptDir) {
    $argument += " -RemoteScriptDir `"$RemoteScriptDir`""
}
if ($RemoteStateSyncDir) {
    $argument += " -RemoteStateSyncDir `"$RemoteStateSyncDir`""
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Daily Codex Desktop two-way state sync with backups, mediator merge, repair, and validation." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
