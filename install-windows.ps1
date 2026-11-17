param(
    [string]$TaskName = "MediaPlayerCompanionBridge",
    [string]$VenvDir = "$env:LOCALAPPDATA\mediaplayer-companion-bridge\venv",
    [string]$CacheDir = "$env:LOCALAPPDATA\mediaplayer-companion-bridge\cache",
    [switch]$SkipFirewall,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ProjectDir "config.json"
$TemplatePath = Join-Path $ProjectDir "config.template.json"
$ReqPath = Join-Path $ProjectDir "requirements.txt"
$AppPath = Join-Path $ProjectDir "app.py"

if (-not (Test-Path $ConfigPath) -and (Test-Path $TemplatePath)) {
    Copy-Item $TemplatePath $ConfigPath
    Write-Host "Created config.json from config.template.json"
}

$Port = 5002
if (Test-Path $ConfigPath) {
    try {
        $cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
        if ($cfg.port) { $Port = [int]$cfg.port }
    } catch {
        Write-Warning "Could not parse config.json port; using default $Port"
    }
}

Write-Host "Using local venv: $VenvDir"
# Stop any running scheduled task or pythonw/python instance before updating venv
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*mediaplayer-companion-bridge*app.py*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

if (-not (Test-Path $VenvDir)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $VenvDir) | Out-Null
    python -m venv $VenvDir
}

$PipExe = Join-Path $VenvDir "Scripts\pip.exe"
$PythonwExe = Join-Path $VenvDir "Scripts\pythonw.exe"
if (-not (Test-Path $PythonwExe)) {
    $PythonwExe = Join-Path $VenvDir "Scripts\python.exe"
}

& $PipExe install --upgrade pip | Out-Null
& $PipExe install -r $ReqPath

New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

if (-not $SkipFirewall) {
    $RuleName = "MediaPlayer Companion Bridge (TCP $Port)"
    try {
        $existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
        if (-not $existing) {
            New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
            Write-Host "Added Windows Firewall rule: $RuleName"
        } else {
            Write-Host "Windows Firewall rule already exists: $RuleName"
        }
    } catch {
        Write-Warning "Could not configure Windows Firewall (run PowerShell as Administrator to add TCP $Port rule automatically)."
    }
}

# Run pythonw.exe directly in the interactive user session (required by Windows IDesktopWallpaper COM)
$PyArgs = "`"$AppPath`" --cache-dir `"$CacheDir`""
$Action = New-ScheduledTaskAction -Execute $PythonwExe -Argument $PyArgs -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
Write-Host "Registered Scheduled Task '$TaskName' (AtLogOn, Interactive session, port $Port)"

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started Scheduled Task '$TaskName'. Web UI: http://localhost:$Port/ui"
}
