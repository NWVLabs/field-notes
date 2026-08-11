# Build a Dedicated Raspberry Pi Touchscreen Web Appliance

This tutorial turns a local web application into a Raspberry Pi appliance that
boots directly into a touch-first interface, hides the normal desktop, waits for
the application to become healthy, and recovers automatically when the browser
or compositor fails.

The finished system is:

```text
systemd
├── application service on localhost
├── reverse proxy serving the frontend
└── kiosk service on tty1
    └── Cage (single-application Wayland compositor)
        └── readiness launcher
            └── Chromium kiosk
```

The order matters. Keep the desktop and SSH recovery available while proving
the kiosk manually. Rotate pixels before calibrating touch. Fix visual boot
handoffs only after the functional kiosk is stable. Automate deployment last.

## Before you begin

Use a supported 64-bit Raspberry Pi OS or Debian release, a Raspberry Pi with a
touch display, known-good power and storage, a second computer with working SSH,
and a restorable storage image. Replace every angle-bracket placeholder before
running a command.

Record this baseline and save it off the Pi:

```bash
cat /etc/os-release
uname -a
systemctl get-default
systemctl status display-manager.service --no-pager
loginctl list-sessions
ip address
```

Make a storage backup. Confirm you can reconnect over SSH after reboot. Do not
continue without both recovery paths.

## Phase 0: run the application without kiosk mode

Install the application and reverse proxy first. Bind the backend to localhost,
serve the production frontend through the proxy, and prove these checks:

```bash
systemctl is-active app.service nginx.service
curl --fail http://127.0.0.1:<backend-port>/health
curl --fail http://127.0.0.1/
systemctl --failed --no-pager
```

Open the application in an ordinary browser and finish functional testing.
Kiosk work cannot repair an unhealthy application.

## Phase 1: Create the supervised Cage and Chromium kiosk

# Cage and Chromium Appliance Architecture

Hiding panels and wallpaper does not make a desktop an appliance. A smaller,
more recoverable path is systemd -> Cage -> Chromium -> local application.

Run the backend and reverse proxy independently. Give the kiosk service a real
VT, a PAM session, and logind seat ownership:

```ini
[Unit]
After=systemd-user-sessions.service app.service nginx.service
Wants=app.service nginx.service
Conflicts=display-manager.service getty@tty1.service

[Service]
User=<kiosk-user>
PAMName=login
TTYPath=/dev/tty1
StandardInput=tty
TTYReset=yes
TTYVTDisallocate=yes
ExecStartPre=+/usr/bin/chvt 1
ExecStart=/usr/bin/cage -s -- /usr/local/bin/app-kiosk-launcher
Restart=always
RestartSec=3
```

The launcher should start inside Cage, wait for a local health endpoint, then
`exec` Chromium with a dedicated profile, Wayland/Ozone, kiosk mode, and
first-run/crash-dialog suppression. Waiting inside the launcher keeps Cage in
control of a black surface while the application starts; an `ExecStartPre`
readiness loop can interfere with VT/PAM session establishment.

Test manually before changing the default boot target: backend unavailable,
Chromium exit, Cage exit, offline boot, touch input, and repeated reboot. Keep
SSH and a desktop rollback command available. Only then disable the display
manager and enable the kiosk service.

### Frustration-proof rollout

Do not begin by disabling the desktop. Confirm SSH from a second computer and
keep that session open. Record the current default target and display manager:

```bash
systemctl get-default
systemctl status display-manager.service --no-pager
loginctl list-sessions
```

Install Cage, Chromium, `wlr-randr`, curl, and the PAM components required by
your distribution. Start the unit manually while the desktop remains your
rollback. A healthy session should show a real `tty1`, an active local seat,
and Cage owning the physical DRM output—not a nested Wayland window.

Use a launcher shaped like this:

```bash
#!/usr/bin/env bash
set -u
readonly HEALTH_URL="http://127.0.0.1:<port>/health"
readonly APP_URL="http://127.0.0.1/?kiosk=1"

until /usr/bin/curl --fail --silent "$HEALTH_URL" >/dev/null; do
  sleep 1
done

exec /usr/bin/chromium \
  --ozone-platform=wayland \
  --enable-features=UseOzonePlatform \
  --kiosk --no-first-run --noerrdialogs \
  --disable-session-crashed-bubble \
  --user-data-dir="<dedicated-profile>" \
  "$APP_URL"
```

