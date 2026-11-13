<#
.SYNOPSIS
Puts the latest main on the running bot in one command.

.DESCRIPTION
Fetches main. If it has new commits, the script checks that no release window is open or about
to open with its alert still unposted, stops the bot, fast-forwards this checkout to main and
starts the "Earnings Bot" scheduled task again. It finishes when the bot says monitoring has
started. If the bot isn't running, it starts it. Then it restarts the "Earnings Bot Watchdog"
task, which runs from this checkout too, so the watchdog runs the same code as the bot.

    .\scripts\update-bot.ps1            update and restart if main has something new
    .\scripts\update-bot.ps1 -Wait      if a window is open, wait for it, then restart
    .\scripts\update-bot.ps1 -Restart   restart even when main has nothing new
    .\scripts\update-bot.ps1 -Force     restart even while a window is open
#>
param(
    [switch]$Wait,
    [switch]$Restart,
    [switch]$Force,
    [string]$ScheduleDirectory = "schedules",
    [string]$TaskName = "Earnings Bot",
    [string]$WatchdogTaskName = "Earnings Bot Watchdog",
    [int]$StartTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
# What python -m earnings_bot.restart exits with when a restart now would interrupt a release
$windowOpen = 3

function Fail([string]$Message) {
    Write-Host $Message -ForegroundColor Red
    exit 1
}

function Invoke-Native([string]$Command, [string[]]$Arguments) {
    # Windows PowerShell turns a native command's stderr into errors, which "Stop" makes fatal
    $ErrorActionPreference = "Continue"
    $lines = @(& $Command @Arguments 2>&1 | ForEach-Object { "$_" })
    [pscustomobject]@{ Code = $LASTEXITCODE; Output = $lines }
}

function Invoke-Git {
    $result = Invoke-Native "git.exe" $args
    if ($result.Code -ne 0) {
        Fail "git $($args -join ' ') failed:`n$($result.Output -join "`n")"
    }
    $result.Output
}

function Get-BotProcesses {
    # The supervisor, the cmd.exe and uv.exe it starts, and the bot's own processes. Matched on
    # what they run, so other tools working in this folder are left alone.
    @(Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq "powershell.exe" -and $_.CommandLine -match "run-bot\.ps1") -or
        (($_.Name -in "cmd.exe", "uv.exe") -and $_.CommandLine -match "run --locked earnings-bot ") -or
        (($_.Name -in "python.exe", "earnings-bot.exe") -and
            $_.CommandLine -match "\\Scripts\\earnings-bot\.exe")
    })
}

function Wait-ForWindows {
    # Returns whether it had to wait
    $uv = (Get-Command uv -CommandType Application).Source
    $env:PYTHONUTF8 = "1"
    $waited = $false
    while ($true) {
        # --no-sync: the running bot holds the environment's files
        $check = Invoke-Native $uv @(
            "run", "--no-sync", "python", "-m", "earnings_bot.restart", $ScheduleDirectory
        )
        if ($check.Code -eq 0) {
            $check.Output | ForEach-Object { Write-Host $_ }
            return $waited
        }
        if ($check.Code -ne $windowOpen) {
            Fail "Couldn't check the release windows:`n$($check.Output -join "`n")"
        }
        $reasons = "  " + ($check.Output -join "`n  ")
        if (-not $Wait) {
            Fail ("Not restarting, because a restart now could miss a release:`n$reasons`n" +
                "Run this again later, add -Wait to restart as soon as it's safe, or add -Force " +
                "to restart anyway.")
        }
        if (-not $waited) {
            Write-Host "Waiting to restart, because:`n$reasons"
            $waited = $true
        }
        Start-Sleep -Seconds 30
    }
}

