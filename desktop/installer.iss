; Inno Setup (https://jrsoftware.org/isinfo.php): Installer für Windows. Aufruf: iscc /DAppVersion=0.1.0 desktop\installer.iss
; Installiert ohne Administratorrechte in das Benutzerprofil. Nach dem PyInstaller-Build (dist\Darkroom\) ausführen.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6E6A2F4C-3B5E-4C77-9B0A-0D4A7D3C5E11}
AppName=Darkroom
AppVersion={#AppVersion}
AppPublisher=mq-data
DefaultDirName={localappdata}\Programs\Darkroom
DefaultGroupName=Darkroom
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Darkroom-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\Darkroom.exe

[Files]
Source: "..\dist\Darkroom\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Darkroom"; Filename: "{app}\Darkroom.exe"
Name: "{autodesktop}\Darkroom"; Filename: "{app}\Darkroom.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Symbol auf dem Desktop anlegen"; Flags: unchecked

[Run]
Filename: "{app}\Darkroom.exe"; Description: "Darkroom starten"; Flags: nowait postinstall skipifsilent

; Fotos und Einstellungen liegen in %APPDATA%\Darkroom und im Ordner «Bilder\Darkroom»; sie bleiben beim Deinstallieren erhalten.
