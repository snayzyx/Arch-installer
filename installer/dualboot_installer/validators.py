"""Input validation. Every validator returns an error message, or None if valid."""

from __future__ import annotations

import re

LINUX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
PACKAGE_RE = re.compile(r"^[a-z0-9@._+][a-z0-9@._+-]*$")
MOK_PASSWORD_RE = re.compile(r"^[A-Za-z0-9]{8,32}$")
COMPUTER_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,13}[A-Za-z0-9])?$")

RESERVED_LINUX_USERS = frozenset(
    {"root", "daemon", "bin", "sys", "nobody", "http", "ftp", "mail", "git",
     "dbus", "avahi", "sddm", "polkitd", "games"}
)
RESERVED_WINDOWS_USERS = frozenset(
    {"administrator", "guest", "defaultaccount", "wdagutilityaccount", "system",
     "network service", "local service", "krbtgt", "trustedinstaller"}
)
WINDOWS_USERNAME_FORBIDDEN = frozenset('"/\\[]:;|=,+*?<>@')

MIN_LUKS_PASSPHRASE = 8


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def validate_hostname(value: str) -> str | None:
    if not value:
        return "The hostname is required."
    if not HOSTNAME_RE.match(value):
        return ("The hostname may only contain lowercase letters, digits and hyphens, "
                "and must not start or end with a hyphen (63 characters max).")
    return None


def validate_linux_username(value: str) -> str | None:
    if not value:
        return "The user name is required."
    if not LINUX_USER_RE.match(value):
        return ("The user name must start with a lowercase letter or underscore and contain "
                "only lowercase letters, digits, underscores and hyphens (32 characters max).")
    if value in RESERVED_LINUX_USERS:
        return f"'{value}' is reserved for the system. Choose another user name."
    return None


def validate_windows_username(value: str, computer_name: str = "") -> str | None:
    if not value:
        return "The Windows user name is required."
    if len(value) > 20:
        return "The Windows user name must be 20 characters or fewer."
    if any(ch in WINDOWS_USERNAME_FORBIDDEN for ch in value) or _has_control_chars(value):
        return 'The Windows user name must not contain any of: " / \\ [ ] : ; | = , + * ? < > @'
    if value.endswith(".") or not value.strip(" ."):
        return "The Windows user name must not end with a period or consist only of periods and spaces."
    if value.lower() in RESERVED_WINDOWS_USERS:
        return f"'{value}' is reserved by Windows. Choose another user name."
    if computer_name and value.lower() == computer_name.lower():
        return "The Windows user name must differ from the computer name."
    return None


def validate_windows_computer_name(value: str) -> str | None:
    if not value:
        return "The computer name is required."
    if not COMPUTER_NAME_RE.match(value):
        return ("The computer name may only contain letters, digits and hyphens, must not start "
                "or end with a hyphen, and is limited to 15 characters.")
    if value.isdigit():
        return "The computer name must not consist only of digits."
    return None


def validate_password(password: str, confirm: str, *, label: str = "Password",
                      min_length: int = 1) -> str | None:
    if not password:
        return f"{label} is required."
    if len(password) < min_length:
        return f"{label} must be at least {min_length} characters long."
    if len(password) > 127:
        return f"{label} must be 127 characters or fewer."
    if _has_control_chars(password):
        return f"{label} must not contain control characters."
    if password != confirm:
        return f"{label} and its confirmation do not match."
    return None


def validate_luks_passphrase(passphrase: str, confirm: str) -> str | None:
    return validate_password(passphrase, confirm, label="The disk encryption passphrase",
                             min_length=MIN_LUKS_PASSPHRASE)


def validate_mok_password(password: str, confirm: str) -> str | None:
    if password != confirm:
        return "The Secure Boot enrollment password and its confirmation do not match."
    if not MOK_PASSWORD_RE.match(password):
        return ("The Secure Boot enrollment password must be 8 to 32 letters and digits only: "
                "it is typed on the firmware's own screen, which always uses a US keyboard layout.")
    return None


def parse_package_list(text: str) -> tuple[list[str], str | None]:
    packages = text.replace(",", " ").split()
    for name in packages:
        if not PACKAGE_RE.match(name):
            return [], f"'{name}' is not a valid package name."
    return list(dict.fromkeys(packages)), None


def validate_iso_url(url: str) -> str | None:
    if not url.strip():
        return "Paste the download link of the Windows 11 ISO."
    if not re.match(r"^https://[^\s/]+\.[^\s/]+/\S+$", url.strip()):
        return "The download link must be a full https:// address."
    return None


def validate_iso_path(path: str) -> str | None:
    if not path.strip():
        return "Enter the path of the Windows 11 ISO file."
    if not path.lower().endswith(".iso"):
        return "The Windows image must be an .iso file."
    return None
