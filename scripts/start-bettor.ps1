$ErrorActionPreference = "Stop"

$BettorRoot = "D:\bettor"
$DashboardUrl = "http://127.0.0.1:8001/"
$HealthUrl = "http://127.0.0.1:8001/health"
$Launcher = Join-Path $BettorRoot ".venv\Scripts\better.exe"
$EnvironmentFile = Join-Path $BettorRoot ".env"
$LogFile = Join-Path $BettorRoot "data\logs\888.jsonl"

function Show-LauncherError([string] $Message) {
    $shell = New-Object -ComObject WScript.Shell
    $null = $shell.Popup($Message, 0, "Avvio Bettor", 16)
}

function Test-BettorHealth {
    try {
        $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
        return $health.status -eq "ok"
    }
    catch {
        return $false
    }
}

try {
    if (Test-BettorHealth) {
        Start-Process $DashboardUrl
        exit 0
    }

    $portInUse = Test-NetConnection -ComputerName 127.0.0.1 -Port 8001 `
        -InformationLevel Quiet -WarningAction SilentlyContinue
    if ($portInUse) {
        throw "La porta 8001 e occupata da un processo che non risponde come Bettor."
    }

    if (-not (Test-Path -LiteralPath $Launcher)) {
        throw "Eseguibile Bettor non trovato: $Launcher"
    }

    $keyLine = Get-Content -LiteralPath $EnvironmentFile | Where-Object {
        $_ -match '^OPENAI_API_KEY='
    } | Select-Object -Last 1
    if (-not $keyLine) {
        throw "OPENAI_API_KEY non configurata nel file .env."
    }
    $runtimeKey = ($keyLine -split '=', 2)[1].Trim().Trim('"').Trim("'")
    if (-not $runtimeKey) {
        throw "OPENAI_API_KEY vuota nel file .env."
    }

    $previousKey = $env:OPENAI_API_KEY
    $env:OPENAI_API_KEY = $runtimeKey
    try {
        Start-Process -FilePath $Launcher `
            -ArgumentList '--config', 'config/888', '--log-file', $LogFile, 'serve', '--port', '8001' `
            -WorkingDirectory $BettorRoot `
            -WindowStyle Hidden
    }
    finally {
        if ($null -eq $previousKey) {
            Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
        }
        else {
            $env:OPENAI_API_KEY = $previousKey
        }
    }

    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 500
        if (Test-BettorHealth) {
            Start-Process $DashboardUrl
            exit 0
        }
    } while ((Get-Date) -lt $deadline)

    throw "Bettor non ha raggiunto health=ok entro 45 secondi. Controllare $LogFile"
}
catch {
    Show-LauncherError $_.Exception.Message
    exit 1
}
