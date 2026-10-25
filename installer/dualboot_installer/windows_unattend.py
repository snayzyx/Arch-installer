"""The answer file for Windows Setup, and the small scripts that run around it.

Windows Setup is started in WinPE from the FAT32 ``WINSETUP`` partition with one of
``MAX_DISKS`` answer files (they only differ by the disk number), chosen by a launcher that
finds the target disk by its GPT GUID. See docs/ARCHITECTURE.md.

Everything here is pure text generation so it can be tested without Windows.
"""

from __future__ import annotations

import base64
from xml.sax.saxutils import escape

from .config import InstallConfig, WindowsConfig
from .steps.bootloader import BOOTLOADER_ID  # the name of GRUB's firmware boot entry

MAX_DISKS = 16
SETUP_LABEL = "WINSETUP"
# Label of the one-time firmware boot entry that starts Windows Setup from the WINSETUP partition.
SETUP_ENTRY_TITLE = "Windows 11 Setup"
BOOT_ORDER_TASK = "ArchInstallBootOrder"
SETUP_LOADER = r"\EFI\BOOT\BOOTX64.EFI"  # Microsoft's boot manager, as copied from the ISO
ANSWER_DIR = "archinstall"

# Public "generic" installation keys published by Microsoft. They select the edition and let
# Setup run unattended; they do not activate Windows.
GENERIC_KEYS = {"Home": "YTMG3-N6DKC-DKB77-7M9GH-8HVX7", "Pro": "VK7JG-NPHTM-C97JM-9MPGT-3V66T"}

# Preinstalled apps removed for the debloat tweak. Store, Photos, Notepad, Paint, Calculator,
# Terminal and the Xbox components are kept.
REMOVED_APPS = (
    "Microsoft.BingNews", "Microsoft.BingWeather", "Microsoft.BingSearch", "Microsoft.GetHelp",
    "Microsoft.Getstarted", "Microsoft.MicrosoftOfficeHub", "Microsoft.MicrosoftSolitaireCollection",
    "Microsoft.People", "Microsoft.WindowsFeedbackHub", "Microsoft.YourPhone", "Microsoft.Todos",
    "Microsoft.PowerAutomateDesktop", "Microsoft.ZuneMusic", "Microsoft.ZuneVideo", "Clipchamp.Clipchamp",
    "MicrosoftCorporationII.QuickAssist", "Microsoft.549981C3F5F10",  # Cortana
    "MicrosoftTeams", "MSTeams", "Microsoft.OutlookForWindows", "Microsoft.Copilot",
)

