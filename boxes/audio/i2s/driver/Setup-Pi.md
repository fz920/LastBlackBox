# NB3 audio on this Pi

These commands implement the [tutorial](README.md) for the **Revision 2 NB3
mouth**, using its `nb3-audio-module.c`, I²S interface, and GPIO16 amplifier
enable. They do not change microphone/speaker wiring.

The existing headers for kernel `6.18.50+rpt-rpi-v8` were sufficient to build
the unmodified source on Debian 13, so no header package installation was needed.

From `~/LastBlackBox`, build as your normal user:

```bash
mkdir -p _tmp/nb3-audio-driver
cp boxes/audio/i2s/driver/{Makefile,nb3-audio-module.c} _tmp/nb3-audio-driver/
make -C _tmp/nb3-audio-driver all
```

Review and install with [setup-pi.sh](setup-pi.sh):

```bash
sudo bash boxes/audio/i2s/driver/setup-pi.sh
```

The script checks the module's kernel version, backs up the boot configuration
under `/var/backups/nb3-audio/`, installs the module under the current kernel's
`extra/` directory, and enables loading at boot via
`/etc/modules-load.d/nb3-audio.conf`. It adds the tutorial's two configuration
lines to an `[all]` section of `/boot/firmware/config.txt`.

The stock `snd_soc_max98357a` driver and the repo's driver claim the same codec.
`/etc/modprobe.d/nb3-audio.conf` prevents the playback-only stock codec from
claiming it first, so the repo's capture/playback driver can serve ears and mouth.
This does not disable HDMI or headphone output.

The script also attempts to activate the same overlay in the current session.
If it reports that a reboot is needed, reboot when ready, reconnect to the Pi,
and start the robot website again. Check both directions with:

```bash
aplay -l
arecord -l
```

Look for `MAX98357A`. Card numbers can change; use the card name in applications.
Enumerating a device confirms driver registration, not physical wiring or sound.
After a kernel upgrade, rebuild and reinstall for the new running kernel.

On 2026-09-23, installation activated the overlay without rebooting. Both
`aplay -l` and `arecord -l` listed `MAX98357A`, and
`/sys/bus/platform/devices/max98357a/driver` pointed to `nb3_audio`.
The [robot website](../../../servers/python/robot_control/README.md#spoken-descriptions-through-the-robots-mouth)
can generate and play spoken descriptions for a listening check.

To undo this setup, restore the saved `config.txt`, remove the two added
`nb3-audio.conf` files (or restore their backups if they previously existed),
and reboot. The unreferenced kernel module can remain installed.
