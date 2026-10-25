"""Installing and configuring Arch Linux."""

from __future__ import annotations

import uuid
from pathlib import Path

from .. import extras
from ..config import InstallConfig
from ..data.xkb import xkb_layout
from ..drivers import packages_for
from ..layout import ROLE_ESP, ROLE_HOME, Layout
from ..runner import CommandError
from ..system.hardware import detect_laptop
from .context import TARGET, Context, StepError
from .storage import CRYPT_HOME, home_device, root_device

HYPRLAND_PACKAGES = [
    "hyprland", "xdg-desktop-portal-hyprland", "xdg-desktop-portal-gtk", "hyprpolkitagent", "polkit",
    "kitty", "waybar", "wofi", "sddm", "xorg-server",
    "pipewire", "pipewire-pulse", "pipewire-alsa", "wireplumber",
    "network-manager-applet", "qt5-wayland", "qt6-wayland",
    "noto-fonts", "noto-fonts-emoji", "ttf-jetbrains-mono-nerd", "brightnessctl", "pavucontrol",
]

# Added on a laptop: power profiles, Bluetooth, battery information and the firmware most recent
# laptops need for their audio (Sound Open Firmware).
# python-gobject: `powerprofilesctl`, the command that switches the profile, is a Python script
# that needs it (the package does not depend on it).
LAPTOP_PACKAGES = ["power-profiles-daemon", "python-gobject", "bluez", "bluez-utils", "upower", "sof-firmware"]
LAPTOP_SERVICES = ["bluetooth.service", "power-profiles-daemon.service"]

# systemd-based initramfs: it reads /etc/vconsole.conf, so the LUKS passphrase is typed on the
# chosen keyboard layout.
_INITRAMFS_HOOKS = ("base", "systemd", "autodetect", "microcode", "modconf", "kms", "keyboard",
                    "sd-vconsole", "block")


def cpu_ucode(cpuinfo: str | None = None) -> list[str]:
    if cpuinfo is None:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
        except OSError:
            return []
    if "GenuineIntel" in cpuinfo:
        return ["intel-ucode"]
    if "AuthenticAMD" in cpuinfo:
        return ["amd-ucode"]
    return []


def strategy_of(cfg: InstallConfig) -> str:
    return cfg.secureboot.resolved or "shim"


def wants_laptop(cfg: InstallConfig) -> bool:
    """Laptop extras: forced by the answers file, otherwise decided by the hardware."""
    return detect_laptop() if cfg.arch.laptop is None else cfg.arch.laptop


def package_list(cfg: InstallConfig, ucode: list[str], *, laptop: bool = False) -> list[str]:
    """Everything pacstrap installs, in one transaction."""
    a = cfg.arch
    packages = [
        "base", a.kernel, "linux-firmware", *ucode,
        "grub", "efibootmgr", "networkmanager", "sudo", "nano", "man-db",
        "ntfs-3g", "dosfstools", "zram-generator",
    ]
    if a.filesystem == "btrfs":
        packages.append("btrfs-progs")
    elif a.filesystem == "xfs":
        packages.append("xfsprogs")
    if a.luks_enabled:
        packages.append("cryptsetup")
    # mokutil: the service that queues the MOK enrollment again until it is done.
    packages += ["sbctl"] if strategy_of(cfg) == "sbctl" else ["sbsigntools", "openssl", "mokutil"]
    if extras.effective_aur_helper(a) != "none":
        packages += ["base-devel", "git"]
    packages += packages_for(a.gpu_profiles, a.kernel)
    if laptop:
        packages += LAPTOP_PACKAGES
        # A discrete NVIDIA GPU next to an integrated one: `prime-run` starts a program on the NVIDIA GPU.
        if "nvidia-open" in a.gpu_profiles and {"intel", "amd"} & set(a.gpu_profiles):
            packages.append("nvidia-prime")
    if a.desktop == "hyprland":
        packages += HYPRLAND_PACKAGES
    packages += extras.official_packages(a)
    packages += a.extra_packages
    return list(dict.fromkeys(packages))


