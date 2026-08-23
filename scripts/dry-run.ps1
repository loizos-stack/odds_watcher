<#
.SYNOPSIS
    Run the watcher in dry-run mode, logging to a file as well as the screen.

.PARAMETER At
    Optional local start time, e.g. "20:10". Waits until then before starting.
    A time already past today is taken as tomorrow.

.PARAMETER LogPath
    Where to write the log. Defaults to watcher.log in the repository root.

.EXAMPLE
    .\scripts\dry-run.ps1
    .\scripts\dry-run.ps1 -At "20:10"
#>
param(
    [string]$At = "",
    [string]$LogPath = "watcher.log",
    [string]$LogLevel = "DEBUG"
)

Set-Location (Split-Path -Parent $PSScriptRoot)

if ($At -ne "") {
    $start = Get-Date $At
    if ((Get-Date) -gt $start) { $start = $start.AddDays(1) }
    $wait = [int]($start - (Get-Date)).TotalSeconds
    Write-Host "Waiting until $($start.ToString('HH:mm')) ($wait seconds). Ctrl+C to cancel."
    Start-Sleep -Seconds $wait
}

Write-Host "Starting dry run, logging to $LogPath. Ctrl+C to stop."
py -m odds_watcher run --dry-run --log-level $LogLevel *>&1 | Tee-Object -FilePath $LogPath
