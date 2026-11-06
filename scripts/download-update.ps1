param([string]$Destination = "$env:USERPROFILE\Downloads\Bettor")
$ErrorActionPreference = "Stop"
$Release = Invoke-RestMethod -Uri 'https://api.github.com/repos/almahub/bettor/releases/latest'
$Setups = @($Release.assets | Where-Object { $_.name -match '^Bettor-Setup-[0-9]+\.[0-9]+\.[0-9]+-x64\.exe$' })
if ($Setups.Count -eq 0) {
  $Setups = @($Release.assets | Where-Object { $_.name -match '^Better-Setup-[0-9]+\.[0-9]+\.[0-9]+-x64\.exe$' })
}
if ($Setups.Count -ne 1) { throw 'Release priva di un setup univoco' }
$Setup = $Setups[0]
$Checks = @($Release.assets | Where-Object { $_.name -eq ($Setup.name + '.sha256') })
if ($Checks.Count -ne 1) { throw 'Checksum mancante o ambiguo' }
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
foreach ($Asset in @($Setup, $Checks[0])) {
  $Uri = [Uri]$Asset.browser_download_url
  if ($Uri.Scheme -ne 'https' -or $Uri.Host -ne 'github.com' -or !$Uri.AbsolutePath.StartsWith('/almahub/bettor/releases/download/')) { throw 'Origine download non consentita' }
  Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile (Join-Path $Destination $Asset.name)
}
$File = Join-Path $Destination $Setup.name
$Expected = ((Get-Content -LiteralPath ($File + '.sha256') -Raw).Trim() -split '\s+')[0]
if ($Expected -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash -ne $Expected) {
  Remove-Item -LiteralPath $File
  throw 'Integrità setup non verificata: download rimosso'
}
Write-Output "Setup verificato: $File"
Write-Output 'Nessuna installazione automatica. Leggi le note della release e avvia il setup quando Bettor è fermo.'
Write-Output $Release.html_url
