#!/usr/bin/env python3
"""Build and smoke-test TAMU Linux 1.0D from its archived floppy images.

The guest has no serial installer. QEMU's monitor supplies screenshots and
keystrokes; Tesseract lets us wait for each actual installer prompt instead of
guessing how long 1994 software will take to boot or copy a floppy.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://mirror.math.princeton.edu/pub/oldlinux/Linux.old/distributions/TAMU/TAMU-1.0D/bin/3inch"
NAMES = ["boot"] + ["bin.%02d" % n for n in range(1, 22)]
CHECKSUMS = ROOT / "checksums" / "sha256.txt"
XCONFIG = ROOT / "config" / "Xconfig.tamu"
FETCH = ROOT / "tamu-fetch"


def log(session, message):
    line = "[%s] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S UTC"), message)
    print(line, flush=True)
    with (session / "build.log").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def run(*args, timeout=300):
    return subprocess.run(args, check=True, text=True, timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def expected_hashes():
    entries = {}
    for line in CHECKSUMS.read_text(encoding="ascii").splitlines():
        digest, name = line.split(maxsplit=1)
        entries[name.strip()] = digest
    if set(entries) != set(NAMES):
        raise RuntimeError("Checksum list must contain boot and bin.01 through bin.21")
    return entries


def get_media(media_dir, session):
    media_dir.mkdir(parents=True, exist_ok=True)
    hashes = expected_hashes()
    for name in NAMES:
        target = media_dir / name
        if target.exists():
            if sha256(target) != hashes[name]:
                raise RuntimeError("Existing media has the wrong SHA-256: %s" % target)
            continue
        url = SOURCE + "/" + name
        part = media_dir / (name + ".download-%d" % os.getpid())
        log(session, "Downloading %s" % url)
        try:
            with urllib.request.urlopen(url, timeout=60) as response, part.open("xb") as stream:
                shutil.copyfileobj(response, stream)
            if sha256(part) != hashes[name]:
                raise RuntimeError("Downloaded media failed SHA-256: %s" % name)
            part.rename(target)
        finally:
            if part.exists():
                part.unlink()
    log(session, "Verified boot and all 21 binary disks against pinned SHA-256 values")
    return hashes


class Monitor:
    """A small synchronous client for QEMU's human monitor socket."""

    def __init__(self, path, process, scratch):
        deadline = time.monotonic() + 15
        while not path.exists():
            if process.poll() is not None:
                raise RuntimeError("QEMU exited before creating its monitor socket")
            if time.monotonic() > deadline:
                raise TimeoutError("QEMU monitor did not appear: %s" % path)
            time.sleep(0.1)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(str(path))
        self.scratch = scratch
        self._read_prompt()

    def _read_prompt(self):
        data = bytearray()
        while not data.endswith(b"(qemu) "):
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("QEMU monitor closed unexpectedly")
            data.extend(chunk)
            if len(data) > 2 * 1024 * 1024:
                raise RuntimeError("QEMU monitor response was too large")
        return bytes(data)

    def command(self, value):
        if "\n" in value or "\r" in value:
            raise ValueError("Monitor commands must fit on one line")
        self.sock.sendall(value.encode("utf-8") + b"\n")
        return self._read_prompt()

    def key(self, name):
        self.command("sendkey " + name)

    def type(self, value):
        special = {"-": "minus", "/": "slash", " ": "spc", ".": "dot"}
        for char in value:
            if not (char.isascii() and (char.isalnum() or char in special)):
                raise ValueError("Unsupported guest keystroke: %r" % char)
            self.key(special.get(char, char))
        self.key("ret")

    def capture(self, fmt="png"):
        path = self.scratch / ("screen." + fmt)
        self.command("screendump %s -f %s" % (path, fmt))
        if not path.is_file():
            raise RuntimeError("QEMU did not create a screenshot: %s" % path)
        return path

    def close(self):
        self.sock.close()


def text_on_screen(monitor):
    image = monitor.capture()
    result = run("tesseract", str(image), "stdout", "--psm", "6", timeout=20)
    return result.stdout


