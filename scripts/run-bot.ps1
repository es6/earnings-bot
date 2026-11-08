param(
    [string]$ScheduleDirectory = "schedules",
    [int]$RestartSeconds = 15
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$mutex = [System.Threading.Mutex]::new($false, "Global\EarningsBotRunner")
$ownsLock = $false
Push-Location -LiteralPath $repo
try {
    try {
        $ownsLock = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $ownsLock = $true
    }
    if (-not $ownsLock) {
        throw "An earnings-bot supervisor is already running on this PC."
    }
    $schedule = (Resolve-Path -LiteralPath $ScheduleDirectory).Path
    if (-not (Test-Path -LiteralPath $schedule -PathType Container)) {
        throw "ScheduleDirectory must be a directory."
    }
    $uv = (Get-Command uv -CommandType Application).Source
    $logs = Join-Path $repo "recordings"
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    # Python writes UTF-8, and cmd.exe appends its output to the log byte for byte. PowerShell's
    # own redirection would write UTF-16 into a file that also gets these UTF-8 lines, and wrap
    # every line on stderr in an error record.
    $env:PYTHONUTF8 = "1"
    $env:EARNINGS_BOT_UV = $uv
    $env:EARNINGS_BOT_SCHEDULE = $schedule
    while ($true) {
        $log = Join-Path $logs ("runner-" + (Get-Date -Format "yyyy-MM-dd") + ".log")
        $env:EARNINGS_BOT_LOG = $log
        [System.IO.File]::AppendAllText($log, "[$(Get-Date -Format o)] Starting earnings-bot`r`n", $utf8)
        # --% passes the rest to cmd.exe as written, with %VARIABLES% from the environment
        cmd.exe --% /d /s /c ""%EARNINGS_BOT_UV%" run --locked earnings-bot "%EARNINGS_BOT_SCHEDULE%" >> "%EARNINGS_BOT_LOG%" 2>&1"
        $code = $LASTEXITCODE
        [System.IO.File]::AppendAllText($log, "[$(Get-Date -Format o)] Bot exited ($code); restarting`r`n", $utf8)
        Start-Sleep -Seconds $RestartSeconds
    }
} finally {
    if ($ownsLock) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
    Pop-Location
}