function Stop-Bot {
    $supervisors = Get-BotProcesses | Where-Object { $_.Name -eq "powershell.exe" }
    foreach ($supervisor in $supervisors) {
        Invoke-Native "taskkill" @("/PID", "$($supervisor.ProcessId)", "/T", "/F") | Out-Null
    }
    # Anything left, such as a copy started by hand: one Discord token can only run one bot
    $deadline = (Get-Date).AddSeconds(15)
    while (($left = Get-BotProcesses).Count -gt 0) {
        if ((Get-Date) -gt $deadline) {
            $names = $left | ForEach-Object { "  $($_.ProcessId) $($_.Name)" }
            Fail "These bot processes didn't stop:`n$($names -join "`n")"
        }
        $left | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 500
    }
    # Task Scheduler ignores a start while it still counts the task as running
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running") {
        if ((Get-Date) -gt $deadline) {
            Fail "Task Scheduler still shows '$TaskName' running after the bot stopped."
        }
        Start-Sleep -Milliseconds 500
    }
}

function Read-LogFrom([string]$Path, [long]$Offset) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    # The bot keeps appending to the log, so share it for writing
    $stream = [System.IO.File]::Open($Path, "Open", "Read", "ReadWrite")
    try {
        [void]$stream.Seek($Offset, "Begin")
        return [System.IO.StreamReader]::new($stream, [System.Text.UTF8Encoding]::new($false)).ReadToEnd()
    } finally {
        $stream.Dispose()
    }
}

function Start-Bot {
    $log = Join-Path $repo ("recordings\runner-" + (Get-Date -Format "yyyy-MM-dd") + ".log")
    $offset = 0
    if (Test-Path -LiteralPath $log) {
        $offset = (Get-Item -LiteralPath $log).Length
    }
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started the '$TaskName' task. Waiting for the bot to come up..."
    $deadline = (Get-Date).AddSeconds($StartTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        $text = Read-LogFrom $log $offset
        if ($text -match "Earnings monitoring started" -or $text -match "Bot exited \(") {
            $text.TrimEnd() -split "`r?`n" | Select-Object -Last 20 | ForEach-Object { Write-Host "  $_" }
            if ($text -match "Earnings monitoring started") {
                Write-Host "The bot is running on $(Invoke-Git rev-parse --short HEAD)." -ForegroundColor Green
                return
            }
            Fail "The bot exited while starting. The supervisor will keep retrying; see $log"
        }
    }
    Fail "The bot didn't say it started within $StartTimeoutSeconds s. See $log"
}

function Restart-Watchdog {
    # The watchdog keeps running the code it started with, so it restarts with the bot
    if (-not (Get-ScheduledTask -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue)) {
        Write-Host "There's no '$WatchdogTaskName' task, so no watchdog was restarted." -ForegroundColor Yellow
        return
    }
    $supervisors = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "powershell.exe" -and $_.CommandLine -match "run-watchdog\.ps1"
    })
    foreach ($supervisor in $supervisors) {
        Invoke-Native "taskkill" @("/PID", "$($supervisor.ProcessId)", "/T", "/F") | Out-Null
    }
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-ScheduledTask -TaskName $WatchdogTaskName).State -eq "Running") {
        if ((Get-Date) -gt $deadline) {
            Fail "Task Scheduler still shows '$WatchdogTaskName' running after the watchdog stopped."
        }
        Start-Sleep -Milliseconds 500
    }
    Start-ScheduledTask -TaskName $WatchdogTaskName
    Write-Host "Restarted the watchdog on $(Invoke-Git rev-parse --short HEAD)." -ForegroundColor Green
}

Push-Location -LiteralPath $repo
try {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Fail "There's no '$TaskName' task in Task Scheduler. See the README to set it up."
    }
    $branch = Invoke-Git rev-parse --abbrev-ref HEAD
    if ($branch -ne "main") {
        Fail "This checkout is on '$branch'. The bot runs whatever is checked out, so switch back to main first."
    }
    $changed = Invoke-Git status --porcelain --untracked-files=no
    if ($changed) {
        Fail "These files have uncommitted changes, so nothing was updated:`n$($changed -join "`n")"
    }
    Invoke-Git fetch --quiet origin main | Out-Null
    if ((Invoke-Native "git.exe" @("merge-base", "--is-ancestor", "HEAD", "origin/main")).Code -ne 0) {
        Fail "This checkout has commits that aren't on main, so it can't move to main by itself."
    }
    $current = Invoke-Git rev-parse --short HEAD
    $new = @(Invoke-Git log --oneline HEAD..origin/main)
    $running = (Get-BotProcesses).Count -gt 0

    if ($new.Count -eq 0 -and $running -and -not $Restart) {
        Write-Host "Already on the latest main ($current), and the bot is running." -ForegroundColor Green
        exit 0
    }
    if ($running -and -not $Force -and (Wait-ForWindows)) {
        # Main may have moved while this waited
        Invoke-Git fetch --quiet origin main | Out-Null
        $new = @(Invoke-Git log --oneline HEAD..origin/main)
    }
    if ($running) {
        Write-Host "Stopping the bot..."
        Stop-Bot
    } else {
        Write-Host "The bot isn't running, so this starts it."
    }
    if ($new.Count -gt 0) {
        Invoke-Git merge --ff-only --quiet origin/main | Out-Null
        Write-Host "Updated $current to $(Invoke-Git rev-parse --short HEAD):"
        $new | ForEach-Object { Write-Host "  $_" }
    }
    Start-Bot
    Restart-Watchdog
} finally {
    Pop-Location
}