def right_pane_text(monitor):
    """Read only fetch's information pane so the colored logo cannot pollute OCR."""
    image = monitor.capture("ppm")
    with image.open("rb") as stream:
        if stream.readline() != b"P6\n":
            raise RuntimeError("QEMU did not produce a P6 screenshot")
        width, height = map(int, stream.readline().split())
        if stream.readline() != b"255\n":
            raise RuntimeError("Unexpected PPM color depth")
        pixels = stream.read()
    if len(pixels) != width * height * 3:
        raise RuntimeError("Incomplete QEMU screenshot")
    left = int(width * 0.45)
    cropped = monitor.scratch / "right-pane.ppm"
    with cropped.open("wb") as stream:
        stream.write(("P6\n%d %d\n255\n" % (width - left, height)).encode("ascii"))
        for row in range(height):
            start = (row * width + left) * 3
            stream.write(pixels[start:start + (width - left) * 3])
    return run("tesseract", str(cropped), "stdout", "--psm", "6", timeout=20).stdout


def wait_for(monitor, pattern, stage, session, seconds=120, reader=text_on_screen):
    deadline = time.monotonic() + seconds
    last = ""
    while time.monotonic() < deadline:
        last = reader(monitor)
        if re.search(pattern, last, flags=re.IGNORECASE):
            log(session, "Saw %s" % stage)
            return last
        time.sleep(0.5)
    evidence = session / "evidence"
    evidence.mkdir(exist_ok=True)
    shutil.copy2(monitor.capture(), evidence / ("timeout-" + stage + ".png"))
    (evidence / ("timeout-" + stage + ".txt")).write_text(last, encoding="utf-8")
    raise TimeoutError("Timed out waiting for %s; see %s" % (stage, evidence))


def answer(monitor, pattern, response, stage, session, seconds=120):
    wait_for(monitor, pattern, stage, session, seconds)
    if response:
        monitor.type(response)
    else:
        monitor.key("ret")


def wait_for_menu(monitor, session):
    deadline = time.monotonic() + 150
    video_accepted = False
    shown = ""
    while time.monotonic() < deadline:
        shown = text_on_screen(monitor)
        if "Linux Maintenance Diskette" in shown and re.search(r"enter choice", shown, re.I):
            log(session, "Booted the original maintenance floppy")
            return
        if not video_accepted and re.search(r"modes available|space.*continue", shown, re.I):
            monitor.key("spc")
            video_accepted = True
        time.sleep(0.5)
    shutil.copy2(monitor.capture(), session / "evidence" / "timeout-installer-menu.png")
    (session / "evidence" / "timeout-installer-menu.txt").write_text(shown, encoding="utf-8")
    raise TimeoutError("Original boot floppy did not reach the installer menu")


def requested_disk(text):
    matches = re.findall(r"Insert disk\s+([0-9@OolI]+)\s+and press enter", text, re.I)
    if not matches:
        return None
    value = matches[-1].translate(str.maketrans({"@": "0", "O": "0", "o": "0", "l": "1", "I": "1"}))
    return int(value)


def wait_disk(monitor, expected, session):
    deadline = time.monotonic() + 150
    shown = ""
    while time.monotonic() < deadline:
        shown = text_on_screen(monitor)
        found = requested_disk(shown)
        # Tesseract can read the old console's "10" as "18".
        if found == expected or (expected == 10 and found == 18):
            return
        time.sleep(0.5)
    shutil.copy2(monitor.capture(), session / "evidence" / ("timeout-disk-%02d.png" % expected))
    (session / "evidence" / ("timeout-disk-%02d.txt" % expected)).write_text(shown, encoding="utf-8")
    raise TimeoutError("Installer did not request disk %02d" % expected)


def start_qemu(disk, floppy, port, monitor_path, qemu_log):
    args = [
        "qemu-system-i386", "-machine", "pc", "-cpu", "486", "-m", "32",
        "-drive", "file=%s,if=ide,format=qcow2" % disk,
        "-boot", "a" if floppy else "c", "-vga", "cirrus", "-nic", "none",
        "-rtc", "base=1994-06-01", "-display", "vnc=127.0.0.1:%d" % (port - 5900),
        "-monitor", "unix:%s,server,nowait" % monitor_path,
    ]
    if floppy:
        args.extend(["-drive", "file=%s,if=floppy,format=raw,readonly=on" % floppy])
    stream = qemu_log.open("wb")
    try:
        process = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT)
    finally:
        stream.close()
    return process