PACSTRAP_ATTEMPTS = 3

_VFAT_OPTIONS = ("rw,relatime,fmask=0077,dmask=0077,codepage=437,iocharset=ascii,shortname=mixed,"
                 "utf8,errors=remount-ro")
_BTRFS_OPTIONS = "rw,noatime,compress=zstd"


def fstab_content(cfg: InstallConfig, layout: Layout, *, root_uuid: str, esp_uuid: str, home_uuid: str) -> str:
    """/etc/fstab, written by hand from the UUIDs we know.

    genfstab is not used: it falls back to /dev/xxx names when it cannot resolve a UUID, and it
    copies the live system's active swap into the new fstab (a swap partition of another disk
    that systemd activated would then block the boot).
    """
    fs = cfg.arch.filesystem
    has_home_partition = layout.get(ROLE_HOME) is not None
    rows: list[tuple[str, str, str, str, int]] = []  # source, mount point, type, options, fsck pass

    if fs == "btrfs":
        rows.append((f"UUID={root_uuid}", "/", "btrfs", f"{_BTRFS_OPTIONS},subvol=/@", 0))
        if not has_home_partition:
            rows.append((f"UUID={root_uuid}", "/home", "btrfs", f"{_BTRFS_OPTIONS},subvol=/@home", 0))
    else:
        rows.append((f"UUID={root_uuid}", "/", fs, "rw,relatime", 1 if fs == "ext4" else 0))
    if has_home_partition:
        options = _BTRFS_OPTIONS if fs == "btrfs" else "rw,relatime"
        rows.append((f"UUID={home_uuid}", "/home", fs, options, 2 if fs == "ext4" else 0))
    rows.append((f"UUID={esp_uuid}", "/boot", "vfat", _VFAT_OPTIONS, 2))

    lines = ["# <file system>  <mount point>  <type>  <options>  <dump>  <pass>"]
    lines += [f"{source}  {mount}  {kind}  {options}  0  {order}" for source, mount, kind, options, order in rows]
    return "\n".join(lines) + "\n"


def mkinitcpio_hooks(cfg: InstallConfig) -> str:
    hooks = list(_INITRAMFS_HOOKS)
    if cfg.arch.luks_enabled:
        hooks.append("sd-encrypt")
    hooks.append("filesystems")
    return f"HOOKS=({' '.join(hooks)})\n"


def hyprland_config(keymap: str) -> str:
    return f"""\
# Generated by ArchInstall. Full reference: https://wiki.hypr.land/Configuring/
monitor = , preferred, auto, auto

$terminal = kitty
$menu = wofi --show drun

exec-once = waybar
exec-once = nm-applet --indicator
exec-once = systemctl --user start hyprpolkitagent

input {{
    kb_layout = {xkb_layout(keymap)}
    follow_mouse = 1
    touchpad {{
        natural_scroll = true
        tap-to-click = true
        disable_while_typing = true
    }}
}}

general {{
    gaps_in = 5
    gaps_out = 10
    border_size = 2
    layout = dwindle
}}

$mod = SUPER
bind = $mod, Q, exec, $terminal
bind = $mod, R, exec, $menu
bind = $mod, C, killactive
bind = $mod, F, fullscreen
bind = $mod, V, togglefloating
bind = $mod SHIFT, M, exit
bind = $mod, left, movefocus, l
bind = $mod, right, movefocus, r
bind = $mod, up, movefocus, u
bind = $mod, down, movefocus, d
bind = $mod, 1, workspace, 1
bind = $mod, 2, workspace, 2
bind = $mod, 3, workspace, 3
bind = $mod, 4, workspace, 4
bind = $mod, 5, workspace, 5
bind = $mod SHIFT, 1, movetoworkspace, 1
bind = $mod SHIFT, 2, movetoworkspace, 2
bind = $mod SHIFT, 3, movetoworkspace, 3
bind = $mod SHIFT, 4, movetoworkspace, 4
bind = $mod SHIFT, 5, movetoworkspace, 5
bindm = $mod, mouse:272, movewindow
bindm = $mod, mouse:273, resizewindow

# Laptop function keys: volume, microphone, screen brightness.
bindel = , XF86AudioRaiseVolume, exec, wpctl set-volume -l 1 @DEFAULT_AUDIO_SINK@ 5%+
bindel = , XF86AudioLowerVolume, exec, wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-
bindl = , XF86AudioMute, exec, wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle
bindl = , XF86AudioMicMute, exec, wpctl set-mute @DEFAULT_AUDIO_SOURCE@ toggle
bindel = , XF86MonBrightnessUp, exec, brightnessctl -e4 -n2 set 5%+
bindel = , XF86MonBrightnessDown, exec, brightnessctl -e4 -n2 set 5%-
"""