_NS = ('xmlns="urn:schemas-microsoft-com:unattend" '
       'xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" '
       'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
_COMPONENT = 'processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"'

# (name, value) of every LabConfig switch: skips the TPM, Secure Boot, RAM, CPU and storage checks.
_LAB_CONFIG = ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck", "BypassCPUCheck", "BypassStorageCheck")


def encode_password(password: str, kind: str = "Password") -> str:
    """The obfuscated form Setup accepts with PlainText=false (not encryption!)."""
    return base64.b64encode((password + kind).encode("utf-16-le")).decode("ascii")


def _command(order: int, path: str) -> str:
    return (f'<RunSynchronousCommand wcm:action="add"><Order>{order}</Order>'
            f"<Path>{escape(path)}</Path></RunSynchronousCommand>")


def _run_synchronous(paths: list[str]) -> str:
    return "<RunSynchronous>" + "".join(_command(i, p) for i, p in enumerate(paths, start=1)) + "</RunSynchronous>"


def _reg(key: str, name: str, value: int) -> str:
    return f'reg.exe add "{key}" /v {name} /t REG_DWORD /d {value} /f'


def specialize_commands() -> list[str]:
    """Registry changes made before the first boot into Windows (the 'always applied' tweaks)."""
    return [
        # Windows keeps the hardware clock in local time; Linux uses UTC. Make Windows use UTC too.
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\TimeZoneInformation", "RealTimeIsUniversal", 1),
        # No Fast Startup / hibernation: a "shut down" would leave the NTFS volume in a saved state that
        # Linux refuses to write to (and writing to it anyway would corrupt the Windows hibernation image).
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power", "HiberbootEnabled", 0),
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Power", "HibernateEnabled", 0),
        # No automatic BitLocker / device encryption: a changed boot chain would trigger recovery.
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\BitLocker", "PreventDeviceEncryption", 1),
        # Privacy: least telemetry allowed, no consumer features, no advertising id.
        _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection", "AllowTelemetry", 0),
        _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent", "DisableWindowsConsumerFeatures", 1),
        _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AdvertisingInfo", "DisabledByGroupPolicy", 1),
        # Keeps the local account flow available when no network is connected.
        _reg(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE", "BypassNRO", 1),
    ]


DRIVERS_FOLDER = r"C:\Drivers"  # where sources/$OEM$/$1/Drivers lands


def drivers_component() -> str:
    r"""Tells Windows to install the drivers staged in C:\Drivers while it sets the machine up (specialize pass)."""
    return (f'\n    <component name="Microsoft-Windows-PnpCustomizationsNonWinPE" {_COMPONENT}>\n'
            '      <DriverPaths><PathAndCredentials wcm:action="add" wcm:keyValue="1">'
            f"<Path>{DRIVERS_FOLDER}</Path></PathAndCredentials></DriverPaths>\n"
            "    </component>")


def build_unattend(cfg: InstallConfig, *, disk_id: int, partition_id: int, image_index: int,
                   drivers: bool = False) -> str:
    r"""autounattend.xml for one disk number. `drivers`: add C:\Drivers as a driver path (nothing else changes)."""
    w = cfg.windows
    language = w.language_tag
    keyboard = cfg.windows_keyboard_tag()
    timezone = cfg.windows_timezone_id()
    user = escape(w.username)
    key = GENERIC_KEYS.get(w.edition, GENERIC_KEYS["Pro"])

    lab_config = [_reg(r"HKLM\SYSTEM\Setup\LabConfig", name, 1) for name in _LAB_CONFIG]
    international = (f"<InputLocale>{escape(keyboard)}</InputLocale><SystemLocale>{language}</SystemLocale>"
                     f"<UILanguage>{language}</UILanguage><UserLocale>{language}</UserLocale>")

    return f"""<?xml version="1.0" encoding="utf-8"?>
<unattend {_NS}>
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" {_COMPONENT}>
      <SetupUILanguage><UILanguage>{language}</UILanguage></SetupUILanguage>
      {international}
    </component>
    <component name="Microsoft-Windows-Setup" {_COMPONENT}>
      {_run_synchronous(lab_config)}
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>{disk_id}</DiskID>
          <WillWipeDisk>false</WillWipeDisk>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add">
              <Order>1</Order>
              <PartitionID>{partition_id}</PartitionID>
              <Format>NTFS</Format>
              <Label>Windows</Label>
              <Letter>C</Letter>
            </ModifyPartition>
          </ModifyPartitions>
        </Disk>
        <WillShowUI>OnError</WillShowUI>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom>
            <MetaData wcm:action="add"><Key>/IMAGE/INDEX</Key><Value>{image_index}</Value></MetaData>
          </InstallFrom>
          <InstallTo><DiskID>{disk_id}</DiskID><PartitionID>{partition_id}</PartitionID></InstallTo>
          <WillShowUI>OnError</WillShowUI>
        </OSImage>
      </ImageInstall>
      <UserData>
        <AcceptEula>true</AcceptEula>
        <ProductKey><Key>{key}</Key><WillShowUI>OnError</WillShowUI></ProductKey>
      </UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" {_COMPONENT}>
      <ComputerName>{escape(w.computer_name)}</ComputerName>
      <TimeZone>{escape(timezone)}</TimeZone>
    </component>
    <component name="Microsoft-Windows-Deployment" {_COMPONENT}>
      {_run_synchronous(specialize_commands())}
    </component>{drivers_component() if drivers else ""}
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" {_COMPONENT}>
      {international}
    </component>
    <component name="Microsoft-Windows-Shell-Setup" {_COMPONENT}>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <NetworkLocation>Home</NetworkLocation>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{user}</Name>
            <DisplayName>{user}</DisplayName>
            <Group>Administrators</Group>
            <Password><Value>{encode_password(cfg.windows_password())}</Value><PlainText>false</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <TimeZone>{escape(timezone)}</TimeZone>
    </component>
  </settings>
</unattend>
"""


# -- WinPE launcher -----------------------------------------------------------------------------


def winpeshl_ini() -> str:
    """Replaces the Setup image's shell: run our launcher instead of setup.exe directly."""
    return ("[LaunchApps]\r\n"
            "%SYSTEMDRIVE%\\Windows\\System32\\cmd.exe, /c %SYSTEMDRIVE%\\Windows\\System32\\archinstall.cmd\r\n")


def launcher_cmd(disk_guid: str) -> str:
    """Finds the target disk by GPT GUID, picks the matching answer file, starts Setup."""
    guid = disk_guid.strip("{}").upper()
    letters = " ".join("CDEFGHIJKLMNOPQRSTUVWYZ")
    return f"""@echo off
rem ArchInstall: start Windows Setup on the disk this installer partitioned.
wpeinit
set DISK=
for /l %%i in (0,1,{MAX_DISKS - 1}) do (
  if not defined DISK (
    (echo select disk %%i& echo detail disk) | diskpart 2>nul | find /i "{guid}" >nul && set DISK=%%i
  )
)
if not defined DISK goto nodisk
set ANSWER=
for %%d in ({letters}) do (
  if not defined ANSWER if exist %%d:\\{ANSWER_DIR}\\unattend-%DISK%.xml set ANSWER=%%d:\\{ANSWER_DIR}\\unattend-%DISK%.xml
)
if not defined ANSWER goto noanswer
%SYSTEMDRIVE%\\sources\\setup.exe /unattend:%ANSWER%
exit /b 0

:nodisk
echo ArchInstall: the target disk (GPT GUID {guid}) was not found.
echo Windows Setup was not started. Nothing was changed on the disk.
cmd /k
exit /b 1

:noanswer
echo ArchInstall: the answer file for disk %DISK% was not found on any drive.
cmd /k
exit /b 1
""".replace("\n", "\r\n")


# -- scripts copied into the installed Windows ---------------------------------------------------


def setup_complete_cmd() -> str:
    return ("@echo off\r\n"
            'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0archinstall-finish.ps1" '
            '> "%SystemRoot%\\Temp\\archinstall-finish.log" 2>&1\r\n'
            "exit /b 0\r\n")


def bootorder_script() -> str:
    """Runs once, as SYSTEM, at the first start after Windows Setup: puts GRUB first, then removes
    the scheduled task that started it."""
    return f"""# Generated by ArchInstall. Started once by the scheduled task '{BOOT_ORDER_TASK}'.
$log = Join-Path $env:SystemRoot 'Temp\\archinstall-bootorder.log'
function Say($text) {{ Add-Content -Path $log -Value ('{{0:s}} {{1}}' -f (Get-Date), $text) }}

# bcdedit's labels are translated, so find the entry by its name and read the GUID format.
$entries = ((bcdedit /enum firmware /v) -join "`r`n") -split '(?:\\r?\\n){{2,}}'
$grub = $null
foreach ($entry in $entries) {{
    if ($entry -match '{BOOTLOADER_ID}' -and $entry -match '\\{{[0-9a-fA-F-]{{36}}\\}}') {{ $grub = $Matches[0]; break }}
}}
if ($grub) {{
    bcdedit /set '{{fwbootmgr}}' displayorder $grub /addfirst | Out-Null
    Say "firmware boot order: $grub ({BOOTLOADER_ID}) is first"
}} else {{
    Say 'no firmware entry named {BOOTLOADER_ID}: the boot order stays as Windows Setup made it'
}}
Unregister-ScheduledTask -TaskName '{BOOT_ORDER_TASK}' -Confirm:$false -ErrorAction SilentlyContinue
Say 'done'
"""


WIFI_TASK = "ArchInstallWifi"
WIFI_PROFILE_FILE = "archinstall-wifi.xml"
WIFI_SECURITY_KINDS = ("open", "psk")  # what a passphrase can describe (not 802.1X, not WEP)


def wifi_supported(security: str) -> bool:
    return security in WIFI_SECURITY_KINDS


def wifi_profile_xml(ssid: str, security: str, passphrase: str) -> str | None:
    """A Windows WLAN profile (`netsh wlan add profile`) for the network used during the installation.

    WPA2-Personal with AES: it also joins WPA2/WPA3 mixed-mode access points. A network that only
    accepts WPA3 has to be joined by hand once. None for what a passphrase cannot describe.
    """
    if not wifi_supported(security) or (security == "psk" and not passphrase):
        return None
    name = escape(ssid)
    if security == "psk":
        auth = ("<authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption>"
                "<useOneX>false</useOneX></authEncryption>"
                "<sharedKey><keyType>passPhrase</keyType><protected>false</protected>"
                f"<keyMaterial>{escape(passphrase)}</keyMaterial></sharedKey>")
    else:
        auth = ("<authEncryption><authentication>open</authentication><encryption>none</encryption>"
                "<useOneX>false</useOneX></authEncryption>")
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">\n'
            f"  <name>{name}</name>\n"
            f"  <SSIDConfig><SSID><name>{name}</name></SSID></SSIDConfig>\n"
            "  <connectionType>ESS</connectionType>\n"
            "  <connectionMode>auto</connectionMode>\n"
            f"  <MSM><security>{auth}</security></MSM>\n"
            "</WLANProfile>\n")


def wifi_script() -> str:
    """Adds the staged Wi-Fi profile to Windows. Run at the end of Setup and, when there is no Wi-Fi adapter
    yet (its driver may come from Windows Update later), again by a scheduled task until it works."""
    return f"""# Generated by ArchInstall. Adds the Wi-Fi network used during the installation to Windows.
$xml = Join-Path $PSScriptRoot '{WIFI_PROFILE_FILE}'
$log = Join-Path $env:SystemRoot 'Temp\\archinstall-wifi.log'
function Say($text) {{ Add-Content -Path $log -Value ('{{0:s}} {{1}}' -f (Get-Date), $text) }}
function Finish() {{
    Remove-Item -Path $xml -Force -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName '{WIFI_TASK}' -Confirm:$false -ErrorAction SilentlyContinue
}}
if (-not (Test-Path $xml)) {{ Finish; exit 0 }}
# The profile holds the passphrase in clear text: only SYSTEM and the administrators may read it.
icacls.exe $xml /inheritance:r /grant:r '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
if (((Get-Date) - (Get-Item $xml).CreationTime).TotalDays -gt 3) {{
    Say 'no Wi-Fi adapter after 3 days: giving up, the profile file is deleted'
    Finish; exit 0
}}
$result = (netsh.exe wlan add profile filename="$xml" user=all 2>&1) -join ' '
if ($LASTEXITCODE -eq 0) {{
    Say "Wi-Fi profile added: $result"
    Finish
}} else {{
    Say "Wi-Fi profile not added yet ($result); trying again later"
}}
"""


# Part of the end-of-Setup script when network drivers were staged in C:\Drivers (see steps/windows_drivers.py).
# Not a format string: the braces are PowerShell's.
DRIVERS_SECTION = r"""# 1e. Network, Wi-Fi and Bluetooth drivers staged by the installer in C:\Drivers (taken from the Windows that was
#     on this computer, or bundled in the installer): the answer file made Windows install them while it set the
#     machine up. Say which ones are in the driver store, then delete the folder.
if (Test-Path 'C:\Drivers') {
    try {
        $driversStaged = @(Get-ChildItem -Path 'C:\Drivers' -Recurse -Filter '*.inf' -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Name.ToLower() } | Sort-Object -Unique)
        $driversStored = @(Get-WindowsDriver -Online -All -ErrorAction Stop |
            ForEach-Object { [IO.Path]::GetFileName($_.OriginalFileName).ToLower() } | Sort-Object -Unique)
        $driversPresent = @($driversStaged | Where-Object { $driversStored -contains $_ })
        Say "drivers staged in C:\Drivers: $($driversStaged.Count) INF file(s), $($driversPresent.Count) in the Windows driver store"
        foreach ($name in $driversPresent) { Say "  driver store: $name" }
    } catch {
        Say "could not list the driver store ($_)"
    }
    Remove-Item -Path 'C:\Drivers' -Recurse -Force -ErrorAction SilentlyContinue
    Say 'C:\Drivers deleted'
}

"""

# Part of the end-of-Setup script when a Wi-Fi network was used for the installation (filled with .format).
WIFI_SECTION = r"""# 1d. Wi-Fi: add the network used during the installation. A laptop with no driver for its Wi-Fi adapter yet
#     gets it from Windows Update later, so a task tries again every 15 minutes for two days.
& (Join-Path $PSScriptRoot 'archinstall-wifi.ps1')
if (Test-Path (Join-Path $PSScriptRoot 'archinstall-wifi.xml')) {{
    try {{
        $wifiAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'archinstall-wifi.ps1')`""
        $wifiAtStart = New-ScheduledTaskTrigger -AtStartup
        $wifiRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(10) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 2)
        $wifiWho = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName '{task}' -Action $wifiAction -Trigger @($wifiAtStart, $wifiRepeat) -Principal $wifiWho -Force -ErrorAction Stop | Out-Null
        Say 'no Wi-Fi adapter yet: a task will add the Wi-Fi profile when there is one'
    }} catch {{
        Say "could not schedule the Wi-Fi profile ($_)"
    }}
}} else {{
    Say 'Wi-Fi profile added'
}}

"""


def finish_script(w: WindowsConfig, *, shared_partition: bool, wifi: bool = False, drivers: bool = False) -> str:
    """Runs once at the end of Windows Setup, as SYSTEM, before the first sign-in."""
    removed = ", ".join(f"'{name}'" for name in REMOVED_APPS)
    shared = "$true" if shared_partition else "$false"
    wifi_section = WIFI_SECTION.format(task=WIFI_TASK) if wifi else ""
    drivers_section = DRIVERS_SECTION if drivers else ""
    return f"""# Generated by ArchInstall. Runs once from SetupComplete.cmd. Every action leaves a line in the log.
$ErrorActionPreference = 'Continue'
function Say($text) {{ '{{0:s}} {{1}}' -f (Get-Date), $text }}

# 1. Remove the preinstalled apps nobody asked for (they would otherwise install for every new user).
$remove = @({removed})
$provisioned = @(Get-AppxProvisionedPackage -Online)
Say "preinstalled apps found: $($provisioned.Count)"
foreach ($name in $remove) {{
    $package = $provisioned | Where-Object {{ $_.DisplayName -eq $name }}
    if ($package) {{
        $package | Remove-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue | Out-Null
        Say "removed $name"
    }}
}}

# 1b. Hibernation off: removes hiberfil.sys and with it Fast Startup (the registry values set at
#     setup time already say so; this makes it effective for sure).
powercfg.exe /hibernate off
Say "hibernation and Fast Startup turned off (powercfg exit code $LASTEXITCODE)"

# 1c. Laptops and PCs sold with Windows carry a licence key in the firmware. Windows Setup was told to
#     install with a generic key (so that it never asks for one), which would hide that licence:
#     install the firmware key instead. The key itself is never written to the log.
try {{
    $firmwareKey = (Get-CimInstance -ClassName SoftwareLicensingService -ErrorAction Stop).OA3xOriginalProductKey
    if ($firmwareKey) {{
        Say 'a product key is stored in the firmware: installing it'
        cscript.exe //nologo "$env:windir\\system32\\slmgr.vbs" /ipk $firmwareKey | Out-Null
        Say "key installed (exit code $LASTEXITCODE)"
        cscript.exe //nologo "$env:windir\\system32\\slmgr.vbs" /ato | Out-Null
        Say "activation attempted (exit code $LASTEXITCODE); Windows retries by itself once it is online"
    }} else {{
        Say 'no product key in the firmware: Windows stays unactivated until a key or a digital licence is available'
    }}
}} catch {{
    Say "could not read the firmware product key ($_)"
}}

{drivers_section}{wifi_section}# 2. GRUB goes back first in the firmware boot order, but NOT yet. Windows still restarts by itself
#    once more after this script (end of its first setup). If GRUB (through shim) were first now,
#    that restart would land on shim's blue key-enrollment screen with nobody to press a key, and
#    with Secure Boot on Windows could not finish (seen in the end-to-end test). A task changes the
#    order at the next start instead, when Windows is up, then deletes itself.
$bootOrderScript = Join-Path $PSScriptRoot 'archinstall-bootorder.ps1'
try {{
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$bootOrderScript`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName '{BOOT_ORDER_TASK}' -Action $action -Trigger $trigger -Principal $principal -Force -ErrorAction Stop | Out-Null
    Say 'scheduled the firmware boot order change for the next start of Windows'
}} catch {{
    Say "could not schedule the boot order change ($_): doing it now"
    & $bootOrderScript
}}

#    The one-time entry that started Windows Setup is of no use any more.
$entries = ((bcdedit /enum firmware /v) -join "`r`n") -split '(?:\\r?\\n){{2,}}'
foreach ($entry in $entries) {{
    if ($entry -match '{SETUP_ENTRY_TITLE}' -and $entry -match '\\{{[0-9a-fA-F-]{{36}}\\}}') {{
        $id = $Matches[0]
        bcdedit /delete $id | Out-Null
        Say "deleted the one-time entry $id ({SETUP_ENTRY_TITLE})"
    }}
}}

# 3. The partition that held the Windows Setup files: shared data partition, or removed.
$part = Get-Partition | ForEach-Object {{
    $volume = $_ | Get-Volume -ErrorAction SilentlyContinue
    if ($volume -and $volume.FileSystemLabel -eq '{SETUP_LABEL}') {{ $_ }}
}} | Select-Object -First 1
if ($part) {{
    if ({shared}) {{
        Format-Volume -Partition $part -FileSystem NTFS -NewFileSystemLabel 'Shared' -Confirm:$false | Out-Null
        Say "partition $($part.PartitionNumber) ({SETUP_LABEL}) is now the NTFS volume 'Shared'"
    }} else {{
        Remove-Partition -InputObject $part -Confirm:$false
        Say "partition $($part.PartitionNumber) ({SETUP_LABEL}) removed"
    }}
}} else {{
    Say 'no {SETUP_LABEL} partition found'
}}
Say 'done'
"""
