import pytest

from dualboot_installer import validators as v


@pytest.mark.parametrize("value", ["archlinux", "my-pc", "a", "pc01"])
def test_valid_hostnames(value):
    assert v.validate_hostname(value) is None


@pytest.mark.parametrize("value", ["", "-pc", "pc-", "My_PC", "UPPER", "a" * 64, "has space"])
def test_invalid_hostnames(value):
    assert v.validate_hostname(value)


@pytest.mark.parametrize("value", ["alice", "_svc", "bob-2", "a_b"])
def test_valid_linux_usernames(value):
    assert v.validate_linux_username(value) is None


@pytest.mark.parametrize("value", ["", "Alice", "1user", "root", "has space", "a" * 33, "nobody"])
def test_invalid_linux_usernames(value):
    assert v.validate_linux_username(value)


def test_windows_username_rules():
    assert v.validate_windows_username("Alice Dupont") is None
    assert v.validate_windows_username("")
    assert v.validate_windows_username("a" * 21)
    assert v.validate_windows_username("bad:name")
    assert v.validate_windows_username("name.")
    assert v.validate_windows_username("Administrator")
    assert v.validate_windows_username("guest")
    assert v.validate_windows_username("PC-01", computer_name="pc-01")


@pytest.mark.parametrize("value", ["DESKTOP-AB12CD3", "PC", "my-pc-1"])
def test_valid_computer_names(value):
    assert v.validate_windows_computer_name(value) is None


@pytest.mark.parametrize("value", ["", "-PC", "PC-", "1234", "A" * 16, "pc_name", "pc name"])
def test_invalid_computer_names(value):
    assert v.validate_windows_computer_name(value)


def test_password_rules():
    assert v.validate_password("secret", "secret") is None
    assert v.validate_password("", "")
    assert v.validate_password("secret", "other")
    assert v.validate_password("tab\there", "tab\there")
    assert v.validate_password("x" * 128, "x" * 128)


def test_luks_passphrase_needs_8_characters():
    assert v.validate_luks_passphrase("short", "short")
    assert v.validate_luks_passphrase("long-enough", "long-enough") is None


def test_mok_password_is_ascii_alphanumeric_only():
    assert v.validate_mok_password("Abcd1234", "Abcd1234") is None
    assert v.validate_mok_password("short1", "short1")
    assert v.validate_mok_password("with-dash1", "with-dash1")
    accented = "Passw0rd" + chr(233)  # a non-ASCII letter: typed differently on other layouts
    assert v.validate_mok_password(accented, accented)
    assert v.validate_mok_password("Abcd1234", "Abcd1235")


def test_package_list_parsing():
    assert v.parse_package_list("git  vim, htop") == (["git", "vim", "htop"], None)
    assert v.parse_package_list("") == ([], None)
    assert v.parse_package_list("git git") == (["git"], None)
    packages, error = v.parse_package_list("git rm;-rf")
    assert error and packages == []
    assert v.parse_package_list("-evil")[1]


def test_iso_path():
    assert v.validate_iso_path("/mnt/usb/Win11.ISO") is None
    assert v.validate_iso_path("")
    assert v.validate_iso_path("/mnt/usb/readme.txt")