# -- step functions -------------------------------------------------------------


def mirrors(ctx: Context) -> None:
    """Rank the mirrors of the chosen country. A failure is harmless: the default list stays."""
    returncode = ctx.run(
        "reflector", "--country", ctx.cfg.arch.country, "--protocol", "https", "--age", "24",
        "--latest", "20", "--sort", "rate", "--save", "/etc/pacman.d/mirrorlist", check=False)
    if returncode:
        ctx.log("reflector failed; keeping the default mirror list.")


def install_base(ctx: Context) -> None:
    a = ctx.cfg.arch
    # Written before pacstrap so the initramfs generated during the install is already right.
    ctx.write("/etc/vconsole.conf", f"KEYMAP={a.keymap}\n")
    ctx.write("/etc/mkinitcpio.conf.d/archinstall.conf", mkinitcpio_hooks(ctx.cfg))

    laptop = wants_laptop(ctx.cfg)
    packages = package_list(ctx.cfg, cpu_ucode(), laptop=laptop)
    ctx.log(f"Installing {len(packages)} packages" + (" (laptop extras included)." if laptop else "."))
    # A slow or flaky mirror ("Operation too slow") fails pacstrap, and it would otherwise abort
    # the whole installation after the disk has been erased. Running it again resumes the download.
    for attempt in range(1, PACSTRAP_ATTEMPTS + 1):
        if ctx.run("pacstrap", "-K", TARGET, *packages, check=False) == 0:
            break
        if attempt == PACSTRAP_ATTEMPTS:
            raise StepError(f"pacstrap failed {PACSTRAP_ATTEMPTS} times (see the messages above). "
                            "Check the network connection and run the installer again.")
        ctx.log(f"pacstrap failed (attempt {attempt} of {PACSTRAP_ATTEMPTS}): trying again.")
        ctx.run("sleep", "5")

    home_dev = home_device(ctx.cfg, ctx.layout)
    ctx.write("/etc/fstab", fstab_content(
        ctx.cfg,
        ctx.layout,
        root_uuid=ctx.uuid_of(root_device(ctx.cfg, ctx.layout), "<root-fs-uuid>"),
        esp_uuid=ctx.uuid_of(ctx.partition(ROLE_ESP), "<esp-uuid>"),
        home_uuid=ctx.uuid_of(home_dev, "<home-fs-uuid>") if home_dev else "",
    ))


