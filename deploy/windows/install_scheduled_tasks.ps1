#Requires -RunAsAdministrator
<#
  Register ONE Windows Scheduled Task: BNB-Autopilot
  (embeds paper watcher — same as 启动服务器.bat)

  API keys: config.yaml
#>

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

$py = $null
$cfg = Join-Path $Root "config.yaml"
if (Test-Path $cfg) {
    $line = Select-String -Path $cfg -Pattern "python_path:\s*(.+)" | Select-Object -First 1
    if ($line) {
        $candidate = ($line.Matches[0].Groups[1].Value -replace '"', '').Trim()
        if (Test-Path $candidate) { $py = $candidate }
    }
}
if (-not $py) {
    foreach ($c in @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "C:\Python312\python.exe",
        "C:\Program Files\Python312\python.exe"
    )) {
        if (Test-Path $c) { $py = $c; break }
    }
}
if (-not $py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py) { throw "Python not found. Set web.python_path in config.yaml" }

New-Item -ItemType Directory -Force -Path (Join-Path $Root "data\logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root "data\locks") | Out-Null

foreach ($old in @("BNB-Autopilot", "BNB-PaperWatcher")) {
    Unregister-ScheduledTask -TaskName $old -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute $py -Argument "`"$Root\autopilot_daemon.py`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask `
    -TaskName "BNB-Autopilot" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "BNB Quant one-process server (analysis + watcher)" `
    -Force | Out-Null

Start-ScheduledTask -TaskName "BNB-Autopilot"
Write-Host "OK  BNB-Autopilot  ->  $py autopilot_daemon.py"
Write-Host "Log: $Root\data\logs\autopilot.log"
Write-Host "Stop: Stop-ScheduledTask BNB-Autopilot"