If an `ExecStartPre` readiness loop dies with `SIGHUP`, move readiness inside
the Cage-launched script. If Cage reports permission denied for DRM/input,
inspect the logind session, PAM stack, active VT, and competing display manager;
do not “fix” it by running the compositor as root.

Only after manual tests pass:

```bash
sudo systemctl enable app.service nginx.service app-kiosk.service
sudo systemctl set-default multi-user.target
sudo systemctl disable display-manager.service
sudo reboot
```

Rollback over SSH:

```bash
sudo systemctl disable --now app-kiosk.service
sudo systemctl enable display-manager.service
sudo systemctl set-default graphical.target
sudo reboot
```

Acceptance requires three cold boots plus forced Chromium and Cage termination;
each process must return automatically without exposing a desktop or login
prompt.

### Stop gate

Do not disable the desktop yet. Continue only when Cage owns the physical VT, the local application appears without browser chrome, SSH remains available, and killing Chromium or Cage causes an automatic restart.

## Phase 2: Rotate the display and calibrate touch

# Touch Display 2 Rotation and Calibration

Rotating pixels and rotating touch coordinates are separate operations. First
inspect outputs inside the active Wayland session:

```bash
wlr-randr
libinput list-devices
```

Apply the display transform to the detected DSI output, not a guessed name:

```bash
wlr-randr --output <DSI-output> --transform 90
```

Then map the touchscreen coordinates with a targeted udev property. One common
90-degree matrix is shown below; verify it for the physical mounting:

```udev
ACTION!="remove", SUBSYSTEM=="input", KERNEL=="event[0-9]*", \
  ATTRS{name}=="<touchscreen-device-name>", \
  ENV{LIBINPUT_CALIBRATION_MATRIX}="0 -1 1 1 0 0"
```

Reload rules, retrigger input devices, and restart the graphical session. Test
all four corners, edge controls, scrolling, drag direction, and full-screen
reachability. Reboot and repeat. A visually correct display with mirrored or
offset touch is not complete.

Rollback by removing only the targeted calibration rule and display transform;
do not change unrelated input devices.

### Diagnose and validate methodically

Run `wlr-randr` from the Cage session; a plain SSH shell without the Wayland
environment may misleadingly show no outputs. Record output name, native mode,
transform, touchscreen name, and event node before changing anything.

Work in order: rotate early boot if supported, rotate compositor output,
confirm the application fills the panel, then calibrate touch separately.
After installing the rule:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input --action=change
sudo udevadm settle
udevadm info --query=property --name=/dev/input/<event-node> \
  | grep LIBINPUT_CALIBRATION_MATRIX
```

If the image rotates but touch remains portrait, verify the matched device and
matrix. If touch mirrors, return to identity and test one transform at a time.
If the rule does not appear, inspect `udevadm info --attribute-walk`. If it
works only until reboot, prepare the runtime rule from a boot service.

Use numbered targets in all corners and center. Test taps, horizontal/vertical
drag, scrolling, and controls near every edge. Repeat after cold boot and kiosk
restart. Write down the identity matrix and transform rollback before trying
alternatives.

### Stop gate

Continue only when the image orientation and touch coordinates both survive a cold reboot. If either fails, restore the identity transform or remove the calibration rule before changing another layer.

## Phase 3: Polish the boot handoff and remove phantom pointers

# Clean Boot and Phantom-Pointer Diagnosis

A polished boot path is a state handoff: firmware -> Plymouth -> compositor ->
browser first paint. Quiet kernel options and a retained Plymouth splash reduce
console flashes; matching dark compositor/browser backgrounds prevent a white
frame during handoff.

If a cursor appears despite hidden CSS and Xcursor settings, inspect input
classification instead of adding delays:

```bash
libinput list-devices
udevadm info --query=property --name=/dev/input/eventX
```

Some HDMI control devices expose relative axes and are classified as pointers.
Ignore only the proven devices:

```udev
ACTION!="remove", SUBSYSTEM=="input", KERNEL=="event[0-9]*", \
  ATTRS{name}=="<verified-phantom-device>", \
  ENV{LIBINPUT_IGNORE_DEVICE}="1"
