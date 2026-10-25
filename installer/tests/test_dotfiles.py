"""Desktop shell presets: latest version first, the tested commits as a fallback, sources checked early."""

import pytest

from dualboot_installer import extras
from dualboot_installer.config import InstallConfig
from dualboot_installer.plan import build_plan
from dualboot_installer.runner import Command, CommandError, FileWrite, Runner
from dualboot_installer.steps import dotfiles
from dualboot_installer.steps.context import Context, StepError

from test_plan import dry_run_everything, make_config

URL = "https://github.com/example/shell"
OTHER = "https://github.com/example/dots"

PRESET = extras.Preset(
    "demo", "Demo shell", "a test preset",
    packages=("quickshell",), aur_packages=("demo-cli-git",),
    sources=(URL, OTHER),
    scripts=("20-install.sh", "40-verify.sh"),
    fallback_env=(("DEMO_REF", "a" * 40), ("OTHER_REF", "b" * 40)),
    rollback="restore-default.sh",
    base_config="base.lua",
)


@pytest.fixture(autouse=True)
def demo_preset(monkeypatch, tmp_path):
    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "20-install.sh").write_text("#!/usr/bin/env bash\necho install\n")
    (folder / "40-verify.sh").write_text("#!/usr/bin/env bash\necho verify\n")
    (folder / "restore-default.sh").write_text("#!/usr/bin/env bash\necho restore\n")
    (folder / "base.lua").write_text('kb_layout = "@KB_LAYOUT@"\n')
    (folder / "notes.txt").write_text("not a script\n")
    monkeypatch.setattr(extras, "PRESET_DIR", tmp_path)
    monkeypatch.setitem(extras.DOTFILES, "demo", PRESET)


class ScriptedRunner(Runner):
    """Records everything; `fail` lists substrings of commands that exit with 1, `answers` canned captures."""

    def __init__(self, *, dry_run=False, fail=(), answers=None):
        super().__init__(dry_run=dry_run, log=self._remember)
        self.lines: list[str] = []
        self.fail = list(fail)
        self.canned = answers or {}

    def _remember(self, line: str) -> None:
        self.lines.append(line)

    def run(self, command: Command) -> int:
        self.operations.append(command)
        text = " ".join(command.argv)
        if any(marker in text for marker in self.fail):
            if command.check:
                raise CommandError(command, 1)
            return 1
        return 0

    def capture(self, argv, stdin=None) -> str:
        self.operations.append(Command(argv))
        text = " ".join(argv)
        for marker, answer in self.canned.items():
            if marker in text:
                return answer
        return ""

    def write_file(self, op) -> None:
        self.operations.append(op)


def make_ctx(runner: Runner, **arch_fields) -> Context:
    cfg = InstallConfig()
    cfg.arch.username, cfg.arch.dotfiles = "alice", "demo"
    for key, value in arch_fields.items():
        setattr(cfg.arch, key, value)
    cfg.disk.device, cfg.disk.size_bytes, cfg.disk.windows_gib = "/dev/vda", 300 * 1024**3, 100
    return Context(cfg, cfg.layout(), runner)


def commands(runner: Runner) -> list[str]:
    return [" ".join(op.argv) for op in runner.operations if isinstance(op, Command)]


def runs(runner: Runner, script: str) -> list[str]:
    return [c for c in commands(runner) if c.endswith(f"/{script}")]


# -- the registry feeds the package lists ---------------------------------------------------------------------


def test_a_preset_adds_its_packages_and_the_aur_helper():
    a = make_ctx(ScriptedRunner()).cfg.arch
    assert extras.selected_preset(a) is PRESET
    assert extras.official_packages(a) == ["git", "quickshell"]
    assert extras.aur_packages(a) == ["demo-cli-git"]
    assert extras.effective_aur_helper(a) == "yay"


def test_a_preset_needs_the_hyprland_desktop():
    a = make_ctx(ScriptedRunner(), desktop="minimal").cfg.arch
    assert extras.selected_preset(a) is None and extras.official_packages(a) == []


