param([string]$Iscc = "ISCC.exe")
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Push-Location $Root
try {
  $Python = Join-Path $Root ".cache/packaging-venv/Scripts/python.exe"
  if (!(Test-Path $Python)) { throw "Prepara ambiente build: docs/windows-distribution.md" }
  # PyInstaller writes progress to stderr. Windows PowerShell 5 treats that as
  # a terminating error with ErrorActionPreference=Stop; inspect the exit code.
  $Build = Start-Process -FilePath $Python -ArgumentList @('-m', 'PyInstaller', '--noconfirm', '--clean', '--workpath', '.cache/pyinstaller', 'packaging/Better.spec') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput '.cache/windows-build.out.log' -RedirectStandardError '.cache/windows-build.err.log'
  if ($Build.ExitCode -ne 0) { throw "PyInstaller fallito: .cache/windows-build.err.log" }
  $Version = & $Python -c "from better import __version__; print(__version__)"
  $Compile = Start-Process -FilePath $Iscc -ArgumentList @("/DAppVersion=$Version", 'packaging/Better.iss') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput '.cache/windows-installer.out.log' -RedirectStandardError '.cache/windows-installer.err.log'
  if ($Compile.ExitCode -ne 0) { throw "Installer fallito: .cache/windows-installer.err.log" }
  $Installer = Join-Path $Root "dist/installer/Better-Setup-$Version-x64.exe"
  $Hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
  "$Hash  $(Split-Path $Installer -Leaf)" | Set-Content -Encoding ascii "$Installer.sha256"
  # Preserve the legacy release asset name for existing update clients.
  $BrandedInstaller = Join-Path $Root "dist/installer/Bettor-Setup-$Version-x64.exe"
  Copy-Item -LiteralPath $Installer -Destination $BrandedInstaller -Force
  "$Hash  $(Split-Path $BrandedInstaller -Leaf)" | Set-Content -Encoding ascii "$BrandedInstaller.sha256"
} finally { Pop-Location }