def configure_system(ctx: Context) -> None:
    a = ctx.cfg.arch

    ctx.chroot("ln", "-sf", f"/usr/share/zoneinfo/{a.timezone}", "/etc/localtime")
    ctx.chroot("hwclock", "--systohc", check=False)  # virtual machines may have no RTC

    locales = ["en_US.UTF-8 UTF-8"]
    if a.locale != "en_US.UTF-8":
        locales.append(f"{a.locale} UTF-8")
    ctx.write("/etc/locale.gen", "\n".join(locales) + "\n")
    ctx.chroot("locale-gen")
    if not ctx.runner.dry_run:
        # locale-gen exits 0 even when it cannot build a locale, so check the result.
        generated = ctx.capture("arch-chroot", ctx.target, "locale", "-a").lower().split()
        wanted = a.locale.lower().replace("utf-8", "utf8")
        if wanted not in generated:
            raise StepError(f"The locale {a.locale} could not be generated (see the locale-gen output above).")
    ctx.write("/etc/locale.conf", f"LANG={a.locale}\n")

    ctx.write("/etc/hostname", f"{a.hostname}\n")
    ctx.write("/etc/hosts", f"127.0.0.1 localhost\n::1 localhost\n127.0.1.1 {a.hostname}\n")

    ctx.write("/etc/systemd/zram-generator.conf",
              "[zram0]\nzram-size = min(ram / 2, 4096)\ncompression-algorithm = zstd\n")

    ctx.write("/etc/X11/xorg.conf.d/00-keyboard.conf",
              'Section "InputClass"\n    Identifier "system-keyboard"\n    MatchIsKeyboard "on"\n'
              f'    Option "XkbLayout" "{xkb_layout(a.keymap)}"\nEndSection\n')

    _home_unlock_key(ctx)
    _accounts(ctx)

    services = ["NetworkManager.service", "systemd-timesyncd.service", "fstrim.timer"]
    if wants_laptop(ctx.cfg):
        services += LAPTOP_SERVICES
    if "docker" in a.dev_groups:
        services.append("docker.service")
    if a.desktop == "hyprland":
        services.append("sddm.service")
    ctx.chroot("systemctl", "enable", *services)
    _wifi_profile(ctx)


def _keyfile_escape(value: str) -> str:
    return value.replace("\\", "\\\\")


def wifi_profile(ssid: str, security: str, passphrase: str) -> str | None:
    """A NetworkManager connection file for the Wi-Fi network used during the installation.

    Without it a laptop that only has Wi-Fi boots the new system offline. Enterprise (802.1X)
    networks cannot be described from a passphrase, so they are not carried over.
    """
    if security not in ("open", "psk") or (security == "psk" and not passphrase):
        return None
    name = _keyfile_escape(ssid)
    lines = [
        "[connection]", f"id={name}", f"uuid={uuid.uuid5(uuid.NAMESPACE_DNS, 'archinstall-wifi-' + ssid)}",
        "type=wifi", "autoconnect=true", "",
        "[wifi]", "mode=infrastructure", f"ssid={name}", "",
    ]
    if security == "psk":
        lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={_keyfile_escape(passphrase)}", ""]
    lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(lines)


def _wifi_profile(ctx: Context) -> None:
    n = ctx.cfg.network
    if not n.wifi_ssid:
        return
    content = wifi_profile(n.wifi_ssid, n.wifi_security, n.wifi_passphrase)
    if content is None:
        ctx.log(f"The Wi-Fi network {n.wifi_ssid!r} cannot be carried over: connect to it again after the first boot.")
        return
    safe = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in n.wifi_ssid)
    ctx.write(f"/etc/NetworkManager/system-connections/{safe}.nmconnection", content, 0o600, secret=True)
    ctx.log(f"The Wi-Fi network {n.wifi_ssid!r} is kept for the installed system.")


def _accounts(ctx: Context) -> None:
    a = ctx.cfg.arch
    groups = ["wheel"] + (["docker"] if "docker" in a.dev_groups else [])
    ctx.chroot("useradd", "-m", "-G", ",".join(groups), "-s", "/bin/bash", a.username)
    ctx.chroot("chpasswd", stdin=f"{a.username}:{a.user_password}\nroot:{a.effective_root_password}\n", secret=True)
    ctx.write("/etc/sudoers.d/10-wheel", "%wheel ALL=(ALL:ALL) ALL\n", 0o440)

    if a.desktop == "hyprland":
        home = f"/home/{a.username}"
        ctx.write(f"{home}/.config/hypr/hyprland.conf", hyprland_config(a.keymap))
        ctx.chroot("chown", "-R", f"{a.username}:{a.username}", f"{home}/.config")


