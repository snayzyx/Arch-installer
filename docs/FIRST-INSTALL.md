# First installation on a laptop: checklist

Nothing here was tried on real hardware (see [RESULTS.md](RESULTS.md)). Read it once before you
start; the disk you choose is **erased completely**.

## Before

1. **Back up everything** on the target disk (or use a spare disk). Have your Windows 11 licence
   key at hand if you bought one: the installer uses the key stored in the firmware if there is one.
2. **Build the ISO** (Docker Desktop running), from this folder:
   ```powershell
   .\build.ps1 -SkipImage -WindowsIso .\ISO\<your Windows 11 ISO>.iso
   ```
   The offline ISO is about 10.7 GB and appears in `dist\`. Leave out `-SkipImage` the first time
   on a machine that has never built it (it builds the Docker builder image first). To build
   **and** check the result (installer files identical to the repository, shim hash, Windows
   image), use `.\scripts\rebuild-all.ps1 -SkipImage -WindowsIso .\ISO\<your Windows 11 ISO>.iso`.
3. **Write it to a USB stick of 16 GB or more** with Rufus in **DD mode** ("DD Image"), or copy the
   file to a Ventoy stick. A plain "ISO mode" copy to a FAT32 stick cannot work.
4. **Firmware setup of the laptop** (usually F2, F10, F12 or Del at power-on):
   - UEFI mode (not legacy/CSM).
   - Storage mode **AHCI**. If the laptop uses Intel RST / VMD / "RAID", Windows Setup cannot see the
     disk: switch it first (the wizard shows a "Storage controller" warning in that case).
   - **Secure Boot off** for now: the live ISO is not signed. It is turned on again at the end.
   - Plug in the charger (the wizard warns when the laptop runs on battery).
5. Network: Wi-Fi is fine (the wizard connects and keeps the network for the installed system);
   Arch packages are downloaded during the installation.

## The installation

1. Boot the stick (boot menu key, often F12). The wizard starts by itself.
2. Answer the questions. Check the **Summary** screen carefully: it names the disk that will be
   erased. Type the confirmation it asks for.
3. The installer partitions the disk and installs Arch Linux (about 5-10 minutes with a good
   connection). When it says it is finished, press **Reboot now** and remove the USB stick.
4. **Do not touch the keyboard.** The first restart starts Windows Setup on its own: it takes about
   15-30 minutes depending on the machine (about 11 in the test VM) and restarts several times. It ends on the Windows lock screen.
5. The next restart shows a **blue screen (MokManager)**. You have **10 seconds** to press a key,
   then: *Enroll MOK*, *Continue*, *Yes*, type the password you chose in the wizard, *Reboot*.
   If you miss it, Arch's boot queues the request again (see RESULTS.md, "Things to know").
6. Enter the firmware setup and **turn Secure Boot back on**. The GRUB menu lists Arch Linux
   (default, after 5 seconds) and Windows 11.

## If something goes wrong

- The installer log is `/var/log/archinstall.log` on the live system; to try again, run
  `archinstall-launcher`. `archinstall-launcher --partial` installs Arch Linux and GRUB only, which
  is a good first try on a new machine.
- Windows Setup does not start after the first restart: check the boot order in the firmware; the
  entry "Windows 11 Setup" must come first, then `ArchInstall`.
- Shim shows "Verification failed" at a boot: the key was not enrolled. Turn Secure Boot off,
  start Arch (it queues the enrollment again), restart and press a key within 10 seconds.
- Windows has no Wi-Fi after its installation: a stock Windows ISO does not carry every Wi-Fi driver. The
  installer keeps the Wi-Fi, network and Bluetooth drivers of the Windows that was on the disk before (read
  while the disk still holds it, then given to the new Windows), but not when that Windows was encrypted with
  BitLocker or was already gone. Connect an Ethernet cable, or your phone by USB (tethering), once: Windows
  Update installs the driver. To avoid it next time, build the ISO with your laptop's driver packages:
  `.\build.ps1 -DriversDir <folder>` (extracted: the `.inf`, `.cat` and `.sys` files). The list of what
  Windows received is in `C:\Windows\Temp\archinstall-finish.log`.
- Keep the USB stick: the installer can be run again from it.