def test_the_plan_installs_the_desktop_shell_after_the_aur_step():
    cfg = InstallConfig()
    ids = [step.key for step in build_plan(cfg, cfg.layout())]
    assert ids.index("dotfiles") == ids.index("aur") + 1


# -- installing: the latest version first --------------------------------------------------------------------


def test_nothing_is_done_without_a_preset():
    runner = ScriptedRunner()
    dotfiles.install_dotfiles(make_ctx(runner, dotfiles="none"))
    assert runner.operations == []


def test_the_scripts_are_copied_into_the_new_system_and_run_in_order_as_the_user_with_no_pin():
    runner = ScriptedRunner()
    dotfiles.install_dotfiles(make_ctx(runner))
    written = {op.path: op for op in runner.operations if isinstance(op, FileWrite)}
    script = written["/mnt/usr/local/share/archinstall/presets/demo/20-install.sh"]
    assert script.mode == 0o755 and "echo install" in script.content
    assert written["/mnt/usr/local/share/archinstall/presets/demo/notes.txt"].mode == 0o644
    install, verify = runs(runner, "20-install.sh"), runs(runner, "40-verify.sh")
    assert len(install) == 1 and len(verify) == 1
    assert commands(runner).index(install[0]) < commands(runner).index(verify[0])
    assert install[0].startswith("arch-chroot /mnt runuser -l alice -c timeout 60m env KB_LAYOUT=us bash ")
    assert "DEMO_REF" not in install[0]  # the latest commit, not a pinned one
    assert any("is installed (latest version)" in line for line in runner.lines)
    assert not runs(runner, "restore-default.sh")


def test_passwordless_sudo_exists_only_while_the_preset_installs():
    runner = ScriptedRunner()
    dotfiles.install_dotfiles(make_ctx(runner))
    ops = runner.operations
    wrote = next(i for i, op in enumerate(ops) if isinstance(op, FileWrite) and "99-archinstall-aur" in op.path)
    removed = next(i for i, op in enumerate(ops) if isinstance(op, Command) and op.argv[:2] == ["rm", "-f"])
    first_script = next(i for i, op in enumerate(ops) if isinstance(op, Command) and "20-install.sh" in " ".join(op.argv))
    assert wrote < first_script < removed


# -- the safeguards ------------------------------------------------------------------------------------------


def test_when_the_latest_fails_the_preset_is_rolled_back_and_retried_with_the_tested_commits():
    runner = ScriptedRunner()
    original = runner.run
    state = {"first": True}

    def run(command):
        if "40-verify.sh" in " ".join(command.argv) and state["first"]:
            state["first"] = False
            runner.fail = ["40-verify.sh"]
            code = original(command)
            runner.fail = []
            return code
        return original(command)

    runner.run = run  # type: ignore[method-assign]
    dotfiles.install_dotfiles(make_ctx(runner))
    installs = runs(runner, "20-install.sh")
    assert len(installs) == 2 and "DEMO_REF" not in installs[0]
    assert f"DEMO_REF={'a' * 40}" in installs[1] and f"OTHER_REF={'b' * 40}" in installs[1]
    assert len(runs(runner, "restore-default.sh")) == 1  # rolled back between the two attempts
    assert any("trying the versions that were tested" in line for line in runner.lines)
    assert any("the last tested versions, not the latest" in line for line in runner.lines)


def test_when_nothing_works_the_default_setup_is_back_and_nothing_is_raised():
    runner = ScriptedRunner(fail=["20-install.sh"])
    dotfiles.install_dotfiles(make_ctx(runner))  # does not raise
    assert len(runs(runner, "20-install.sh")) == 2  # latest, then the tested commits
    assert len(runs(runner, "restore-default.sh")) == 2
    assert any("could not be installed. The default Hyprland setup is back" in line for line in runner.lines)
    text = commands(runner)
    assert any(c.startswith("rm -f") and "99-archinstall-aur" in c for c in text)  # sudoers removed
    assert "rm -rf /home/alice/.cache/yay /home/alice/.cache/paru" in text[-1]  # and the build caches


