<#
.SYNOPSIS
    Register a Windows scheduled task that starts the dry run at a given time.

    Unlike dry-run.ps1 -At, this survives the console being closed and will
    still fire if you are away from the machine. The task runs once; re-run
    this script to schedule another.

.PARAMETER At
    Local start time, e.g. "20:10".

.EXAMPLE
    .\scripts\schedule-dry-run.ps1 -At "20:10"
    Get-ScheduledTask -TaskName OddsWatcherDryRun      # check it exists
    Unregister-ScheduledTask -TaskName OddsWatcherDryRun -Confirm:$false
#>
param(
    [Parameter(Mandatory = $true)][string]$At,
    [string]$TaskName = "OddsWatcherDryRun"
)

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "scripts\dry-run.ps1"

$start = Get-Date $At
if ((Get-Date) -gt $start) { $start = $start.AddDays(1) }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At $start
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null

Write-Host "Scheduled '$TaskName' for $($start.ToString('yyyy-MM-dd HH:mm'))."
Write-Host "The machine must be awake at that time; sleep will delay it."