```

Reload, retrigger, and cold boot. Confirm the touchscreen remains present and
the pointer does not return. Never blanket-ignore all pointer-class devices.

Keep diagnostics in the journal and over SSH even when the physical console is
visually quiet. Roll back by removing the single udev rule and rebuilding the
boot configuration if applicable.

### Isolate each visible defect

Film a cold boot in slow motion. Classify each unwanted frame as firmware,
kernel console, Plymouth, compositor, or Chromium; each has a different owner.
Do not add arbitrary sleeps—they usually lengthen the exposed frame.

A typical quiet command line keeps serial recovery while suppressing local
status noise:

```text
quiet splash plymouth.ignore-serial-consoles systemd.show_status=false
loglevel=3 vt.global_cursor_default=0
```

Back up the single-line boot command before editing. Retain Plymouth until the
compositor owns the display, use matching dark backgrounds, and give Chromium
a dark first paint; CSS loaded later cannot prevent an earlier white frame.

For a phantom cursor, inventory pointer candidates with `libinput list-devices`
and `udevadm info`. Apply one targeted ignore rule, retrigger, and restart. If
touch disappears, remove it immediately. Never match every device with
`ID_INPUT_MOUSE=1`.

Acceptance is five cold boots with no console, desktop, cursor, or white flash,
while SSH, serial diagnostics, touch, journal logs, and crash recovery remain
functional. Roll back one visual layer at a time.

### Stop gate

Continue only after repeated cold boots show a controlled splash-to-kiosk handoff with no desktop, console, white browser frame, or phantom cursor—and touch and SSH still work.

## Phase 4: Make deployment reproducible and recovery routine

# Reproducible Deployment and Recovery

Treat the appliance configuration as source: systemd units, proxy config,
kiosk launcher, udev rules, and boot assets belong in version control. Keep
credentials, Wi-Fi settings, runtime databases, and generated builds outside.

An installer should be idempotent: create required accounts/directories,
install files with explicit owners and modes, reload systemd/udev only when
needed, build to a staging location, atomically replace the served frontend,
and restart services in dependency order. Preserve application data across
upgrades and record checksums for deployed configuration.

Verify after every deployment:

```bash
systemctl is-active app.service nginx.service app-kiosk.service
curl --fail http://127.0.0.1:<port>/health
systemctl --failed --no-pager
journalctl -u app.service -u app-kiosk.service -b --no-pager
```

Acceptance includes cold boot, offline boot, backend interruption, browser and
compositor crash recovery, touch alignment, disk-space checks, and a second
device provisioned from the same instructions.

Rollback must be documented before deployment: retain the previous release,
restore prior config by checksum, re-enable the display manager if necessary,
and keep SSH usable. A deployment is reproducible only when another device can
be built and a failed upgrade can be reversed without private memory.

### Deployment contract and gates

Maintain a table mapping every asset to destination, owner, mode, validation
command, and restart trigger. Separate source, reproducible builds, runtime
data, and secrets. Back up runtime data and deployed config before writing.

Use this sequence:

```text
preflight -> backup -> stage -> validate -> atomic activate
-> daemon reload -> backend/proxy restart -> health check
-> kiosk restart -> acceptance
```

Stop and restore the backup if any gate fails. Never restart the kiosk while
the backend health endpoint or proxy validation is red. Install staged files
on the same filesystem and rename them into place so interruption cannot leave
a half-written configuration.

Record effective state, not just file existence:

```bash
systemctl show app.service --property=User,Group,ExecStart,FragmentPath
systemctl is-enabled app.service app-kiosk.service
curl --fail http://127.0.0.1:<port>/health
curl --fail http://127.0.0.1/
systemctl --failed --no-pager
```

Drill backend failure, invalid proxy config, kiosk crash loop, broken display
mode, interrupted rerun, and storage restore. Provision a second clean Pi using
only the guide; compare packages, checksums, effective service properties,
boot, and touch. Any undocumented rescue command is a missing deployment step.

### Stop gate

The deployment is complete only when a second clean device can be provisioned from the guide, an interrupted rerun is harmless, runtime data survives upgrade, and every rollback drill succeeds.

## Final cold-boot acceptance

Perform at least three cold boots. On each boot verify:

1. the splash orientation is correct
2. no desktop, login prompt, console, cursor, or white frame appears
3. the application becomes usable without network access
4. every touchscreen corner, edge, drag, and scroll works
5. backend, proxy, Cage, and Chromium recover from forced failure
6. SSH and journals remain available
7. `systemctl --failed` reports no unexplained failures

Record OS/package versions, boot time, service state, checksums, and observed
results. A result that exists only in memory is not a reproducible deployment.

## Emergency rollback card

Keep this beside the device before changing boot mode:

```bash
sudo systemctl disable --now app-kiosk.service
sudo systemctl enable display-manager.service
sudo systemctl set-default graphical.target
sudo reboot
```

If the display is unusable, run it over SSH. If SSH is also unavailable, restore
the storage image made before Phase 1. Never experiment on the only copy of
runtime data.
