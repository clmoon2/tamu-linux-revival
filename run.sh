#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
disk="$root/build/tamu-1.0d.qcow2"
port=5911

while (($#)); do
    case "$1" in
        --disk)
            if (($# < 2)); then
                printf '%s requires a path.\n' "$1" >&2
                exit 2
            fi
            disk=$2
            shift 2
            ;;
        --vnc-port)
            if (($# < 2)); then
                printf '%s requires a port.\n' "$1" >&2
                exit 2
            fi
            port=$2
            shift 2
            ;;
        -h|--help)
            printf 'Usage: %s [--disk PATH] [--vnc-port 5900..5999]\n' "$0"
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

if [[ ! -f "$disk" ]]; then
    printf 'No VM image at %s. Run python3 tools/build.py first.\n' "$disk" >&2
    exit 1
fi
if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 5900 || port > 5999)); then
    printf 'VNC port must be between 5900 and 5999.\n' >&2
    exit 2
fi

printf 'TAMU Linux 1.0D: VNC 127.0.0.1::%s. Log in as root; run tamu-fetch or startx.\n' "$port"
printf 'After halting the guest, enter quit in the QEMU monitor below.\n'
exec qemu-system-i386 \
    -machine pc -cpu 486 -m 32 \
    -drive "file=$disk,if=ide,format=qcow2" \
    -boot c -vga cirrus -nic none -rtc base=1994-06-01 \
    -display "vnc=127.0.0.1:$((port - 5900))" \
    -monitor stdio
