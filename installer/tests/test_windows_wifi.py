"""The Wi-Fi network used for the installation is also given to Windows."""

import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from dualboot_installer.config import InstallConfig
from dualboot_installer.runner import FileWrite
from dualboot_installer.steps import windows
from dualboot_installer.windows_unattend import (
    WIFI_PROFILE_FILE,
    WIFI_TASK,
    finish_script,
    wifi_profile_xml,
    wifi_script,
    wifi_supported,
)

NS = {"w": "http://www.microsoft.com/networking/WLAN/profile/v1"}


def parse(xml: str) -> ET.Element:
    return ET.fromstring(xml.split("?>", 1)[1])  # ElementTree refuses a str with an encoding declaration


def powershell_parse_errors(script: str) -> list[str]:
    """Syntax errors found by the real PowerShell parser; skipped when there is no PowerShell."""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("no PowerShell on this machine")
    command = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseInput("
               "[Console]::In.ReadToEnd(), [ref]$null, [ref]$e); $e | ForEach-Object { $_.Message }")
    result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", command], input=script,
                            capture_output=True, text=True, timeout=60)
    return [line for line in result.stdout.splitlines() if line.strip()]


# -- the profile ---------------------------------------------------------------------------------------------


def test_a_wpa2_network_becomes_a_windows_profile_with_its_passphrase():
    root = parse(wifi_profile_xml("Home Wi-Fi", "psk", "pass phrase 123"))
    assert root.tag == "{%s}WLANProfile" % NS["w"]
    assert root.findtext("w:name", namespaces=NS) == "Home Wi-Fi"
    assert root.findtext("w:SSIDConfig/w:SSID/w:name", namespaces=NS) == "Home Wi-Fi"
    assert root.findtext("w:connectionType", namespaces=NS) == "ESS"
    assert root.findtext("w:connectionMode", namespaces=NS) == "auto"  # joins by itself
    security = "w:MSM/w:security/"
    assert root.findtext(security + "w:authEncryption/w:authentication", namespaces=NS) == "WPA2PSK"
    assert root.findtext(security + "w:authEncryption/w:encryption", namespaces=NS) == "AES"
    assert root.findtext(security + "w:authEncryption/w:useOneX", namespaces=NS) == "false"
    assert root.findtext(security + "w:sharedKey/w:keyType", namespaces=NS) == "passPhrase"
    assert root.findtext(security + "w:sharedKey/w:protected", namespaces=NS) == "false"
    assert root.findtext(security + "w:sharedKey/w:keyMaterial", namespaces=NS) == "pass phrase 123"


def test_an_open_network_has_no_key():
    root = parse(wifi_profile_xml("Cafe", "open", ""))
    assert root.findtext("w:MSM/w:security/w:authEncryption/w:authentication", namespaces=NS) == "open"
    assert root.findtext("w:MSM/w:security/w:authEncryption/w:encryption", namespaces=NS) == "none"
    assert root.find("w:MSM/w:security/w:sharedKey", NS) is None


def test_special_characters_cannot_break_the_xml():
    text = wifi_profile_xml('A&B <home> "x"', "psk", "p&ss<w>rd\"'")
    root = parse(text)  # still well-formed
    assert root.findtext("w:name", namespaces=NS) == 'A&B <home> "x"'
    assert root.findtext("w:MSM/w:security/w:sharedKey/w:keyMaterial", namespaces=NS) == "p&ss<w>rd\"'"


@pytest.mark.parametrize("security", ["8021x", "wep", "", "sae"])
def test_networks_a_passphrase_cannot_describe_are_not_copied(security):
    assert not wifi_supported(security)
    assert wifi_profile_xml("Corp", security, "x" * 10) is None


def test_a_protected_network_without_a_passphrase_is_not_copied():
    assert wifi_profile_xml("Home", "psk", "") is None


# -- the scripts ---------------------------------------------------------------------------------------------


def test_the_wifi_script_adds_the_profile_for_every_user_locks_the_file_and_cleans_up():
    script = wifi_script()
    assert 'netsh.exe wlan add profile filename="$xml" user=all' in script
    assert "icacls.exe" in script and "*S-1-5-18" in script and "*S-1-5-32-544" in script  # SYSTEM, Administrators
    assert f"Unregister-ScheduledTask -TaskName '{WIFI_TASK}'" in script
    assert "Remove-Item -Path $xml" in script
    assert "TotalDays -gt 3" in script  # gives up and deletes the clear-text passphrase
    assert WIFI_PROFILE_FILE in script
    assert "keyMaterial" not in script  # the passphrase is in the XML file only


def test_the_finish_script_only_deals_with_wifi_when_a_network_was_staged():
    without = finish_script(InstallConfig().windows, shared_partition=False)
    with_wifi = finish_script(InstallConfig().windows, shared_partition=False, wifi=True)
    assert "archinstall-wifi" not in without and WIFI_TASK not in without
    assert "archinstall-wifi.ps1" in with_wifi and WIFI_TASK in with_wifi
    assert "-RepetitionInterval (New-TimeSpan -Minutes 15)" in with_wifi
    assert "-AtStartup" in with_wifi


def test_the_scripts_are_valid_powershell():
    assert powershell_parse_errors(wifi_script()) == []
    assert powershell_parse_errors(finish_script(InstallConfig().windows, shared_partition=False, wifi=True)) == []
    assert powershell_parse_errors(finish_script(InstallConfig().windows, shared_partition=True, wifi=False)) == []


# -- staging -------------------------------------------------------------------------------------------------

SCRIPTS = "/tmp/archinstall-windows-setup/sources/$OEM$/$$/Setup/Scripts"


def staged(**network):
    from test_windows_steps import make_ctx  # the same helper as the other staging tests

    lines: list[str] = []
    ctx = make_ctx()
    ctx.runner.log = lines.append
    for key, value in network.items():
        setattr(ctx.cfg.network, key, value)
    ctx.state.update(windows_iso="/media/Win11.iso", image_index=1)
    windows.stage_setup(ctx)
    written = {op.path: op for op in ctx.runner.operations if isinstance(op, FileWrite)}
    return written, lines


def test_the_wifi_profile_is_staged_with_the_end_of_setup_scripts_as_a_secret():
    written, lines = staged(wifi_ssid="Home", wifi_security="psk", wifi_passphrase="wifi-pass-123")
    xml = written[f"{SCRIPTS}/{WIFI_PROFILE_FILE}"]
    assert xml.secret and "wifi-pass-123" in xml.content
    assert f"{SCRIPTS}/archinstall-wifi.ps1" in written
    assert "archinstall-wifi.ps1" in written[f"{SCRIPTS}/archinstall-finish.ps1"].content
    assert not any("wifi-pass-123" in line for line in lines)  # never logged


def test_nothing_about_wifi_is_staged_without_a_network():
    written, _ = staged()
    assert f"{SCRIPTS}/{WIFI_PROFILE_FILE}" not in written
    assert f"{SCRIPTS}/archinstall-wifi.ps1" not in written
    assert "archinstall-wifi" not in written[f"{SCRIPTS}/archinstall-finish.ps1"].content


def test_an_enterprise_network_is_not_staged_and_the_log_says_so():
    written, lines = staged(wifi_ssid="Corp", wifi_security="8021x", wifi_passphrase="")
    assert f"{SCRIPTS}/{WIFI_PROFILE_FILE}" not in written
    assert any("'Corp' cannot be copied to Windows" in line for line in lines)
