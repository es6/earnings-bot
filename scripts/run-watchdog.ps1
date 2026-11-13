param(
    [string]$ScheduleDirectory = "schedules",
    [int]$RestartSeconds = 15
)

# Keeps the watchdog (earnings_bot.watchdog) running apart from the bot, for its own scheduled
# task: restarts it after any exit and writes its output to recordings/watchdog-<date>.log.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$mutex = [System.Threading.Mutex]::new($false, "Global\EarningsBotWatchdog")
$ownsLock = $false
Push-Location -LiteralPath $repo
try {
    try {
        $ownsLock = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $ownsLock = $true
    }
    if (-not $ownsLock) {
        throw "An earnings-bot watchdog is already running on this PC."
    }
    $schedule = (Resolve-Path -LiteralPath $ScheduleDirectory).Path
    $uv = (Get-Command uv -CommandType Application).Source
    $logs = Join-Path $repo "recordings"
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    # As in run-bot.ps1: cmd.exe appends Python's UTF-8 output to the log byte for byte
    $env:PYTHONUTF8 = "1"
    $env:EARNINGS_BOT_UV = $uv
    $env:EARNINGS_BOT_SCHEDULE = $schedule
    while ($true) {
        $log = Join-Path $logs ("watchdog-" + (Get-Date -Format "yyyy-MM-dd") + ".log")
        $env:EARNINGS_BOT_LOG = $log
        [System.IO.File]::AppendAllText($log, "[$(Get-Date -Format o)] Starting the watchdog`r`n", $utf8)
        # --no-sync: the running bot holds the environment's files
        cmd.exe --% /d /s /c ""%EARNINGS_BOT_UV%" run --no-sync python -m earnings_bot.watchdog "%EARNINGS_BOT_SCHEDULE%" >> "%EARNINGS_BOT_LOG%" 2>&1"
        $code = $LASTEXITCODE
        [System.IO.File]::AppendAllText($log, "[$(Get-Date -Format o)] Watchdog exited ($code); restarting`r`n", $utf8)
        Start-Sleep -Seconds $RestartSeconds
    }
} finally {
    if ($ownsLock) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
    Pop-Location
}
