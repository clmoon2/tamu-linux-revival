# Rebuilding TAMU Linux 1.0D

## Sources and scope

TAMU's [original announcement](https://mirror.math.princeton.edu/pub/oldlinux/Linux.old/distributions/TAMU/TAMU-1.0D/ANNOUNCE) describes the 1.0D binary release as one boot disk plus 21 data disks. Its [installation instructions](https://mirror.math.princeton.edu/pub/oldlinux/Linux.old/distributions/TAMU/TAMU-1.0D/bin/INSTALL.BIN) describe the menu-driven installer. This project retrieves the archived 3.5-inch images from Princeton's [OldLinux mirror](https://mirror.math.princeton.edu/pub/oldlinux/Linux.old/distributions/TAMU/TAMU-1.0D/bin/3inch/). The SHA-256 list was made from the mirror files for this project. It detects drift or damage relative to those files; it does not authenticate the original 1994 publication.

This rebuild targets **1.0D**, dated 1994, rather than TAMU's earlier releases. The guest itself reports Linux 1.0, i486, and GNU Bash 1.13.1-CWRU. The announcement's Bash 1.13.5 line differs from the installed guest's version string. We report the observed guest value without assuming why they differ.

No original floppy image or built VM disk is stored in this repository. We have not established a single distribution-wide grant to redistribute every archived package. The MIT license here applies to the new code, minimal X configuration, and documentation we wrote; original packages retain their own terms.

## The install we automated

The guest has no serial installer. `tools/build.py` starts an emulated 486 with 32 MiB RAM, a new 256 MiB IDE disk, the original boot image in its floppy drive, QEMU's legacy Cirrus VGA device, no network interface, and a 1994 guest clock. It uses the QEMU monitor's `screendump`, `sendkey`, and floppy `change` commands. Tesseract reads each screenshot so the script waits for the actual next prompt. Every wait has a deadline and preserves a screenshot and OCR text on timeout.

The [original installer](https://mirror.math.princeton.edu/pub/oldlinux/Linux.old/distributions/TAMU/TAMU-1.0D/bin/INSTALL.BIN) is answered as follows:

| Prompt | Choice |
| --- | --- |
| Install a full distribution set | Yes |
| Install floppy drive | Drive A: 3.5 inch |
| Run fdisk manually | No; use its default empty `/dev/hda1` |
| Root partition | `/dev/hda1` |
| Swap file | No; 32 MiB guest RAM |
| Install all packages | Yes |
| BOOTACTV partition selector | No |
| LILO location | Default `/dev/hda` MBR |
| Hostname | Default `tamu` |
| Networking | No |
| Erase the new root partition | Yes, only after QEMU created the new disk |

The script switches in `bin.01` through `bin.21` only when the installer requests each one. It waits for `restore complete`, ejects the floppy, and reboots from the hard disk. It logs in, halts cleanly, and then patches only the offline copy of the new guest filesystem. Source media hashes are checked again afterward.

## Why the X patch exists

The complete original install has XFree86 2.1, FVWM, XFM, and several X servers, but `startx` initially reports **“No config file found?”**. The installer selects `XF86_SVGA` by default. That server did not recognize the tested QEMU Cirrus revision in our earlier manual attempt. The shipped `XF86_VGA16` server works with a small [Xconfig.tamu](../config/Xconfig.tamu): PS/2 mouse at `/dev/psaux`, generic VGA16 mode 33, 640 × 480. The builder changes the installed `/usr/X386/bin/X` symlink to `XF86_VGA16` and adds the config. It does not replace the historical X binaries.

The builder also puts [tamu-fetch](../tamu-fetch) in `/usr/bin`, which is on the original guest's `PATH`. It uses Bash-era syntax and the old guest's existing `whoami`, `hostname`, `uname`, `awk`, `df`, `cat`, and `printf`. On Linux 1.0, `awk` could not read the proc files directly in our tests, so the script pipes `cat /proc/...` to `awk`.

## Verification and reproducibility

After patching, a second boot verifies the observed kernel and Bash versions, runs `tamu-fetch` and checks its fields, starts X at 640 × 480 and recognizes the XFM menu, exits X, and halts cleanly. The host then runs read-only `qemu-img check` and `e2fsck -fn` against the final disk before publishing it to the requested local output path. A `build/session-*/manifest.json` records input hashes, patch hashes, QEMU version, checks performed, and final image hash. The full session also keeps installer, boot, fetch, desktop, and version screenshots.

“Reproducible” here means the build can be repeated from the pinned archived media on a clean host setup. It does **not** promise a bit-identical qcow2: guest timestamps, install timing, filesystem allocation, and QEMU versions can change output bytes. The tested host used QEMU 8.2.2 on Linux. The guest is an emulated 486, not a native install on modern laptop hardware. We make no claim that this is the first TAMU Linux revival.
