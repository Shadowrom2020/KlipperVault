; Inno Setup script for KlipperVault
; Usage: iscc klippervault.iss with environment variables:
;   KV_VERSION        - application version (from VERSION file)
;   KV_APP_DIR        - path to KlipperVault.exe
;   KV_OUTPUT_DIR     - output directory for setup.exe

#ifndef KV_VERSION
#define KV_VERSION "0.0.0"
#endif

#ifndef KV_APP_DIR
#define KV_APP_DIR "dist\KlipperVault.exe"
#endif

#ifndef KV_OUTPUT_DIR
#define KV_OUTPUT_DIR "release\inno-setup-output"
#endif

[Setup]
AppName=KlipperVault
AppVersion={#KV_VERSION}
DefaultDirName={autopf}\KlipperVault
DefaultGroupName=KlipperVault
OutputDir={#KV_OUTPUT_DIR}
OutputBaseFilename=KlipperVault-{#KV_VERSION}-windows-x64
UninstallDisplayIcon={app}\KlipperVault.exe
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional tasks:"; Flags: unchecked
Name: "quicklaunchicon"; Description: "Create a &Quick Launch shortcut"; GroupDescription: "Additional tasks:"; Flags: unchecked; OnlyBelowVersion: 6.1

[Files]
Source: "{#KV_APP_DIR}"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\favicon.svg"; DestDir: "{app}\assets"; Flags: ignoreversion

[Icons]
Name: "{group}\KlipperVault"; Filename: "{app}\KlipperVault.exe"
Name: "{group}\{cm:UninstallProgram,KlipperVault}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\KlipperVault"; Filename: "{app}\KlipperVault.exe"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\KlipperVault"; Filename: "{app}\KlipperVault.exe"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\KlipperVault.exe"; Description: "Launch KlipperVault"; Flags: nowait postinstall skipifsilent
