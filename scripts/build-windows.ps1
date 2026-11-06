param([string]$Iscc = "ISCC.exe")
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Push-Location $Root
try {
  $Python = Join-Path $Root ".cache/packaging-venv/Scripts/python.exe"
  if (!(Test-Path $Python)) { throw "Prepara ambiente build: docs/windows-distribution.md" }
  & $Python -m PyInstaller --noconfirm --clean --workpath .cache/pyinstaller packaging/Better.spec
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller fallito" }
  $Version = & $Python -c "from better import __version__; print(__version__)"
  & $Iscc "/DAppVersion=$Version" packaging/Better.iss
  if ($LASTEXITCODE -ne 0) { throw "Installer fallito" }
  $Installer = Join-Path $Root "dist/installer/Better-Setup-$Version-x64.exe"
  $Hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
  "$Hash  $(Split-Path $Installer -Leaf)" | Set-Content -Encoding ascii "$Installer.sha256"
} finally { Pop-Location }
