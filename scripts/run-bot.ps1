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
    while ($true) {
        $log = Join-Path $logs ("runner-" + (Get-Date -Format "yyyy-MM-dd") + ".log")
        Add-Content -LiteralPath $log -Value "[$(Get-Date -Format o)] Starting earnings-bot"
        $ErrorActionPreference = "Continue"
        & $uv run --locked earnings-bot $schedule *>> $log
        $code = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        Add-Content -LiteralPath $log -Value "[$(Get-Date -Format o)] Bot exited ($code); restarting"
        Start-Sleep -Seconds $RestartSeconds
    }
} finally {
    if ($ownsLock) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
    Pop-Location
}
