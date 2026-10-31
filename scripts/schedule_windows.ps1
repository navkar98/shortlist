<#
.SYNOPSIS
  Register (or remove) the Windows scheduled task that runs Shortlist.

.DESCRIPTION
  The task starts when you log on and then checks again every -CheckEveryMinutes while you stay logged on.
  Each check runs `python -m shortlist run --if-due`, which does a real run only when the limits in
  config.yaml allow it (schedule.max_runs_per_day, schedule.min_hours_between_runs); otherwise it logs
  "not due" and exits within a second. Output is appended to shortlist.log in the project folder.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
  powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -CheckEveryMinutes 30
  powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -Remove
#>
param(
    [int]$CheckEveryMinutes = 60,
    [string]$TaskName = "Shortlist",
    [string]$Config = "",
    [switch]$Remove
)
$ErrorActionPreference = "Stop"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed scheduled task '$TaskName'."
    return
}

$project = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $project ".venv\Scripts\pythonw.exe"  # no console window, so nothing pops up or can be closed
if (-not $Config) { $Config = Join-Path $project "config.yaml" }
$log = Join-Path $project "shortlist.log"
foreach ($path in @($pythonw, $Config)) {
    if (-not (Test-Path $path)) { throw "Not found: $path" }
}

$action = New-ScheduledTaskAction -Execute $pythonw -WorkingDirectory $project `
    -Argument "-m shortlist run --if-due --config `"$Config`" --log-file `"$log`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# Repeat the logon trigger with no end date; the app's --if-due check decides whether a check becomes a real run.
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $CheckEveryMinutes)
$trigger.Repetition = $repeat.Repetition
# Priority 5 = normal. The default (7) also lowers disk priority, and on a busy disk the run then stalls for
# minutes just loading Python (seen with antivirus and search indexing busy).
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew -Priority 5
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description "Shortlist: checks at logon and every $CheckEveryMinutes min; runs as allowed by config.yaml schedule" |
    Out-Null
Write-Output "Registered '$TaskName': checks at logon and every $CheckEveryMinutes minutes; output goes to $log"