def test_a_preset_without_tested_commits_is_not_retried():
    bare = extras.Preset("demo2", "Bare", scripts=("go.sh",), rollback="restore-default.sh")
    extras.DOTFILES["demo2"] = bare
    try:
        (extras.PRESET_DIR / "demo2").mkdir(exist_ok=True)
        (extras.PRESET_DIR / "demo2" / "go.sh").write_text("#!/usr/bin/env bash\n")
        (extras.PRESET_DIR / "demo2" / "restore-default.sh").write_text("#!/usr/bin/env bash\n")
        runner = ScriptedRunner(fail=["go.sh"])
        dotfiles.install_dotfiles(make_ctx(runner, dotfiles="demo2"))
        assert len(runs(runner, "go.sh")) == 1
    finally:
        del extras.DOTFILES["demo2"]


# -- the Lua form of the default setup ------------------------------------------------------------------------


def test_a_preset_with_a_lua_base_gets_hyprland_lua_with_the_keyboard_layout_and_no_conf():
    cfg = make_config(dotfiles="demo", keymap="fr")
    runner, _ = dry_run_everything(cfg)
    written = {op.path: op for op in runner.operations if isinstance(op, FileWrite)}
    lua = written["/mnt/home/alice/.config/hypr/hyprland.lua"]
    assert lua.content == 'kb_layout = "fr"\n'
    assert "/mnt/home/alice/.config/hypr/hyprland.conf" not in written


def test_without_a_preset_the_usual_hyprland_conf_is_written():
    runner, _ = dry_run_everything(make_config())
    written = {op.path for op in runner.operations if isinstance(op, FileWrite)}
    assert "/mnt/home/alice/.config/hypr/hyprland.conf" in written
    assert "/mnt/home/alice/.config/hypr/hyprland.lua" not in written


# -- before the disk is erased -------------------------------------------------------------------------------


def test_the_sources_are_only_announced_in_a_dry_run():
    runner = ScriptedRunner(dry_run=True)
    dotfiles.check_sources(make_ctx(runner))
    assert not any("curl" in c for c in commands(runner))
    assert any("Would check" in line for line in runner.lines)


def aur_answer(*names: str) -> str:
    return '{"resultcount":%d,"results":[%s]}' % (len(names), ",".join('{"Name":"%s"}' % n for n in names))


def test_reachable_sources_pass():
    runner = ScriptedRunner(answers={"aur.archlinux.org": aur_answer("demo-cli-git")})
    dotfiles.check_sources(make_ctx(runner))
    probes = [c for c in commands(runner) if "info/refs?service=git-upload-pack" in c]
    assert len(probes) == 2 and probes[0].startswith("curl") and URL in probes[0]


def test_an_unreachable_repository_stops_everything_before_the_erase():
    runner = ScriptedRunner(fail=["example/dots"], answers={"aur.archlinux.org": aur_answer("demo-cli-git")})
    with pytest.raises(StepError, match=r"cannot be downloaded right now.*example/dots.*Nothing was changed"):
        dotfiles.check_sources(make_ctx(runner))


def test_a_missing_aur_package_stops_everything_before_the_erase():
    runner = ScriptedRunner(answers={"aur.archlinux.org": aur_answer()})
    with pytest.raises(StepError, match="AUR packages cannot be found: demo-cli-git"):
        dotfiles.check_sources(make_ctx(runner))


def test_a_missing_application_only_warns():
    runner = ScriptedRunner(answers={"aur.archlinux.org": aur_answer("postman-bin")})
    dotfiles.check_sources(make_ctx(runner, dotfiles="none", apps=["vscodium", "postman"]))  # does not raise
    warnings = [line for line in runner.lines if "WARNING" in line]
    assert warnings == ["WARNING: the AUR package vscodium-bin could not be found; it will be skipped."]


def test_nothing_is_checked_when_nothing_is_downloaded():
    runner = ScriptedRunner()
    dotfiles.check_sources(make_ctx(runner, dotfiles="none", apps=["librewolf"]))  # official package only
    assert runner.operations == []


# -- the real MacShell preset --------------------------------------------------------------------------------


@pytest.fixture
def real_presets(monkeypatch):
    monkeypatch.undo()  # the autouse fixture redirected PRESET_DIR to a temporary folder


