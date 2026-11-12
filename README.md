# tamu-fetch

A tiny fetch-style system information script for [TAMU Linux 1.0D](https://mirror.math.princeton.edu/pub/oldlinux/Linux.old/distributions/TAMU/TAMU-1.0D/), the 1994 Texas A&M Linux release with a Linux 1.0 kernel. It runs in the original guest's Bash 1.13.1-CWRU and reads the guest's actual username, hostname, kernel, architecture, memory, disk use, and uptime. No modern Bash or external packages are required.

This is an independent script, not Neofetch or Fastfetch. It targets the working 1.0D release, not the earlier 1992 media.

![tamu-fetch in the TAMU Linux VM](screenshots/tamu-fetch.png)

## Install in a TAMU Linux guest

Copy `tamu-fetch` into the guest, then run:

```sh
chmod +x tamu-fetch
./tamu-fetch
```

The seven-row ASCII logo, colored labels, and terminal palette use the old console's ANSI colors. Color appears only on a terminal; set `NO_COLOR=1` to disable it.

For my QEMU guest without networking, I copied the script into an offline copy of its virtual disk and verified the installed file matched the repository copy byte for byte before booting. Avoid editing a disk while its VM is running.

The screenshot was captured from a QEMU VM running TAMU Linux 1.0D with XFree86 2.1, FVWM and XFM. The script works from the text console too.

![TAMU Linux 1.0D desktop in QEMU](screenshots/tamu-desktop.png)

## Share image

The [1080 × 1350 Instagram image](social/instagram-post.png) uses the real guest screenshot and labels it as a QEMU VM. Its editable [HTML source](social/instagram-post.html) is included.

## What it reads

- `hostname` and `uname` for the machine name, kernel, and architecture
- `/proc/meminfo` for memory use
- `df -k /` for root disk use
- `/proc/uptime` for uptime

The OS name and release are fixed because this script is specifically for TAMU Linux 1.0D. The ASCII logo is original text; no Texas A&M artwork is included.

## License

MIT. See [LICENSE](LICENSE).
