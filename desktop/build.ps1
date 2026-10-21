# Baut die Desktop-App für Windows: dist\Darkroom\Darkroom.exe (+ dist\Darkroom-<Version>-windows.zip)
# Voraussetzung: Python 3.12 (x64). Aufruf aus dem Hauptordner:  powershell -ExecutionPolicy Bypass -File desktop\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$venv = ".venv-build"
if (-not (Test-Path $venv)) { python -m venv $venv }
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt -r desktop\requirements-desktop.txt
if (Test-Path dist\Darkroom) { Remove-Item -Recurse -Force dist\Darkroom }
& $py -m PyInstaller desktop\darkroom.spec --noconfirm --distpath dist --workpath build\pyi
if ($LASTEXITCODE -ne 0) { throw "PyInstaller ist fehlgeschlagen" }

# Prüfung: startet der gebaute Server?
$p = Start-Process -FilePath "dist\Darkroom\Darkroom.exe" -ArgumentList "--smoke-test","--library","$env:TEMP\darkroom-smoke\lib","--data","$env:TEMP\darkroom-smoke\app" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "Smoke-Test fehlgeschlagen (Exit $($p.ExitCode))" }
Write-Host "Smoke-Test ok"

$version = (Select-String -Path darkroom\__init__.py -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$zip = "dist\Darkroom-$version-windows.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\Darkroom -DestinationPath $zip
Write-Host "Fertig: $zip"

# Optional: Installer (Inno Setup, https://jrsoftware.org/isinfo.php), wenn iscc im Pfad liegt
if (Get-Command iscc -ErrorAction SilentlyContinue) {
    iscc /DAppVersion=$version desktop\installer.iss
    Write-Host "Installer: dist\Darkroom-$version-setup.exe"
}