def test_the_macshell_preset_ships_every_file_it_names(monkeypatch):
    monkeypatch.undo()
    preset = extras.DOTFILES["macshell"]
    folder = extras.PRESET_DIR / "macshell"
    for name in (*preset.scripts, preset.rollback, preset.base_config, "packages-official.txt"):
        assert (folder / name).is_file(), name
    for name in (*preset.scripts, preset.rollback):
        assert (folder / name).read_text(encoding="utf-8").startswith("#!/usr/bin/env bash"), name
        assert "\r" not in (folder / name).read_text(encoding="utf-8")


def test_the_macshell_preset_follows_the_latest_commits_and_knows_the_tested_ones(monkeypatch):
    monkeypatch.undo()
    preset = extras.DOTFILES["macshell"]
    assert all(url.startswith("https://github.com/") for url in preset.sources)
    assert {key for key, _ in preset.fallback_env} == {"MACSHELL_REF", "MACTAHOE_REF", "MACOSLIKE_REF"}
    assert all(len(value) == 40 and set(value) <= set("0123456789abcdef") for _, value in preset.fallback_env)
    text = " ".join((extras.PRESET_DIR / "macshell" / name).read_text(encoding="utf-8") for name in preset.scripts)
    assert "git clone -q --depth 1" in text  # HEAD of the default branch when no *_REF is given


def test_the_macshell_packages_come_from_its_list_and_include_what_the_recipe_found_missing(monkeypatch):
    monkeypatch.undo()
    packages = extras.DOTFILES["macshell"].packages
    for expected in ("quickshell", "awww", "hyprlock", "python-numpy", "bluez-utils", "python-pillow", "librsvg"):
        assert expected in packages
    assert all(name == name.strip() and not name.startswith("#") for name in packages)


# -- the real Caelestia preset -------------------------------------------------------------------------------


def test_the_caelestia_preset_ships_every_file_it_names(monkeypatch):
    monkeypatch.undo()
    preset = extras.DOTFILES["caelestia"]
    folder = extras.PRESET_DIR / "caelestia"
    for name in (*preset.scripts, preset.rollback, "packages-official.txt"):
        assert (folder / name).is_file(), name
    for name in (*preset.scripts, preset.rollback):
        text = (folder / name).read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash") and "\r" not in text, name


def test_caelestia_needs_yay_whatever_helper_was_chosen_and_pipewire_jack_before_any_aur_build(monkeypatch):
    monkeypatch.undo()
    for chosen in ("none", "paru", "yay"):
        a = cfg_arch(dotfiles="caelestia", aur_helper=chosen)
        assert extras.effective_aur_helper(a) == "yay"
    a = cfg_arch(dotfiles="caelestia")
    assert extras.aur_packages(a) == ["caelestia-cli"]
    packages = extras.official_packages(a)
    assert {"pipewire-jack", "gammastep", "geoclue", "git"} <= set(packages)
    assert "jack2" not in packages


def test_caelestia_retries_with_the_tested_dots_commit(monkeypatch):
    monkeypatch.undo()
    preset = extras.DOTFILES["caelestia"]
    ((key, value),) = preset.fallback_env
    assert key == "CAELESTIA_DOTS_REF" and len(value) == 40
    assert "CAELESTIA_DOTS_REF" in (extras.PRESET_DIR / "caelestia" / "20-caelestia.sh").read_text(encoding="utf-8")


def test_the_preflight_also_checks_what_caelestia_install_pulls_in(monkeypatch):
    monkeypatch.undo()
    runner = ScriptedRunner(answers={"aur.archlinux.org": aur_answer("caelestia-cli")})
    with pytest.raises(StepError, match="AUR packages cannot be found: .*quickshell-git"):
        dotfiles.check_sources(make_ctx(runner, dotfiles="caelestia"))
    probes = [c for c in commands(runner) if "info/refs" in c]
    assert any("git.outfoxxed.me" in c for c in probes) and any("caelestia-dots/caelestia" in c for c in probes)


def cfg_arch(**fields):
    cfg = InstallConfig()
    for key, value in fields.items():
        setattr(cfg.arch, key, value)
    return cfg.arch


