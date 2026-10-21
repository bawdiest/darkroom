# Baut die Desktop-App für Windows: dist\Darkroom\Darkroom.exe und dist\Darkroom-<Version>-windows-<arch>.zip
# Die Architektur ergibt sich aus dem Python, das gebaut wird:
#   x64   (Python 3.12 x64)    -> läuft auf Intel/AMD und auf ARM-Rechnern per Emulation, mit OpenCV und Objektivkorrektur
#   arm64 (Python 3.12 ARM64)  -> nativ auf Windows on ARM, ohne OpenCV/lensfunpy (Ersatz in darkroom\cvx.py)
# Aufruf aus dem Hauptordner:
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1                      (Python aus dem Pfad)
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1 -Python "C:\...\python.exe"
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$plat = (& $Python -c "import sysconfig; print(sysconfig.get_platform())").Trim()
$arch = if ($plat -eq "win-arm64") { "arm64" } elseif ($plat -eq "win-amd64") { "x64" } else { throw "Unbekannte Plattform: $plat (gebraucht wird Windows-Python 3.12)" }
Write-Host "Baue Windows $arch ($plat)"

$venv = ".venv-build-$arch"
if (-not (Test-Path $venv)) { & $Python -m venv $venv }
$py = Join-Path $venv "Scripts\python.exe"
$req = if ($arch -eq "arm64") { "desktop\requirements-win-arm64.txt" } else { "requirements.txt" }
& $py -m pip install --upgrade pip
& $py -m pip install -r $req -r desktop\requirements-desktop.txt
if ($LASTEXITCODE -ne 0) { throw "Installation der Pakete fehlgeschlagen" }
if (Test-Path dist\Darkroom) { Remove-Item -Recurse -Force dist\Darkroom }
& $py -m PyInstaller desktop\darkroom.spec --noconfirm --distpath dist --workpath "build\pyi-$arch"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller ist fehlgeschlagen" }

# Prüfung: startet der gebaute Server?
$smoke = Join-Path $env:TEMP "darkroom-smoke-$arch"
if (Test-Path $smoke) { Remove-Item -Recurse -Force $smoke }
$p = Start-Process -FilePath "dist\Darkroom\Darkroom.exe" -ArgumentList "--smoke-test","--library","$smoke\lib","--data","$smoke\app" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "Smoke-Test fehlgeschlagen (Exit $($p.ExitCode))" }
Write-Host "Smoke-Test ok"

$version = (Select-String -Path darkroom\__init__.py -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$zip = "dist\Darkroom-$version-windows-$arch.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\Darkroom -DestinationPath $zip
Write-Host "Fertig: $zip"

# Optional: Installer (Inno Setup, https://jrsoftware.org/isinfo.php), wenn iscc im Pfad liegt
if (Get-Command iscc -ErrorAction SilentlyContinue) {
    iscc /DAppVersion=$version /DAppArch=$arch desktop\installer.iss
    Write-Host "Installer: dist\Darkroom-$version-windows-$arch-setup.exe"
}
