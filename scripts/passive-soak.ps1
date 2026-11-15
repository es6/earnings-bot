param(
    [ValidateRange(1,24)][int]$Hours = 4,
    [ValidateRange(5,60)][int]$EverySeconds = 30,
    [Parameter(Mandatory=$true)][string]$OutPath
)
$ErrorActionPreference = 'Stop'
$outPath = [System.IO.Path]::GetFullPath($OutPath)
[System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($outPath)) | Out-Null
# Never overwrite earlier evidence; use a new filename for each run.
[System.IO.File]::Open($outPath, [System.IO.FileMode]::CreateNew).Dispose()
$endAt = [DateTime]::UtcNow.AddHours($Hours)
@{ event='started'; utc=[DateTime]::UtcNow.ToString('o'); ends_utc=$endAt.ToString('o'); interval_seconds=$EverySeconds; scope='passive existing bot and owned Chrome; no release load injected' } | ConvertTo-Json -Compress | Add-Content -Encoding utf8 -LiteralPath $outPath
while ([DateTime]::UtcNow -lt $endAt) {
    try {
        $inventory = @(Get-CimInstance Win32_Process)
        $bots = @($inventory | Where-Object { $_.Name -eq 'earnings-bot.exe' -or ($_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match '-m\s+earnings_bot(?:\s|$)') })
        $ownedIds = @($bots | ForEach-Object { [int]$_.ProcessId })
        $ownedChrome = @($inventory | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -match 'earnings-bot-chrome-' })
        $ownedIds += @($ownedChrome | ForEach-Object { [int]$_.ProcessId })
        do {
            $children = @($inventory | Where-Object { $_.ParentProcessId -in $ownedIds -and $_.ProcessId -notin $ownedIds })
            $ownedIds += @($children | ForEach-Object { [int]$_.ProcessId })
        } while ($children.Count -gt 0)
        $samples = @($inventory | Where-Object { $_.ProcessId -in $ownedIds } | ForEach-Object {
            @{ pid=[int]$_.ProcessId; parent=[int]$_.ParentProcessId; name=$_.Name; started=$_.CreationDate.ToUniversalTime().ToString('o'); working_bytes=[long]$_.WorkingSetSize; private_bytes=[long]$_.PrivatePageCount; handles=[int]$_.HandleCount; cpu_seconds=([double]$_.KernelModeTime+[double]$_.UserModeTime)/10000000 }
        })
        $os = Get-CimInstance Win32_OperatingSystem
        @{ event='sample'; utc=[DateTime]::UtcNow.ToString('o'); bot_count=$bots.Count; free_memory_bytes=[long]$os.FreePhysicalMemory*1024; processes=$samples } | ConvertTo-Json -Depth 5 -Compress | Add-Content -Encoding utf8 -LiteralPath $outPath
    } catch {
        @{ event='sample_error'; utc=[DateTime]::UtcNow.ToString('o'); error_type=$_.Exception.GetType().Name } | ConvertTo-Json -Compress | Add-Content -Encoding utf8 -LiteralPath $outPath
    }
    Start-Sleep -Seconds $EverySeconds
}
@{ event='finished'; utc=[DateTime]::UtcNow.ToString('o') } | ConvertTo-Json -Compress | Add-Content -Encoding utf8 -LiteralPath $outPath
