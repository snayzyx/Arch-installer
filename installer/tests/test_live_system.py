"""LiveSystem parsing, with the command output faked."""

import json

from dualboot_installer.system import live
from dualboot_installer.system.live import LiveSystem


def fake_run(outputs: dict[str, str]):
    def run(argv, timeout=15):
        return outputs.get(argv[0] + " " + " ".join(argv[1:3]), outputs.get(argv[0], ""))

    return run


def test_live_medium_disk_walks_up_through_device_mapper(monkeypatch):
    # Ventoy: /run/archiso/bootmnt is on a device-mapper node whose ancestors are sdb1 then sdb.
    monkeypatch.setattr(live, "_run", fake_run({
        "findmnt": "/dev/mapper/ventoy\n",
        "lsblk": "ventoy dm\nsdb1 part\nsdb disk\n",
    }))
    assert LiveSystem._live_medium_disk() == "sdb"


def test_live_medium_disk_is_none_when_not_booted_from_the_iso(monkeypatch):
    monkeypatch.setattr(live, "_run", fake_run({"findmnt": "", "blkid": ""}))
    assert LiveSystem._live_medium_disk() is None


def test_live_medium_is_found_by_its_label_when_archiso_unmounted_it(monkeypatch):
    # copytoram unmounts /run/archiso/bootmnt; the USB stick must still be protected from erasure.
    monkeypatch.setattr(live, "_run", fake_run({
        "findmnt": "",
        "blkid": "/dev/sdb1\n",
        "lsblk": "sdb1 part\nsdb disk\n",
    }))
    assert LiveSystem._live_medium_disk() == "sdb"


def test_disks_are_parsed_and_the_live_medium_is_flagged(monkeypatch):
    lsblk = {"blockdevices": [
        {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 512110190592, "model": "Samsung SSD 980 PRO ",
         "tran": "nvme", "type": "disk", "rm": False, "ro": False, "log-sec": 512, "pttype": "gpt"},
        {"name": "sdb", "path": "/dev/sdb", "size": "32015679488", "model": "Ultra", "tran": "usb",
         "type": "disk", "rm": "1", "ro": "0", "log-sec": "512", "pttype": None},
        {"name": "loop0", "path": "/dev/loop0", "size": 1000, "type": "loop", "rm": False, "ro": False},
        {"name": "sr0", "path": "/dev/sr0", "size": 0, "type": "rom", "rm": True, "ro": False},
    ]}
    monkeypatch.setattr(live, "_run", fake_run({"lsblk": json.dumps(lsblk), "findmnt": ""}))
    monkeypatch.setattr(LiveSystem, "_live_medium_disk", staticmethod(lambda: "sdb"))

    disks = LiveSystem().disks()

    assert [d.path for d in disks] == ["/dev/nvme0n1", "/dev/sdb"]
    nvme, usb = disks
    assert nvme.model == "Samsung SSD 980 PRO" and nvme.partition_table == "gpt" and not nvme.is_live_medium
    assert usb.removable and usb.is_live_medium and usb.sector_size == 512


def test_wifi_networks_are_parsed_from_iwctl_output(monkeypatch):
    device_list = (
        "                              Devices\n"
        "--------------------------------------------------------------------------------\n"
        "  Name                Address             Powered   Adapter   Mode\n"
        "--------------------------------------------------------------------------------\n"
        "  wlan0               aa:bb:cc:dd:ee:ff   on        phy0      station\n"
    )
    networks = (
        "\x1b[1;90m                         Available networks\x1b[0m\n"
        "--------------------------------------------------------------------------------\n"
        "    Network name                    Security  Signal\n"
        "--------------------------------------------------------------------------------\n"
        "\x1b[1;90m  >\x1b[0m \x1b[0mHome Network                    psk       ****\n"
        "    Cafe  Guest                     open      **\n"
        "    Weak                            psk       *\n"
    )
    monkeypatch.setattr(live, "_run", fake_run({
        "iwctl device list": device_list,
        "iwctl station wlan0": networks,
    }))

    device, found = LiveSystem().wifi_scan()

    assert device == "wlan0"
    assert [(n.ssid, n.security, n.signal) for n in found] == [
        ("Home Network", "psk", 4), ("Cafe  Guest", "open", 2), ("Weak", "psk", 1)]


def test_no_wifi_adapter(monkeypatch):
    monkeypatch.setattr(live, "_run", fake_run({"iwctl": ""}))
    assert LiveSystem().wifi_scan() == ("", [])
