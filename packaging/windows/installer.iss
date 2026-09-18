; Inno Setup script for the Local Scribe Windows installer.
; Built via build.ps1, which stages app/build.ps1's output under BuildDir
; and invokes: iscc.exe /DAppVersion=... /DBuildDir=... /DOutputDir=... installer.iss
;
; Installs per-user (no admin required - PrivilegesRequired=lowest), so
; there is no UAC/permission prompt to grant beyond the one-time
; SmartScreen "Windows protected your PC" warning an unsigned .exe shows,
; documented in the README, until code signing is set up.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef BuildDir
  #define BuildDir "build"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist\windows"
#endif

[Setup]
AppId={{6C9F0E7E-6B2C-4B7E-9C7A-2C1B7E9F5A3D}
AppName=Local Scribe
AppVersion={#AppVersion}
AppPublisher=UIC TRAILblazer Lab
AppPublisherURL=https://www.trailblazerlab.org
DefaultDirName={localappdata}\Programs\Local Scribe
DefaultGroupName=Local Scribe
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=LocalScribe-{#AppVersion}-Windows-x86_64
Compression=lzma2
SolidCompression=yes
SetupIconFile={#BuildDir}\app\app\static\icon\favicon.ico
UninstallDisplayIcon={app}\app\app\static\icon\favicon.ico

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Local Scribe"; Filename: "{app}\LocalScribe.bat"; \
    IconFilename: "{app}\app\app\static\icon\favicon.ico"; WorkingDir: "{app}"
Name: "{commondesktop}\Local Scribe"; Filename: "{app}\LocalScribe.bat"; \
    IconFilename: "{app}\app\app\static\icon\favicon.ico"; WorkingDir: "{app}"; \
    Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\LocalScribe.bat"; Description: "Launch Local Scribe now"; \
    Flags: nowait postinstall skipifsilent