def _home_unlock_key(ctx: Context) -> None:
    """With an encrypted separate /home: unlock it with a key file stored on the (encrypted) root."""
    if not (ctx.cfg.arch.luks_enabled and ctx.layout.get(ROLE_HOME)):
        return
    home_part = ctx.partition(ROLE_HOME)
    key = "/etc/cryptsetup-keys.d/crypthome.key"
    ctx.run("install", "-d", "-m", "0700", ctx.target_path("/etc/cryptsetup-keys.d"))
    ctx.run("dd", "if=/dev/urandom", f"of={ctx.target_path(key)}", "bs=512", "count=8", "status=none")
    ctx.run("chmod", "0400", ctx.target_path(key))
    ctx.run("cryptsetup", "luksAddKey", "--key-file=-", home_part, ctx.target_path(key),
            stdin=ctx.cfg.arch.luks_passphrase, secret=True)
    luks_uuid = ctx.uuid_of(home_part, "<home-luks-uuid>")
    ctx.write("/etc/crypttab", f"{CRYPT_HOME} UUID={luks_uuid} {key} luks\n")


def _build_aur_package(ctx: Context, package: str) -> None:
    script = (f"set -e; cd \"$(mktemp -d)\"; git clone --depth 1 https://aur.archlinux.org/{package}.git; "
              f"cd {package}; makepkg -si --noconfirm")
    ctx.chroot("runuser", "-l", ctx.cfg.arch.username, "-c", script)


def install_aur_helper(ctx: Context) -> None:
    """Build paru or yay from the AUR, then the applications that live there. Never fatal: the AUR
    can be unreachable, and the system is already installed by then."""
    a = ctx.cfg.arch
    helper = extras.effective_aur_helper(a)
    if helper == "none":
        return
    sudoers = "/etc/sudoers.d/99-archinstall-aur"
    # makepkg calls sudo pacman; allow it without a password for the duration of the build only.
    ctx.write(sudoers, f"{a.username} ALL=(ALL) NOPASSWD: ALL\n", 0o440)
    try:
        if not _install_helper(ctx, helper):
            ctx.log(f"WARNING: {helper} could not be installed, so the AUR packages "
                    f"({' '.join(extras.aur_packages(a)) or 'none'}) are skipped. Install them later.")
            return
        for package in extras.aur_packages(a):
            _install_aur_package(ctx, helper, package)
    finally:
        ctx.run("rm", "-f", ctx.target_path(sudoers))


def _install_helper(ctx: Context, helper: str) -> bool:
    a = ctx.cfg.arch
    # The prebuilt "-bin" package takes seconds but may be linked against another libalpm than the
    # installed pacman (paru-bin did not start: libalpm.so.15 missing). The source package takes
    # minutes (it compiles) and always matches. So: try the prebuilt one, check that it starts.
    for package in (f"{helper}-bin", helper):
        try:
            _build_aur_package(ctx, package)
            ctx.chroot("runuser", "-l", a.username, "-c", f"{helper} --version")
            ctx.log(f"The AUR helper {helper} works (installed from {package}).")
            return True
        except CommandError as exc:
            ctx.log(f"WARNING: {package} could not be installed or does not start ({exc}).")
            # A leftover -bin package would conflict with the source package of the same name.
            ctx.chroot("bash", "-c", f"pacman -Qq | grep -E '^{helper}-bin' | xargs -r pacman -Rns --noconfirm",
                       check=False)
    return False


def _install_aur_package(ctx: Context, helper: str, package: str) -> None:
    returncode = ctx.chroot("runuser", "-l", ctx.cfg.arch.username, "-c",
                            f"{helper} -S --needed --noconfirm {package}", check=False)
    if returncode:
        ctx.log(f"WARNING: {package} could not be installed from the AUR (exit code {returncode}). "
                f"Install it later with: {helper} -S {package}")
    else:
        ctx.log(f"{package} installed from the AUR.")
