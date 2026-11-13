#!/usr/bin/env python3
"""Smoke-test a native TAMU image with OVMF on USB and cloned NVMe storage."""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from build import Monitor, copy_span, stop_qemu, wait_for


ROOT = Path(__file__).resolve().parents[1]
OVMF_CODE = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
OVMF_VARS = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")


def run(*argv):
    return subprocess.run(argv, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout


def test_bus(image, bus, session, scratch, port):
    disk = image
    if bus == "nvme":
        disk = scratch / "installed.raw"
        with disk.open("xb") as stream:
            stream.truncate(2 * 1024 * 1024 * 1024)
        with image.open("rb") as src, disk.open("r+b") as dst:
            shutil.copyfileobj(src, dst)
        run("sgdisk", "-e", str(disk))
        run("sgdisk", "-v", str(disk))
    overlay = scratch / (bus + ".qcow2")
    run("qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b", str(disk), str(overlay))
    vars_file = scratch / (bus + "-vars.fd")
    shutil.copy2(OVMF_VARS, vars_file)
    monitor_path = scratch / (bus + ".sock")
    args = ["qemu-system-x86_64", "-machine", "q35,accel=kvm:tcg", "-cpu", "max",
            "-m", "1024", "-smp", "2", "-no-reboot", "-nic", "none", "-display", "none",
            "-vga", "std", "-vnc", "127.0.0.1:%d" % (port - 5900),
            "-monitor", "unix:%s,server=on,wait=off" % monitor_path,
            "-serial", "file:%s" % (session / (bus + "-serial.log")),
            "-drive", "if=pflash,format=raw,unit=0,readonly=on,file=%s" % OVMF_CODE,
            "-drive", "if=pflash,format=raw,unit=1,file=%s" % vars_file,
            "-drive", "if=none,id=disk,file=%s,format=qcow2" % overlay]
    if bus == "usb":
        args += ["-device", "qemu-xhci,id=xhci", "-device", "usb-storage,drive=disk,bootindex=1"]
    else:
        args += ["-device", "nvme,drive=disk,serial=tamu,bootindex=1"]
    with (session / (bus + "-qemu.log")).open("wb") as stream:
        process = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT)
    monitor = None
    try:
        monitor = Monitor(monitor_path, process, scratch)
        time.sleep(12)
        monitor.key("ctrl-alt-f1")
        monitor.key("ret")
        wait_for(monitor, r"tamu login", bus + " login", session, seconds=150)
        shutil.copy2(monitor.capture(), session / "evidence" / (bus + "-login.png"))
        monitor.type("root")
        wait_for(monitor, r"bash", bus + " original shell", session, seconds=60)
        monitor.type("uname -a")
        wait_for(monitor, r"4\.19\.325", bus + " compatibility kernel", session,
                 seconds=30)
        monitor.type("tamu-fetch")
        wait_for(monitor, r"OS:\s*TAMU Linux 1\.0", bus + " fetch", session, seconds=30)
        shutil.copy2(monitor.capture(), session / "evidence" / (bus + "-fetch.png"))
        monitor.type("/sbin/tamu-poweroff")
        if process.wait(timeout=30) != 0:
            raise RuntimeError(bus + " guest did not power off successfully")
        monitor.close()
        monitor = None
    finally:
        if monitor:
            stop_qemu(process, monitor)
        elif process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
    run("qemu-img", "check", str(overlay))
    postboot = scratch / (bus + "-postboot.raw")
    run("qemu-img", "convert", "-f", "qcow2", "-O", "raw", str(overlay), str(postboot))
    info = run("sgdisk", "-i", "2", str(postboot))
    start = re.search(r"First sector: (\d+)", info)
    end = re.search(r"Last sector: (\d+)", info)
    if not (start and end):
        raise RuntimeError("Could not find " + bus + " root partition")
    rootfs = scratch / (bus + "-rootfs.img")
    copy_span(postboot, rootfs, int(start.group(1)) * 512,
              (int(end.group(1)) - int(start.group(1)) + 1) * 512)
    superblock = run("dumpe2fs", "-h", str(rootfs))
    if not re.search(r"Filesystem state:\s+clean\b", superblock):
        raise RuntimeError(bus + " ext2 filesystem was not clean after poweroff")
    run("e2fsck", "-fn", str(rootfs))
    print(bus.upper() + "_UEFI_BOOT_FETCH_CLEAN_POWEROFF_PASS", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, default=ROOT / "build/tamu-native-usb.img")
    parser.add_argument("--bus", choices=("usb", "nvme", "both"), default="both")
    parser.add_argument("--vnc-port", type=int, default=5928)
    args = parser.parse_args()
    if not 5900 <= args.vnc_port <= 5999:
        parser.error("VNC port must be between 5900 and 5999")
    image = args.image.resolve()
    if not image.is_file():
        parser.error("Native image does not exist: " + str(image))
    for path in (OVMF_CODE, OVMF_VARS):
        if not path.is_file():
            parser.error("Install OVMF; missing " + str(path))
    for executable in ("qemu-system-x86_64", "qemu-img", "sgdisk", "dumpe2fs",
                       "e2fsck", "tesseract"):
        if not shutil.which(executable):
            parser.error("Missing required executable: " + executable)
    session = ROOT / "build" / ("native-test-%s-%d" % (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), os.getpid()))
    session.mkdir(parents=True)
    (session / "evidence").mkdir()
    print("Evidence: " + str(session), flush=True)
    with tempfile.TemporaryDirectory(prefix="tamu-native-test-") as scratch_name:
        scratch = Path(scratch_name)
        for bus in (("usb", "nvme") if args.bus == "both" else (args.bus,)):
            test_bus(image, bus, session, scratch, args.vnc_port)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("Native smoke test failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
