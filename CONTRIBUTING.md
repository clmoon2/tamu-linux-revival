# Contributing

The useful test for this project is a fresh build from the pinned original media. Before opening a pull request that changes the installer automation, X config, or guest script, run:

```sh
python3 -m py_compile tools/build.py
bash -n run.sh
shellcheck run.sh
shellcheck -s sh -e SC2002,SC2006 tamu-fetch
python3 tools/build.py --output build/your-test-image.qcow2 --vnc-port 5924
```

The full build checks all installer disks, boots the installed disk, runs the guest commands, opens X, and checks the final filesystem. Media cached under `build/media` is rehashed on every run. Use a new `--output` path; the builder refuses to replace an existing image.

When reporting a failure, include the failing stage, host QEMU version, relevant `build/session-*/build.log` lines, and the saved screenshot or OCR text from that session. Please do not upload the original floppy images or a built guest disk to an issue. Those files contain third-party software with separate licenses.

Changes to compatibility behavior should explain which original guest behavior was observed, which QEMU setup was tested, and why the patch is needed. Keep network access disabled in this historical guest.