# -- the real Revo Shell preset ------------------------------------------------------------------------------


def test_the_revo_preset_ships_every_file_it_names(monkeypatch):
    monkeypatch.undo()
    preset = extras.DOTFILES["revo"]
    folder = extras.PRESET_DIR / "revo"
    for name in (*preset.scripts, preset.rollback, "packages-official.txt"):
        assert (folder / name).is_file(), name
    for name in (*preset.scripts, preset.rollback):
        text = (folder / name).read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash") and "\r" not in text, name


def test_revo_never_runs_the_projects_install_script_unattended_without_its_safeguards(monkeypatch):
    monkeypatch.undo()
    script = (extras.PRESET_DIR / "revo" / "20-revo.sh").read_text(encoding="utf-8")
    assert "--shells" in script and "</dev/null" in script  # install.sh refuses to run without them
    assert "git clone -q --depth 1 --filter=blob:none --sparse" in script  # the repository is 2.3 GB
    assert "find \"$HOME/.config\" -type l -lname '/home/*'" in script  # the absolute links of the author's home
    assert "powerprofilesctl set performance" in script  # removed from the autostart (battery)
    assert "config ok" in script  # install.sh's own exit code is not trusted
    assert "hyprpm add" not in script and "hyprpm update" not in script  # no third-party plugins


def test_the_revo_packages_use_the_names_that_exist_today(monkeypatch):
    monkeypatch.undo()
    packages = extras.DOTFILES["revo"].packages
    for gone in ("mpvpaper", "python-pywal", "ttf-rubik", "ttf-iosevka", "ttf-firacode", "swww"):
        assert gone not in packages
    for present in ("quickshell", "awww", "syntax-highlighting", "ttf-fira-code", "ttf-iosevka-nerd", "hyprpm"):
        assert present in packages
    assert set(extras.DOTFILES["revo"].aur_packages) == {"mpvpaper", "python-pywal16", "ttf-rubik-vf",
                                                          "kde-material-you-colors"}


def test_the_presets_are_listed_in_the_order_the_owner_asked(monkeypatch):
    monkeypatch.undo()
    assert list(extras.DOTFILES) == ["none", "macshell", "revo", "caelestia"]
    assert len(extras.DOTFILES["revo"].fallback_env) == 1


# -- services, clone caches and the rollback markers ---------------------------------------------------------


def test_the_services_a_preset_needs_are_enabled_with_the_system(monkeypatch):
    monkeypatch.undo()
    runner, _ = dry_run_everything(make_config(dotfiles="macshell"))
    enable = next(c for c in commands(runner) if "systemctl enable" in c)
    assert "bluetooth.service" in enable and "power-profiles-daemon.service" in enable
    assert "bluez" in extras.DOTFILES["macshell"].packages  # the daemon itself (bluez-utils is only the tools)
    for preset_id in ("macshell", "revo", "caelestia"):
        preset = extras.DOTFILES[preset_id]
        assert "bluez" in preset.packages and "power-profiles-daemon" in preset.packages, preset_id
        assert "bluetooth.service" in preset.services, preset_id


def test_no_preset_services_without_a_preset():
    runner, _ = dry_run_everything(make_config(laptop=False))
    enable = next(c for c in commands(runner) if "systemctl enable" in c)
    assert "bluetooth.service" not in enable and "power-profiles-daemon.service" not in enable


def test_the_clone_caches_are_deleted_only_after_the_preset_installed(monkeypatch):
    cleaning = extras.Preset("demo3", "Cleaning", scripts=("go.sh",), rollback="restore-default.sh",
                             cleanup=(".cache/archinstall",))
    extras.DOTFILES["demo3"] = cleaning
    try:
        (extras.PRESET_DIR / "demo3").mkdir(exist_ok=True)
        (extras.PRESET_DIR / "demo3" / "go.sh").write_text("#!/usr/bin/env bash\n")
        (extras.PRESET_DIR / "demo3" / "restore-default.sh").write_text("#!/usr/bin/env bash\n")
        ok = ScriptedRunner()
        dotfiles.install_dotfiles(make_ctx(ok, dotfiles="demo3"))
        assert any('rm -rf "$HOME/".cache/archinstall' in c for c in commands(ok))
        failing = ScriptedRunner(fail=["go.sh"])
        dotfiles.install_dotfiles(make_ctx(failing, dotfiles="demo3"))
        assert not any('rm -rf "$HOME/".cache/archinstall' in c for c in commands(failing))  # logs stay for a bug report
    finally:
        del extras.DOTFILES["demo3"]


