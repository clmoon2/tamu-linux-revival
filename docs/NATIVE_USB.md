# Native UEFI boot on a modern x86-64 computer

This path boots the **original TAMU Linux 1.0D userland** directly on a computer, without QEMU. Its 1994 Linux 1.0 kernel cannot drive UEFI, NVMe, and current USB hardware. The USB image therefore uses a patched Linux 4.19.325 kernel to run TAMU's original 32-bit a.out programs. The kernel is end of life. This is a preservation demo, not a secure daily driver.

The image boots from USB or from an internal drive copied from that USB image. It provides a text console, original root shell, and `tamu-fetch`. The old XFree86 VGA16 desktop is only verified in the original QEMU VM; do not expect it to work with a current laptop GPU. The guest has a root account with no password. Keep it offline and disconnected from networks.

## Tested result and limits

The source-built image has been tested in QEMU with x86-64 UEFI firmware and a USB 3 storage device, reaching the original TAMU login, original Bash, `tamu-fetch`, and clean poweroff. A separate QEMU NVMe boot verifies the internal-drive route. A physical laptop has **not** been available to test. An unknown future model cannot be guaranteed: its keyboard, display, firmware, and power controls may need model-specific work. USB keyboard and an external display are useful fallbacks.

## Build the image on Linux Mint or Ubuntu

Install the original VM builder's dependencies and the native builder's tools:

```sh
sudo apt install python3 qemu-system-x86 qemu-utils tesseract-ocr e2fsprogs \
  gcc-12 make flex bison libelf-dev bc openssl libssl-dev \
  binutils patch file xz-utils gdisk dosfstools mtools grub-efi-amd64-bin \
  grub-common cpio busybox-static ovmf
python3 tools/build.py
python3 tools/build-native.py
python3 tools/test-native.py
sha256sum build/tamu-native-usb.img
```

The first builder downloads all original floppy images, checks their pinned SHA-256 values, and installs them in QEMU. The native builder downloads Linux 4.19.325 source, checks its pinned SHA-256, applies the [a.out compatibility patch](../patches/0001-ia32-aout-exec-zmagic.patch), builds the checked-in [kernel configuration](../config/native-kernel-4.19.325.config), and creates a 384 MiB GPT image with a 64 MiB EFI partition and 256 MiB ext2 root. It copies the installed TAMU system into the ext2 partition; original media and generated binaries are not committed to this repository. The build needs several gigabytes of temporary free space and takes longer than the original VM build. The output is never overwritten; use `--output` for another build. `--base-image` accepts a previously built TAMU qcow2 image, and `--kernel-tarball` accepts a locally cached source tarball after the same hash check. The adjacent `.manifest.json` records the image and input hashes.

The smoke test boots a disposable copy through QEMU/OVMF as USB storage, then clones it to a disposable 2 GiB NVMe disk and boots again. It checks login, original shell, fetch output, clean guest poweroff, and ext2 integrity. It never writes a host block device. Screenshots and logs are saved under `build/native-test-*`.

## First boot from a USB stick

Use a spare USB stick of at least 1 GB. **The write destroys its existing contents.** Identify it from its size, model, and connection type each time:

```sh
lsblk -o NAME,SIZE,MODEL,TRAN,TYPE,MOUNTPOINTS
```

Unmount every mounted partition shown for the stick. Replace `/dev/sdX` below with the **whole USB device** you identified, not a numbered partition and not your computer's internal drive:

```sh
lsblk -ln -o PATH /dev/sdX | sed '1d' | while IFS= read -r partition; do
  if findmnt -rn -S "$partition" >/dev/null; then
    sudo umount "$partition" || exit 1
  fi
done
lsblk -o PATH,MOUNTPOINTS /dev/sdX
```

Stop if any mountpoint remains. Once the device and its empty mountpoints are confirmed, write the image:

```sh
sudo dd if=build/tamu-native-usb.img of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

Select the USB stick in the laptop's one-time UEFI boot menu. Disable Secure Boot for this unsigned preservation kernel. Log in as `root` with no password. Run `uname -a` and `tamu-fetch`. On some firmware the console may initially look blank; try Ctrl+Alt+F1, then Enter. Shut down with `/sbin/tamu-poweroff`; the original `halt` and `shutdown` commands do not leave this compatibility boot's ext2 filesystem clean.

If it fails to boot, photograph the last screen and record the exact laptop model, CPU, firmware mode, keyboard behavior, and USB-stick model. Do not install it to the internal drive until USB boot, login, fetch, and clean poweroff all work.

## Optional installation to an internal drive

The same image can boot from an NVMe drive. This is a **whole-drive replacement** and erases the target's existing partition table and data. Prepare it from a separate Linux live USB after the TAMU USB trial succeeds. Back up anything needed, identify the exact internal device with `lsblk`, and substitute its whole-device path for `/dev/nvmeXnY`. Unmount its partitions and mapped volumes; stop if any mountpoint remains:

```sh
lsblk -o NAME,SIZE,MODEL,TRAN,TYPE,MOUNTPOINTS
lsblk -o PATH,MOUNTPOINTS /dev/nvmeXnY
```

Only after confirming the entire target is unmounted, copy the image:

```sh
sudo sgdisk --zap-all /dev/nvmeXnY
sudo dd if=build/tamu-native-usb.img of=/dev/nvmeXnY bs=4M status=progress conv=fsync
sudo sgdisk -e /dev/nvmeXnY
sync
```

The command leaves unused space after the small TAMU partitions. Keep the small layout for the first physical boot. It also leaves the live USB and the source image as recovery paths. This internal-drive sequence has been checked only on a disposable QEMU NVMe disk; do not treat it as validation for a laptop that has not arrived.

Unplug the TAMU USB stick **and every other copy of this image** before booting the internal drive. A byte-for-byte clone has the same EFI label and root partition name; with two copies attached, the loader could mount the wrong root and write to the wrong device. The first internal boot is verified only when it is the sole TAMU image attached.

## Why the compatibility patch exists

TAMU's `init` and Bash use 1994 i386 ZMAGIC a.out. Linux 4.19 provides x86-64 `CONFIG_IA32_AOUT`. Its loader places this old program text in an anonymous page near address zero; modern NX behavior otherwise faults immediately. The small patch sets `READ_IMPLIES_EXEC` for these a.out processes. The kernel config permits the old low mappings. These compatibility settings reduce hardening, so this environment must stay offline.