def terminate_owned_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def stop_qemu(process, monitor):
    if process.poll() is None:
        try:
            monitor.command("quit")
        except (OSError, RuntimeError):
            pass  # A successful quit closes the monitor before it can reply.
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            terminate_owned_process(process)
    monitor.close()


def install(media, disk, port, session, scratch):
    run("qemu-img", "create", "-f", "qcow2", str(disk), "256M")
    monitor_path = session / "install-monitor.sock"
    process = start_qemu(disk, media / "boot", port, monitor_path, session / "qemu-install.log")
    monitor = None
    try:
        monitor = Monitor(monitor_path, process, scratch)
        wait_for_menu(monitor, session)
        monitor.key("ret")
        prompts = [
            (r"floppy drive will be used", "", "floppy-choice"),
            (r"run fdisk now", "", "partition-choice"),
            (r"Which partition will be used", "", "root-partition"),
            (r"Setup swapping", "n", "swap-choice"),
            (r"Install all packages", "y", "package-choice"),
            (r"BOOTACT", "n", "boot-selector-choice"),
            (r"lilo installed", "", "lilo-choice"),
            (r"hostname", "", "hostname-choice"),
            (r"networking configured", "n", "network-choice"),
            (r"Continue\s*\(n\)", "y", "install-confirmation"),
        ]
        for pattern, response, stage in prompts:
            answer(monitor, pattern, response, stage, session)
        for number in range(1, 22):
            wait_disk(monitor, number, session)
            monitor.command("change floppy0 %s" % (media / ("bin.%02d" % number)))
            monitor.key("ret")
            log(session, "Installed disk %02d/21" % number)
        wait_for(monitor, r"restore complete", "installer-complete", session, seconds=180)
        shutil.copy2(monitor.capture(), session / "evidence" / "installer-complete.png")
        monitor.command("eject floppy0")
        monitor.command("system_reset")
        wait_for(monitor, r"tamu login", "first-hard-disk-boot", session, seconds=150)
        shutil.copy2(monitor.capture(), session / "evidence" / "first-boot.png")
        monitor.type("root")
        wait_for(monitor, r"bash", "first-root-shell", session)
        monitor.type("halt")
        wait_for(monitor, r"The system is halted", "clean-install-halt", session)
    finally:
        if monitor:
            stop_qemu(process, monitor)
        elif process.poll() is None:
            terminate_owned_process(process)


def partition_span(raw):
    with raw.open("rb") as stream:
        mbr = stream.read(512)
    if len(mbr) != 512 or mbr[510:512] != b"\x55\xaa":
        raise RuntimeError("Installed disk has no valid MBR signature")
    entry = mbr[446:462]
    kind = entry[4]
    start, sectors = struct.unpack_from("<II", entry, 8)
    if kind != 0x83 or start < 1 or sectors < 1:
        raise RuntimeError("Expected one Linux ext2 partition in MBR entry 1")
    if (start + sectors) * 512 > raw.stat().st_size:
        raise RuntimeError("MBR partition extends beyond the virtual disk")
    return start * 512, sectors * 512


def copy_span(source, target, offset, length, write=False):
    mode = "r+b" if write else "wb"
    with source.open("rb") as src, target.open(mode) as dst:
        if write:
            dst.seek(offset)
        else:
            src.seek(offset)
        remaining = length
        while remaining:
            block = src.read(min(1024 * 1024, remaining))
            if not block:
                raise RuntimeError("Unexpected end of disk image")
            dst.write(block)
            remaining -= len(block)


def debugfs(image, command, writable=False):
    args = ["debugfs"]
    if writable:
        args.append("-w")
    args.extend(["-R", command, str(image)])
    return run(*args).stdout or ""


