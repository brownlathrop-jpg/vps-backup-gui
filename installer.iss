#define AppVer GetEnv("APP_VERSION")

[Setup]
AppId={{8C4E2A71-5B3D-4F6A-9E21-VPSBACKUP0001}
AppName=VPS Backup
AppVerName=VPS Backup v{#AppVer}
AppVersion={#AppVer}
AppPublisher=VPS Backup
DefaultDirName={localappdata}\Programs\VPSBackup
DefaultGroupName=VPS Backup
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=VPSBackup-Setup-v{#AppVer}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\VPSBackup.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Дополнительно:"

[Files]
Source: "dist\VPSBackup\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\VPS Backup"; Filename: "{app}\VPSBackup.exe"
Name: "{group}\Удалить VPS Backup"; Filename: "{uninstallexe}"
Name: "{userdesktop}\VPS Backup"; Filename: "{app}\VPSBackup.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\VPSBackup.exe"; Description: "Запустить VPS Backup"; Flags: nowait postinstall skipifsilent
