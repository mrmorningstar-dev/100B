<#
.SYNOPSIS
    Installs / reconfigures the "100B Daily Video Bot" scheduled task in Windows Task Scheduler.

.DESCRIPTION
    Re-creates the daily Windows Scheduled Task to run at 07:00 AM IST (India Standard Time).
    Computes all paths dynamically from script location (never hardcoded paths).
    Reads task configuration (task_name and time_ist) from agent/config.yaml.
#>

[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# 1. Verify Local Machine Timezone
$localTz = [TimeZoneInfo]::Local.Id
Write-Host "Verifying machine timezone: $localTz"
if ($localTz -ne "India Standard Time") {
    Write-Error "CRITICAL: Machine timezone is '$localTz', not 'India Standard Time'. The 07:00 AM IST daily trigger requires an IST system clock."
    exit 1
}

# 2. Determine Project Root Dynamically
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
Write-Host "Project Root Directory: $ProjectRoot"

if (-not (Test-Path "$ProjectRoot\run_daily.py")) {
    Write-Error "Cannot find run_daily.py at $ProjectRoot. Invalid project directory."
    exit 1
}

# 3. Read Task Configuration from agent/config.yaml
$ConfigPath = "$ProjectRoot\agent\config.yaml"
$TaskName = "100B Daily Video Bot"
$TimeIST = "07:00"

if (Test-Path $ConfigPath) {
    try {
        $yamlContent = Get-Content $ConfigPath -Raw
        if ($yamlContent -match 'task_name:\s*["'']?([^"''\r\n]+)["'']?') {
            $TaskName = $matches[1].Trim()
        }
        if ($yamlContent -match 'time_ist:\s*["'']?([^"''\r\n]+)["'']?') {
            $TimeIST = $matches[1].Trim()
        }
    } catch {
        Write-Warning "Could not parse agent/config.yaml; using defaults ($TaskName at $TimeIST IST)."
    }
}
Write-Host "Configured Daily Task: '$TaskName' (primary time: $TimeIST IST)"

# 3.5 Backup existing task to XML if present
$BackupDir = "$ProjectRoot\backups"
if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
}
$BackupFile = "$BackupDir\100B_Daily_Video_Bot_backup.xml"
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    try {
        Export-ScheduledTask -TaskName $TaskName | Out-File -FilePath $BackupFile -Encoding utf8
        Write-Host "Backed up existing task XML to: $BackupFile"
    } catch {
        Write-Warning "Could not export existing task XML: $_"
    }
}

# 4. Resolve Python Executable
$PythonExe = ""
try {
    $PythonExe = (& python -c "import sys; print(sys.executable)").Trim()
} catch {
    $PythonExe = ""
}

if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $PythonExe = $cmd.Source
    }
}

if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    Write-Error "Could not locate a valid python.exe executable."
    exit 1
}
Write-Host "Python Executable: $PythonExe"

# 5. Define Task Action
# Arguments: run_daily.py --date ist-today
$ActionArgs = "run_daily.py --date ist-today"
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $ActionArgs -WorkingDirectory $ProjectRoot

# 6. Define Task Triggers
# Daily triggers at 07:00, 10:00, 13:00, 16:00, 19:00 IST for retry & pipeline resilience
$TriggerTimes = @("07:00", "10:00", "13:00", "16:00", "19:00")
$Triggers = @()
foreach ($t in $TriggerTimes) {
    $timeObj = [datetime]::ParseExact($t, "HH:mm", [System.Globalization.CultureInfo]::InvariantCulture)
    $Triggers += New-ScheduledTaskTrigger -Daily -At ($timeObj.ToString("HH:mm"))
}

# 7. Define Task Settings
# - StartWhenAvailable
# - MultipleInstances IgnoreNew (do not start second copy if running)
# - ExecutionTimeLimit 3 hours
# - RestartCount 2, RestartInterval 15 minutes
# - Run on battery allowed (AllowStartIfOnBatteries, DontStopIfGoingOnBatteries)
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# 8. Register Task
Write-Host "Registering scheduled task '$TaskName' with 5 daily triggers ($($TriggerTimes -join ', ') IST)..."
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Triggers `
    -Settings $Settings `
    -Description "100B Automated Video Bot daily master run (curation, upload, verify, claims sync) at 07:00, 10:00, 13:00, 16:00, 19:00 IST" `
    -Force | Out-Null

Write-Host "Successfully registered scheduled task '$TaskName'!"
Write-Host "Action: $PythonExe $ActionArgs"
Write-Host "Working Directory: $ProjectRoot"

# Show scheduled task info
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "`nTask Details:"
Write-Host "  Last Run Time  : $($info.LastRunTime)"
Write-Host "  Next Run Time  : $($info.NextRunTime)"
Write-Host "  Last Result    : $($info.LastTaskResult)"
