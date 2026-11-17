param(
    [string]$TaskName = "MediaPlayerCompanionBridge",
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ProjectDir "config.json"

if ($Port -le 0) {
    $Port = 5002
    if (Test-Path $ConfigPath) {
        try {
            $cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
            if ($cfg.port) { $Port = [int]$cfg.port }
        } catch {
            # Use default port 5002
        }
    }
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed Scheduled Task '$TaskName'"
} else {
    Write-Host "Scheduled Task '$TaskName' not found"
}

$stopped = 0
Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*mediaplayer-companion-bridge*app.py*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped++
    }
if ($stopped -gt 0) {
    Write-Host "Stopped $stopped running MediaPlayer Companion Bridge process(es)"
}

$RuleName = "MediaPlayer Companion Bridge (TCP $Port)"
try {
    $rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    if ($rule) {
        Remove-NetFirewallRule -DisplayName $RuleName
        Write-Host "Removed Windows Firewall rule '$RuleName'"
    }
} catch {
    Write-Warning "Could not remove Windows Firewall rule '$RuleName' (run as Administrator to remove it)."
}