def test_macshell_cleans_its_clones_and_the_others_keep_theirs_until_proven_safe(monkeypatch):
    monkeypatch.undo()
    assert extras.DOTFILES["macshell"].cleanup == (".cache/archinstall",)
    assert extras.DOTFILES["caelestia"].cleanup == ()  # its dots clone lives in ~/.local/state and is kept
    assert extras.DOTFILES["revo"].cleanup == (".cache/archinstall", ".cache/pip")  # proven not to be linked to


def test_a_rolled_back_default_autostart_is_marked_and_removed_again_before_macshell_installs(monkeypatch):
    monkeypatch.undo()
    folder = extras.PRESET_DIR / "macshell"
    restore = (folder / "restore-default.sh").read_text(encoding="utf-8")
    install = (folder / "20-macshell.sh").read_text(encoding="utf-8")
    assert "-- >>> ArchInstall default autostart" in restore and "-- <<< ArchInstall default autostart" in restore
    assert "ArchInstall default autostart" in install and "sed -i" in install
    assert install.index("ArchInstall default autostart") < install.index('bash "$SRC/install.sh"')


# -- the person's keyboard layout reaches the scripts ----------------------------------------------------------


def test_every_script_gets_the_keyboard_layout_of_the_wizard():
    runner = ScriptedRunner()
    dotfiles.install_dotfiles(make_ctx(runner))
    ctx_fr = make_ctx(ScriptedRunner(), keymap="fr")
    dotfiles.install_dotfiles(ctx_fr)
    assert all("KB_LAYOUT=fr " in c for c in commands(ctx_fr.runner) if c.endswith(("20-install.sh", "40-verify.sh")))


def test_the_fallback_attempt_keeps_the_layout_next_to_the_tested_commits():
    runner = ScriptedRunner(fail=["20-install.sh"])
    dotfiles.install_dotfiles(make_ctx(runner, keymap="de-latin1"))
    retry = runs(runner, "20-install.sh")[1]
    assert f"DEMO_REF={'a' * 40}" in retry and "KB_LAYOUT=de" in retry


def test_caelestia_writes_the_layout_where_its_dots_never_overwrite(monkeypatch):
    monkeypatch.undo()
    script = (extras.PRESET_DIR / "caelestia" / "20-caelestia.sh").read_text(encoding="utf-8")
    assert "hypr-user.lua" in script and 'kb_layout = \\"$KB_LAYOUT\\"' in script
    assert script.index("hypr-user.lua") > script.index("caelestia install --noconfirm")  # after the dots deployed
    assert script.index("hypr-user.lua") < script.index("RECIPE-RESULT preset=caelestia")


def test_the_caelestia_rollback_restores_the_whole_configuration(monkeypatch):
    monkeypatch.undo()
    text = (extras.PRESET_DIR / "caelestia" / "restore-default.sh").read_text(encoding="utf-8")
    assert 'cp -a "$HOME/.config.bak" "$HOME/.config"' in text and "~/.config/hypr" not in text.split("set -u")[1]


def test_revo_uses_the_installers_keyboard_layout_and_does_not_download_the_whole_wallpaper_folder(monkeypatch):
    monkeypatch.undo()
    script = (extras.PRESET_DIR / "revo" / "20-revo.sh").read_text(encoding="utf-8")
    assert 'KB="${KB_LAYOUT:-' in script and "configs/input.lua" in script and "settings.lua" in script
    assert "api.github.com/repos/vzbc/revo-shell/git/trees/" in script  # sizes without fetching every blob
    assert "git ls-tree -r -l HEAD wallpapers" not in script  # on a partial clone it downloads every file (860 MB)
    assert "systemsettings.png" in script  # the menu icon no longer points into the clone that is deleted
