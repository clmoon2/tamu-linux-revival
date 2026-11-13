#!/usr/bin/env python3
"""Build a UEFI USB image with TAMU 1.0D userland and an a.out-capable kernel."""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from build import copy_span, debugfs, partition_span


ROOT = Path(__file__).resolve().parents[1]
KERNEL_URL = "https://cdn.kernel.org/pub/linux/kernel/v4.x/linux-4.19.325.tar.xz"
KERNEL_SHA256 = "607bed7de5cda31a443df4c8a78dbe5e8a9ad31afde2a4d28fe99ab4730e8de1"
KERNEL_CONFIG = ROOT / "config/native-kernel-4.19.325.config"
KERNEL_PATCH = ROOT / "patches/0001-ia32-aout-exec-zmagic.patch"
IMAGE_BYTES = 384 * 1024 * 1024
ESP_BYTES = 64 * 1024 * 1024
ROOT_BYTES = 256 * 1024 * 1024


def run(*argv, cwd=None, log=None, timeout=1800):
    if log:
        with log.open("a", encoding="utf-8") as stream:
            stream.write("$ " + " ".join(map(str, argv)) + "\n")
            stream.flush()
            subprocess.run(argv, cwd=cwd, check=True, stdout=stream, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return ""
    return subprocess.run(argv, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout).stdout


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def announce(message):
    print(message, flush=True)


def kernel_image(scratch, tarball, jobs, log_path):
    source = scratch / "linux-4.19.325"
    output = scratch / "kernel-build"
    output.mkdir()
    announce("Building pinned Linux 4.19.325 compatibility kernel")
    run("tar", "-xf", str(tarball), "-C", str(scratch), log=log_path)
    run("patch", "-p1", "-i", str(KERNEL_PATCH), cwd=source, log=log_path)
    shutil.copy2(KERNEL_CONFIG, output / ".config")
    run("make", "-C", str(source), "O=" + str(output), "CC=gcc-12", "olddefconfig",
        log=log_path)
    config = (output / ".config").read_text()
    required = ("CONFIG_IA32_AOUT=y", "CONFIG_USELIB=y", "CONFIG_COMPAT_BRK=y",
                "CONFIG_EFI_STUB=y", "CONFIG_BLK_DEV_NVME=y", "CONFIG_EXT2_FS=y",
                "CONFIG_USB_XHCI_HCD=y", "CONFIG_USB_STORAGE=y", "CONFIG_USB_HID=y",
                "CONFIG_MAGIC_SYSRQ=y", "CONFIG_DEFAULT_MMAP_MIN_ADDR=0",
                "CONFIG_LSM_MMAP_MIN_ADDR=0")
    for setting in required:
        if setting not in config.splitlines():
            raise RuntimeError("Kernel configuration lost required setting: " + setting)
    run("make", "-C", str(source), "O=" + str(output), "CC=gcc-12", "-j" + str(jobs),
        "bzImage", log=log_path, timeout=3600)
    image = output / "arch/x86/boot/bzImage"
    if not image.is_file() or image.stat().st_size < 1024 * 1024:
        raise RuntimeError("Kernel build did not produce a bzImage")
    return image


def write_guest_file(rootfs, source, destination, executable=False):
    debugfs(rootfs, "rm " + destination, True)
    debugfs(rootfs, "write %s %s" % (source, destination), True)
    if executable:
        debugfs(rootfs, "sif %s mode 0100755" % destination, True)
    verified = rootfs.parent / (destination.strip("/").replace("/", "-") + ".verified")
    debugfs(rootfs, "dump %s %s" % (destination, verified))
    if verified.read_bytes() != source.read_bytes():
        raise RuntimeError("Guest file verification failed: " + destination)


def guest_root(scratch, base_image):
    raw = scratch / "original.raw"
    rootfs = scratch / "rootfs.img"
    run("qemu-img", "check", str(base_image))
    run("qemu-img", "convert", "-f", "qcow2", "-O", "raw", str(base_image), str(raw))
    offset, length = partition_span(raw)
    if length > ROOT_BYTES:
        raise RuntimeError("Original ext2 partition exceeds the 256 MiB native root slot")
    copy_span(raw, rootfs, offset, length)
    run("e2fsck", "-fn", str(rootfs))
    for name in ("init", "bash"):
        binary = scratch / ("original-" + name)
        guest_path = "/sbin/init" if name == "init" else "/bin/bash"
        debugfs(rootfs, "dump %s %s" % (guest_path, binary))
        if "Linux/i386" not in run("file", "-b", str(binary)) or \
                "ZMAGIC" not in run("file", "-b", str(binary)):
            raise RuntimeError("Expected original TAMU a.out binary: " + guest_path)
    for name in ("fstab", "system_config"):
        original = scratch / (name + ".original")
        modified = scratch / (name + ".native")
        debugfs(rootfs, "dump /etc/%s %s" % (name, original))
        content = original.read_text()
        if "/dev/hda" not in content:
            raise RuntimeError("Unexpected original /etc/%s; refusing blind rewrite" % name)
        modified.write_text(content.replace("/dev/hda1", "/dev/tamu-root")
                            .replace("/dev/hda", "/dev/tamu-root"))
        write_guest_file(rootfs, modified, "/etc/" + name)
    fetch_source = scratch / "tamu-fetch"
    shutil.copy2(ROOT / "tamu-fetch", fetch_source)
    write_guest_file(rootfs, fetch_source, "/usr/bin/tamu-fetch", True)
    if "directory" not in debugfs(rootfs, "stat /sys").lower():
        debugfs(rootfs, "mkdir /sys", True)
    write_guest_file(rootfs, Path("/usr/bin/busybox"), "/sbin/busybox", True)
    poweroff = scratch / "tamu-poweroff"
    poweroff.write_text("""#!/bin/sh
/sbin/busybox sync || exit 1
echo u > /proc/sysrq-trigger || exit 1
/sbin/busybox sleep 2
/sbin/busybox grep -q '^/dev/tamu-root / ext2 ro,' /proc/mounts || exit 1
/sbin/busybox poweroff -f
""")
    write_guest_file(rootfs, poweroff, "/sbin/tamu-poweroff", True)
    run("e2fsck", "-fn", str(rootfs))
    return rootfs


def initramfs(scratch, partname):
    stage = scratch / "initramfs"
    (stage / "bin").mkdir(parents=True)
    for directory in ("dev", "proc", "sys", "newroot"):
        (stage / directory).mkdir()
    shutil.copy2("/usr/bin/busybox", stage / "bin/busybox")
    (stage / "bin/busybox").chmod(0o755)
    init = """#!/bin/busybox sh
PATH=/bin
BB=/bin/busybox
$BB mount -t devtmpfs devtmpfs /dev || exec $BB sh
$BB mount -t proc proc /proc || exec $BB sh
$BB mount -t sysfs sysfs /sys || exec $BB sh
wanted="%s"
found=""
i=0
while [ "$i" -lt 30 ] && [ -z "$found" ]; do
    for event in /sys/class/block/*/uevent; do
        devname=""
        partname=""
        while IFS='=' read -r key value; do
            case "$key" in
                DEVNAME) devname="$value" ;;
                PARTNAME) partname="$value" ;;
            esac
        done < "$event"
        if [ "$partname" = "$wanted" ]; then
            found="$devname"
            break
        fi
    done
    [ -n "$found" ] && break
    i=$((i + 1))
    $BB sleep 1
done
if [ -z "$found" ]; then
    echo "TAMU root partition $wanted was not found" >/dev/console
    exec $BB sh </dev/console >/dev/console 2>&1
fi
$BB ln -s "/dev/$found" /dev/tamu-root
$BB mount -t ext2 -o ro /dev/tamu-root /newroot || exec $BB sh </dev/console >/dev/console 2>&1
$BB mount --move /dev /newroot/dev || exec $BB sh
$BB mount --move /proc /newroot/proc || exec $BB sh
$BB mount --move /sys /newroot/sys || exec $BB sh
exec $BB switch_root /newroot /sbin/init
""" % partname
    (stage / "init").write_text(init)
    (stage / "init").chmod(0o755)
    archive = scratch / "initrd.cpio"
    with archive.open("wb") as stream:
        find = subprocess.Popen(["find", ".", "-print0"], cwd=stage, stdout=subprocess.PIPE)
        try:
            subprocess.run(["cpio", "--null", "-o", "--format=newc"], cwd=stage,
                           stdin=find.stdout, stdout=stream, stderr=subprocess.PIPE, check=True)
        finally:
            find.stdout.close()
            if find.wait() != 0:
                raise RuntimeError("find failed while packaging initramfs")
    result = scratch / "initrd.gz"
    with archive.open("rb") as src, gzip.open(result, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    return result


def make_usb(scratch, output, kernel, rootfs):
    with output.open("xb") as stream:
        stream.truncate(IMAGE_BYTES)
    run("sgdisk", "--clear", "--new=1:2048:+64M", "--typecode=1:EF00",
        "--change-name=1:TAMU_EFI", "--new=2:0:+256M", "--typecode=2:8300",
        "--change-name=2:TAMU_ROOT", str(output))
    info = run("sgdisk", "-i", "2", str(output))
    guid = re.search(r"Partition unique GUID: ([0-9A-Fa-f-]+)", info)
    start = re.search(r"First sector: (\d+)", info)
    end = re.search(r"Last sector: (\d+)", info)
    if not (guid and start and end):
        raise RuntimeError("Could not parse native root GPT entry")
    partname = "TAMU_ROOT_" + guid.group(1)[:8].upper()
    run("sgdisk", "--change-name=2:" + partname, str(output))
    if (int(end.group(1)) - int(start.group(1)) + 1) * 512 < rootfs.stat().st_size:
        raise RuntimeError("Root partition is smaller than source ext2 image")
    with rootfs.open("rb") as src, output.open("r+b") as dst:
        dst.seek(int(start.group(1)) * 512)
        shutil.copyfileobj(src, dst)
    initrd = initramfs(scratch, partname)
    cfg = scratch / "grub.cfg"
    cfg.write_text("""insmod part_gpt
insmod fat
insmod search_label
insmod efi_gop
insmod gfxterm
set gfxmode=auto
set gfxpayload=keep
serial --speed=115200
terminal_input console serial
terminal_output gfxterm serial
search --no-floppy --label TAMUEFI --set=root
set timeout=3
menuentry 'TAMU Linux 1.0D native userland' {
  linux /boot/bzImage console=ttyS0,115200 console=tty0 norandmaps
  initrd /boot/initrd.gz
}
""")
    efi = scratch / "BOOTX64.EFI"
    run("grub-mkstandalone", "-O", "x86_64-efi", "-o", str(efi),
        "--modules=part_gpt fat search search_label linux normal gfxterm efi_gop serial",
        "boot/grub/grub.cfg=" + str(cfg))
    esp = scratch / "esp.img"
    with esp.open("xb") as stream:
        stream.truncate(ESP_BYTES)
    run("mkfs.vfat", "-F", "32", "-n", "TAMUEFI", str(esp))
    run("mmd", "-i", str(esp), "::/EFI", "::/EFI/BOOT", "::/boot")
    for source, dest in ((efi, "::/EFI/BOOT/BOOTX64.EFI"), (kernel, "::/boot/bzImage"),
                         (initrd, "::/boot/initrd.gz"), (cfg, "::/boot/grub.cfg")):
        run("mcopy", "-i", str(esp), str(source), dest)
    run("fsck.vfat", "-n", str(esp))
    with esp.open("rb") as src, output.open("r+b") as dst:
        dst.seek(2048 * 512)
        shutil.copyfileobj(src, dst)
    run("sgdisk", "-v", str(output))
    return {"root_partuuid": guid.group(1).lower(), "root_partname": partname,
            "kernel_sha256": sha256(kernel), "initrd_sha256": sha256(initrd)}


def publish_image(pending, output, manifest):
    """Publish both artifacts without replacing user files on normal failure."""
    manifest_path = output.with_name(output.name + ".manifest.json")
    image_digest = sha256(pending)
    manifest["image_sha256"] = image_digest
    image_temp = None
    manifest_temp = None
    try:
        with pending.open("rb") as src, tempfile.NamedTemporaryFile(
                prefix=output.name + ".", suffix=".partial", dir=output.parent,
                delete=False) as dst:
            image_temp = Path(dst.name)
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if sha256(image_temp) != image_digest:
            raise RuntimeError("Output copy failed SHA-256 verification")
        with tempfile.NamedTemporaryFile(prefix=manifest_path.name + ".", suffix=".partial",
                                         dir=output.parent, mode="w", encoding="utf-8",
                                         delete=False) as stream:
            manifest_temp = Path(stream.name)
            stream.write(json.dumps(manifest, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(manifest_temp, manifest_path)
        try:
            os.link(image_temp, output)
        except BaseException:
            manifest_path.unlink()
            raise
    finally:
        if manifest_temp:
            manifest_temp.unlink(missing_ok=True)
        if image_temp:
            image_temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", type=Path, default=ROOT / "build/tamu-1.0d.qcow2")
    parser.add_argument("--output", type=Path, default=ROOT / "build/tamu-native-usb.img")
    parser.add_argument("--kernel-tarball", type=Path, help="Use a local tarball with the pinned SHA-256")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 12))
    args = parser.parse_args()
    if args.jobs < 1 or args.jobs > 32:
        parser.error("--jobs must be between 1 and 32")
    base_image = args.base_image.resolve()
    output = args.output.resolve()
    if not base_image.is_file():
        parser.error("Build the original VM first, or pass --base-image")
    if output.exists():
        parser.error("Output already exists: " + str(output))
    manifest_path = output.with_name(output.name + ".manifest.json")
    if manifest_path.exists():
        parser.error("Manifest already exists: " + str(manifest_path))
    for executable in ("qemu-img", "debugfs", "e2fsck", "file", "gcc-12", "make",
                       "patch", "tar", "sgdisk", "grub-mkstandalone", "mkfs.vfat",
                       "fsck.vfat", "mmd", "mcopy", "cpio", "find"):
        if not shutil.which(executable):
            parser.error("Missing required executable: " + executable)
    if not Path("/usr/bin/busybox").is_file():
        parser.error("Install the static busybox binary at /usr/bin/busybox")
    if "statically linked" not in run("file", "-b", "/usr/bin/busybox"):
        parser.error("/usr/bin/busybox must be statically linked (busybox-static)")
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path = output.parent / ("native-build-%s-%d.log" % (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), os.getpid()))
    log_path.touch(exist_ok=False)
    announce("Build log: " + str(log_path))
    with tempfile.TemporaryDirectory(prefix="tamu-native-") as scratch_name:
        scratch = Path(scratch_name)
        tarball = args.kernel_tarball.resolve() if args.kernel_tarball else scratch / "linux-4.19.325.tar.xz"
        if not args.kernel_tarball:
            announce("Downloading pinned Linux 4.19.325 source")
            with urllib.request.urlopen(KERNEL_URL, timeout=120) as response, tarball.open("xb") as stream:
                shutil.copyfileobj(response, stream)
        if sha256(tarball) != KERNEL_SHA256:
            raise RuntimeError("Kernel source SHA-256 mismatch: " + str(tarball))
        kernel = kernel_image(scratch, tarball, args.jobs, log_path)
        announce("Cloning and adapting original TAMU Linux 1.0D root")
        rootfs = guest_root(scratch, base_image)
        pending = scratch / "tamu-native-usb.img"
        announce("Assembling UEFI GPT USB image")
        details = make_usb(scratch, pending, kernel, rootfs)
        manifest = {"release": "TAMU Linux 1.0D original userland",
                    "compatibility_kernel": "Linux 4.19.325 (patched, end of life)",
                    "kernel_source_url": KERNEL_URL, "kernel_source_sha256": KERNEL_SHA256,
                    "busybox_sha256": sha256(Path("/usr/bin/busybox")),
                    "original_image_sha256": sha256(base_image),
                    "built_utc": datetime.now(timezone.utc).isoformat(), **details}
        publish_image(pending, output, manifest)
    announce("Native USB image ready: " + str(output))
    announce("SHA-256: " + manifest["image_sha256"])


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("Native build failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
