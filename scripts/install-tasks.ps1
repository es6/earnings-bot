<#
.SYNOPSIS
Registers, or updates, the "Earnings Bot" and "Earnings Bot Watchdog" scheduled tasks.

.DESCRIPTION
Both tasks run scripts/run-bot.ps1 and scripts/run-watchdog.ps1 as the signed-in Windows user,
only while that user is signed in, because Chrome needs the desktop session.

Each task starts at logon and again every few minutes. A supervisor that's already running
ignores the extra start: the task doesn't start a second instance, and each script holds its
own lock. So a logon that starts nothing, a supervisor that dies or a closed window costs at
most those few minutes. On Nov 13, 2026 the PC restarted at 02:04 ET. The logon at 02:07
started neither task, and the bot was down until 15:12.

Each task runs through `conhost.exe --headless`, so it has no window. Windows Terminal is this
PC's default terminal, and it opened a plain powershell.exe as a tab in the user's window,
ignoring -WindowStyle Hidden. Closing that window stopped the bot twice on Nov 9.

Running the script again replaces both definitions. A bot that's already running keeps going
under the old definition until it restarts: `.\scripts\update-bot.ps1 -Restart` moves it, once
no release window is open.

    .\scripts\install-tasks.ps1            register or update both tasks
    .\scripts\install-tasks.ps1 -WhatIf    show what it would register, and change nothing
    .\scripts\install-tasks.ps1 -Force     update them even while a release window is open
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "Earnings Bot",
    [string]$WatchdogTaskName = "Earnings Bot Watchdog",
    [ValidateRange(1, 60)]
    [int]$EveryMinutes = 5,
    [string]$ScheduleDirectory = "schedules",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
# What python -m earnings_bot.restart exits with when a restart now would interrupt a release
$windowOpen = 3

function Test-ReleaseWindows {
    # Changing a running task's definition shouldn't stop it, but don't find out mid-release
    $uv = (Get-Command uv -CommandType Application).Source
    $env:PYTHONUTF8 = "1"
    Push-Location -LiteralPath $repo
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $uv run --no-sync python -m earnings_bot.restart $ScheduleDirectory 2>&1 |
            ForEach-Object { "$_" })
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -eq $windowOpen) {
        Write-Host ("Not changing the tasks while a release could be missed:`n  " +
            ($output -join "`n  ") + "`nRun again after the window, or add -Force.") -ForegroundColor Red
        exit 1
    }
    if ($code -ne 0) {
        Write-Host "Couldn't check the release windows:`n$($output -join "`n")" -ForegroundColor Red
        exit 1
    }
}

function Register-Supervisor([string]$Name, [string]$Script, [string]$Description) {
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $conhost = Join-Path $env:SystemRoot "System32\conhost.exe"
    $path = Join-Path $repo "scripts\$Script"
    $arguments = "--headless `"$powershell`" -NoProfile -NonInteractive " +
        "-ExecutionPolicy RemoteSigned -File `"$path`""
    $action = New-ScheduledTaskAction -Execute $conhost -Argument $arguments -WorkingDirectory $repo
    $triggers = @(
        New-ScheduledTaskTrigger -AtLogOn -User $user
        # No duration: it repeats for as long as the task exists
        New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
            -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
    )
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable
    if ($PSCmdlet.ShouldProcess("$Name ($conhost $arguments)", "Register at logon and every $EveryMinutes min")) {
        Register-ScheduledTask -TaskName $Name -Description $Description -Action $action `
            -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
        Write-Host "Registered '$Name': at logon and every $EveryMinutes min, without a window." `
            -ForegroundColor Green
    }
}

if (-not $Force -and -not $WhatIfPreference) {
    Test-ReleaseWindows
}
Register-Supervisor $TaskName "run-bot.ps1" `
    "Watch earnings schedules and post alerts in the signed-in Windows session."
Register-Supervisor $WatchdogTaskName "run-watchdog.ps1" `
    "Alarm in Discord when a release window is open and its routes stop polling."
if (-not $WhatIfPreference) {
    Write-Host ("A bot or watchdog already running keeps its old definition until it restarts. " +
        "To move both now: .\scripts\update-bot.ps1 -Restart")
}
