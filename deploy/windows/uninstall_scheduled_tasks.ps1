#Requires -RunAsAdministrator
$ErrorActionPreference = "Continue"
foreach ($name in @("BNB-Autopilot", "BNB-PaperWatcher")) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed $name"
}
