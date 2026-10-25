from dualboot_installer.layout import (
    GIB,
    MIB,
    ROLE_ESP,
    ROLE_HOME,
    ROLE_ROOT,
    ROLE_SHARED,
    ROLE_WINDOWS,
    STAGING_GIB,
    compute_layout,
    default_sizes,
    partition_path,
)

DISK_512 = 512 * GIB


def layout(disk=DISK_512, sector=512, **overrides):
    args = dict(windows_gib=200, root_gib=64, home_enabled=False, shared_enabled=False, shared_gib=64)
    args.update(overrides)
    return compute_layout(disk, sector, **args)


def test_partition_path_handles_nvme_and_sata():
    assert partition_path("/dev/sda", 1) == "/dev/sda1"
    assert partition_path("/dev/nvme0n1", 3) == "/dev/nvme0n1p3"
    assert partition_path("/dev/mmcblk0", 2) == "/dev/mmcblk0p2"
    assert partition_path("/dev/vda", 2) == "/dev/vda2"


def test_basic_layout_roles_and_order():
    result = layout()
    assert result.ok
    assert [p.role for p in result.partitions] == [ROLE_ESP, ROLE_WINDOWS, ROLE_ROOT, ROLE_SHARED]
    assert [p.number for p in result.partitions] == [1, 2, 3, 4]


def test_partitions_are_contiguous_aligned_and_fit_the_disk():
    for sector in (512, 4096):
        result = layout(sector=sector)
        mib_sectors = MIB // sector
        previous_end = mib_sectors - 1
        for p in result.partitions:
            assert p.start_sector == previous_end + 1
            assert p.start_sector % mib_sectors == 0
            assert (p.end_sector + 1) % mib_sectors == 0
            previous_end = p.end_sector
        assert (previous_end + 1) * sector <= DISK_512 - MIB  # backup GPT fits after the last partition


def test_root_takes_the_remaining_space_without_home():
    result = layout(windows_gib=200)
    root = result.get(ROLE_ROOT)
    expected = DISK_512 // MIB - 2 - 1024 - 200 * 1024 - STAGING_GIB * 1024
    assert root.size_mib == expected


def test_home_takes_the_remaining_space_when_enabled():
    result = layout(home_enabled=True, root_gib=64)
    assert result.get(ROLE_ROOT).size_mib == 64 * 1024
    home = result.get(ROLE_HOME)
    expected = DISK_512 // MIB - 2 - 1024 - 200 * 1024 - 64 * 1024 - STAGING_GIB * 1024
    assert home.size_mib == expected
    assert [p.role for p in result.partitions][-1] == ROLE_SHARED


def test_staging_partition_is_temporary_unless_shared_is_requested():
    assert layout().get(ROLE_SHARED).temporary
    shared = layout(shared_enabled=True, shared_gib=100)
    assert not shared.get(ROLE_SHARED).temporary
    assert shared.get(ROLE_SHARED).size_mib == 100 * 1024


def test_shared_partition_must_hold_the_windows_setup_files():
    result = layout(shared_enabled=True, shared_gib=4)
    assert any("shared partition" in e for e in result.errors)


def test_windows_minimum_size():
    result = layout(windows_gib=40)
    assert any("Windows 11 needs at least" in e for e in result.errors)


def test_layout_that_does_not_fit_reports_an_error_and_no_partitions():
    result = layout(disk=128 * GIB, windows_gib=200)
    assert result.errors
    assert not result.ok


def test_root_minimum_size():
    result = layout(disk=100 * GIB, windows_gib=70)
    assert any("root partition" in e for e in result.errors)


def test_default_sizes_are_consistent_with_the_rules():
    for gib in (128, 256, 512, 1024, 2000):
        sizes = default_sizes(gib * GIB)
        result = compute_layout(gib * GIB, 512, home_enabled=False, shared_enabled=False, **sizes)
        assert result.ok, (gib, result.errors)
