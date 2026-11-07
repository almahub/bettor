param(
  [ValidateSet('restart','update')][string]$Mode,
  [string]$HomeDirectory,
  [string]$AppExecutable,
  [int]$Port,
  [int]$WindowProcessId
)
$ErrorActionPreference = 'Stop'
try {
  $WindowProcess = Get-Process -Id $WindowProcessId -ErrorAction SilentlyContinue
  if ($WindowProcess) { Wait-Process -Id $WindowProcessId -Timeout 60 }
  # The launcher checks installation identity, checkpoint activity and graceful shutdown.
  $StopArguments = '--data-dir "' + $HomeDirectory + '" --port ' + $Port + ' --stop'
  $Stopped = Start-Process -FilePath $AppExecutable -ArgumentList $StopArguments -WindowStyle Hidden -Wait -PassThru
  if ($Stopped.ExitCode -ne 0) { throw 'Arresto controllato non completato. Attendi la conclusione dei checkpoint e riapri Bettor.' }
  if ($Mode -eq 'update') {
    if (Test-Path -LiteralPath (Join-Path $HomeDirectory 'restore-pending.json')) {
      throw 'Completa o annulla il ripristino pendente prima di installare un aggiornamento.'
    }
    $Folder = Join-Path $HomeDirectory 'updates'
    $Ready = Get-Content -LiteralPath (Join-Path $Folder 'ready.json') -Raw | ConvertFrom-Json
    if ($Ready.version -notmatch '^\d+\.\d+\.\d+$' -or
        $Ready.filename -notmatch '^(Bettor|Better)-Setup-\d+\.\d+\.\d+-x64\.exe$' -or
        $Ready.sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Aggiornamento non verificato.' }
    if ($Ready.filename -ne "Bettor-Setup-$($Ready.version)-x64.exe" -and
        $Ready.filename -ne "Better-Setup-$($Ready.version)-x64.exe") { throw 'Versione setup non coerente.' }
    $Setup = Join-Path $Folder $Ready.filename
    if ((Get-FileHash -LiteralPath $Setup -Algorithm SHA256).Hash -ne $Ready.sha256) { throw 'Integrità setup non verificata.' }
    $TargetDirectory = Split-Path -Parent $AppExecutable
    $InstallerArguments = '/SP- /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR="' + $TargetDirectory + '"'
    $Install = Start-Process -FilePath $Setup -ArgumentList $InstallerArguments -WindowStyle Hidden -Wait -PassThru
    if ($Install.ExitCode -ne 0) { throw 'Installazione non completata. I dati sono conservati: riapri Bettor e verifica.' }
    Remove-Item -LiteralPath (Join-Path $Folder 'ready.json') -ErrorAction SilentlyContinue
  }
  $Arguments = '--data-dir "' + $HomeDirectory + '" --port ' + $Port
  # The helper is hidden; the interactive application must be visible again.
  Start-Process -FilePath $AppExecutable -ArgumentList $Arguments -WindowStyle Normal
} catch {
  Add-Type -AssemblyName System.Windows.Forms
  [System.Windows.Forms.MessageBox]::Show('Riavvio/aggiornamento non completato. Nessun ripristino viene applicato a backend attivo. Riapri Bettor; se erano in corso analisi o checkpoint, attendi e riprova.','Bettor') | Out-Null
} finally {
  Remove-Item -LiteralPath $PSCommandPath -ErrorAction SilentlyContinue
}