def patch_guest(installed, output, session, scratch):
    raw = session / "installed.raw"
    rootfs = session / "rootfs.img"
    run("qemu-img", "convert", "-f", "qcow2", "-O", "raw", str(installed), str(raw))
    offset, length = partition_span(raw)
    copy_span(raw, rootfs, offset, length)

    # debugfs takes source paths as its own command text. Stage them under a
    # temporary path without spaces, even if the clone lives in a spaced path.
    config_source = scratch / "Xconfig.tamu"
    fetch_source = scratch / "tamu-fetch"
    shutil.copy2(XCONFIG, config_source)
    shutil.copy2(FETCH, fetch_source)
    debugfs(rootfs, "write %s /usr/X386/lib/X11/Xconfig.tamu" % config_source, True)
    debugfs(rootfs, "rm /usr/X386/bin/X", True)
    debugfs(rootfs, "symlink /usr/X386/bin/X XF86_VGA16", True)
    debugfs(rootfs, "write %s /usr/bin/tamu-fetch" % fetch_source, True)
    debugfs(rootfs, "sif /usr/bin/tamu-fetch mode 0100755", True)

    for guest_path, source in [
        ("/usr/X386/lib/X11/Xconfig.tamu", XCONFIG),
        ("/usr/bin/tamu-fetch", FETCH),
    ]:
        dumped = scratch / (source.name + ".verify")
        debugfs(rootfs, "dump %s %s" % (guest_path, dumped))
        if not dumped.is_file() or sha256(dumped) != sha256(source):
            raise RuntimeError("Guest file failed byte-for-byte verification: %s" % guest_path)
    if 'Fast link dest: "XF86_VGA16"' not in debugfs(rootfs, "stat /usr/X386/bin/X"):
        raise RuntimeError("The installed X server symlink did not select VGA16")
    run("e2fsck", "-fn", str(rootfs))
    copy_span(rootfs, raw, offset, length, write=True)
    run("qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(raw), str(output))
    log(session, "Installed minimal VGA16 configuration and tamu-fetch; ext2 check passed")


def png_dimensions(path):
    with path.open("rb") as stream:
        header = stream.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("QEMU did not produce a PNG screenshot")
    return struct.unpack(">II", header[16:24])


def smoke(disk, port, session, scratch):
    monitor_path = session / "smoke-monitor.sock"
    process = start_qemu(disk, None, port, monitor_path, session / "qemu-smoke.log")
    monitor = None
    try:
        monitor = Monitor(monitor_path, process, scratch)
        wait_for(monitor, r"tamu login", "patched-guest-boot", session, seconds=150)
        monitor.type("root")
        wait_for(monitor, r"bash", "patched-root-shell", session)
        monitor.type("clear")
        monitor.type("uname -a")
        wait_for(monitor, r"Linux tamu 1\.0", "Linux-1.0-kernel", session)
        monitor.type("bash -version")
        wait_for(monitor, r"GNU bash, version 1\.13\.1-CWRU", "original-Bash", session)
        shutil.copy2(monitor.capture(), session / "evidence" / "version.png")
        monitor.type("clear")
        monitor.type("tamu-fetch")
        # OCR can read the colored terminal's final "D" as "0" and colons
        # as question marks. Check the release stem and all field names.
        wait_for(monitor, r"TAMU Linux 1\.0", "fetch-display", session, reader=right_pane_text)
        wait_for(monitor, r"(?s)Kernel.*Memory.*Disk.*Uptime", "fetch-fields", session,
                 seconds=30, reader=right_pane_text)
        shutil.copy2(monitor.capture(), session / "evidence" / "fetch.png")
        monitor.type("startx")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            screenshot = monitor.capture()
            if png_dimensions(screenshot) == (640, 480):
                time.sleep(2)
                desktop = session / "evidence" / "desktop.png"
                shutil.copy2(monitor.capture(), desktop)
                desktop_text = run("tesseract", str(desktop), "stdout", "--psm", "11",
                                   timeout=20).stdout
                if not re.search(r"File\s+Folder\s+View", desktop_text, re.I):
                    raise RuntimeError("X entered 640x480, but the XFM menu was not visible in the desktop screenshot")
                log(session, "XFree86 VGA16 desktop reached 640x480")
                break
            time.sleep(0.5)
        else:
            failure = session / "evidence" / "desktop-failure.png"
            shutil.copy2(monitor.capture(), failure)
            (session / "evidence" / "desktop-failure.txt").write_text(
                run("tesseract", str(failure), "stdout", "--psm", "11", timeout=20).stdout,
                encoding="utf-8")
            raise RuntimeError("XFree86 did not enter its 640x480 desktop")
        monitor.key("ctrl-alt-backspace")
        wait_for(monitor, r"bash", "returned-from-X", session)
        monitor.type("halt")
        wait_for(monitor, r"The system is halted", "clean-final-halt", session)
    finally:
        if monitor:
            stop_qemu(process, monitor)
        elif process.poll() is None:
            terminate_owned_process(process)


def verify_final_image(disk, session, scratch):
    run("qemu-img", "check", str(disk))
    raw = scratch / "final-check.raw"
    rootfs = scratch / "final-check-rootfs.img"
    run("qemu-img", "convert", "-f", "qcow2", "-O", "raw", str(disk), str(raw))
    offset, length = partition_span(raw)
    copy_span(raw, rootfs, offset, length)
    run("e2fsck", "-fn", str(rootfs))
    log(session, "Final qcow2 structure and guest ext2 filesystem passed read-only checks")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-dir", type=Path, default=ROOT / "build" / "media",
                        help="Verified media cache; missing files are downloaded")
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "tamu-1.0d.qcow2",
                        help="Final tested VM image; existing files are never overwritten")
    parser.add_argument("--vnc-port", type=int, default=5922,
                        help="Loopback VNC port used only during the build (default: 5922)")
    args = parser.parse_args()
    if not 5900 <= args.vnc_port <= 5999:
        parser.error("--vnc-port must be between 5900 and 5999")
    output = args.output.resolve()
    if output.exists():
        parser.error("Output already exists: %s" % output)
    for executable in ("qemu-system-i386", "qemu-img", "tesseract", "debugfs", "e2fsck"):
        if not shutil.which(executable):
            parser.error("Missing required executable: %s" % executable)
    build_root = ROOT / "build"
    build_root.mkdir(exist_ok=True)
    session = build_root / ("session-%s-%d" % (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), os.getpid()))
    session.mkdir()
    (session / "evidence").mkdir()
    log(session, "Build session: %s" % session)
    log(session, "Original media is downloaded and verified; it is not redistributed here")
    hashes = get_media(args.media_dir.resolve(), session)
    with tempfile.TemporaryDirectory(prefix="tamu-revival-") as scratch_name:
        scratch = Path(scratch_name)
        media = scratch / "media"
        media.mkdir()
        for name in NAMES:
            (media / name).symlink_to((args.media_dir.resolve() / name))
        installed = session / "installed.qcow2"
        tested = session / "tested.qcow2"
        install(media, installed, args.vnc_port, session, scratch)
        for name in NAMES:
            if sha256(args.media_dir.resolve() / name) != hashes[name]:
                raise RuntimeError("Original media changed during installation: %s" % name)
        patch_guest(installed, tested, session, scratch)
        smoke(tested, args.vnc_port, session, scratch)
        verify_final_image(tested, session, scratch)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".partial",
                                            dir=output.parent, delete=False)
    temporary_path = Path(temporary.name)
    try:
        with tested.open("rb") as source, temporary as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        if sha256(temporary_path) != sha256(tested):
            raise RuntimeError("Final image copy failed byte-for-byte verification")
        # Hard-linking on the same filesystem publishes atomically and refuses
        # to replace a pre-existing output if another build raced this one.
        os.link(temporary_path, output)
    finally:
        temporary_path.unlink(missing_ok=True)
    manifest = {
        "release": "TAMU Linux 1.0D",
        "source": SOURCE,
        "source_hashes_are_upstream_authenticated": False,
        "media_sha256": hashes,
        "fetch_sha256": sha256(FETCH),
        "xconfig_sha256": sha256(XCONFIG),
        "qemu_version": run("qemu-system-i386", "--version").stdout.splitlines()[0],
        "verified": ["fresh-install", "Linux 1.0", "Bash 1.13.1-CWRU", "tamu-fetch", "XFree86 640x480", "clean-halt", "qcow2-check", "ext2-check"],
        "output_sha256": sha256(output),
    }
    (session / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log(session, "Verified image: %s" % output)
    log(session, "Evidence and manifest: %s" % session)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, TimeoutError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as error:
        print("Build failed: %s" % error, file=sys.stderr)
        raise SystemExit(1)
