# A Narrow Safe-Shutdown Boundary for a Raspberry Pi Appliance

A touchscreen appliance often needs a shutdown button, but a web application
should not become a general-purpose root control plane. This tutorial builds a
narrow path for orderly power-off on Raspberry Pi OS or Debian 13 (Trixie)
using systemd, systemd-logind, and PolicyKit.

The result authorizes one hardened system service to request normal power-off.
It does not run the application as root, grant broad sudo access, accept shell
commands from the browser, or permit shutdown that ignores inhibitors.

## Threat model

Assume an attacker can reach the application's local HTTP interface or exploit
part of the application process. The design should limit what that access can
do:

- The HTTP contract exposes one fixed shutdown operation, not a command,
  executable, argument list, or generic action name.
- The backend runs as an ordinary service account.
- Authorization is tied to the expected systemd unit and its hardening state,
  not merely to a reusable Unix username.
- The operating system retains its normal logind and inhibitor checks.
- The UI feature remains disabled until deployment authorization is proven.

This is defense in depth, not a claim that a compromised backend is harmless.
The application can request the one action it genuinely needs, while unrelated
processes running as the same account do not inherit that authorization merely
from their username.

## Architecture

```text
Touchscreen UI
    -> fixed local API endpoint
    -> unprivileged application service
    -> /usr/bin/systemctl poweroff
    -> systemd-logind over the system D-Bus
    -> PolicyKit evaluates the trusted D-Bus sender
    -> orderly power-off, subject to normal inhibitors
```

`systemctl poweroff` uses the normal systemd/logind machinery. Do not replace
this with a shell assembled from request data, direct writes to kernel power
interfaces, or a blanket passwordless-sudo rule.

## 1. Keep the application service unprivileged

Start with a normal system service. Substitute values appropriate to your
installation:

```ini
# /etc/systemd/system/<app-unit>.service
[Service]
User=<service-user>
Group=<service-group>
ExecStart=<absolute-path-to-runtime> <application-arguments>
NoNewPrivileges=yes
```

`NoNewPrivileges=yes` prevents the service and its descendants from gaining
privileges through mechanisms such as set-user-ID executables. The setting is
also useful as an authorization invariant: PolicyKit can require that the
requesting service still has this hardening enabled.

If the main unit is maintained elsewhere, use a drop-in:

```ini
# /etc/systemd/system/<app-unit>.service.d/security.conf
[Service]
NoNewPrivileges=yes
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart <app-unit>.service
systemctl show <app-unit>.service \
  --property=User \
  --property=Group \
  --property=NoNewPrivileges \
  --property=ControlGroup
```

## 2. Grant only the required PolicyKit actions

Create a root-owned rule and replace the placeholders with literal deployment
values:

```javascript
// /etc/polkit-1/rules.d/49-appliance-power.rules
polkit.addRule(function(action, subject) {
    if (
        (
            action.id == "org.freedesktop.login1.power-off" ||
            action.id == "org.freedesktop.login1.power-off-multiple-sessions"
        ) &&
        subject.user == "<service-user>" &&
        subject.system_unit == "<app-unit>.service" &&
        subject.no_new_privileges
    ) {
        return polkit.Result.YES;
    }
});
```

```bash
sudo chown root:root /etc/polkit-1/rules.d/49-appliance-power.rules
sudo chmod 0644 /etc/polkit-1/rules.d/49-appliance-power.rules
```

The two listed actions cover ordinary power-off and the case where logind sees
multiple sessions. Do not add
`org.freedesktop.login1.power-off-ignore-inhibit`: doing so would authorize the
application to bypass shutdown inhibitors. Do not reduce the rule to
`subject.user == "<service-user>"`; that would grant every matching process the
same power authority.

## 3. Avoid the `pkcheck --process` trap

It is tempting to run `pkcheck --process <pid,start-time,uid>` against the
service PID and expect the rule above to match. On current PolicyKit, that
synthetic Unix-process subject does not prove this rule.

The important `system_unit` and `no_new_privileges` properties are derived on
the trusted D-Bus sender path, where PolicyKit receives race-resistant process
identity information such as a pidfd. A manually constructed `pkcheck`
subject can therefore show the correct PID and user while omitting the two
systemd-derived properties. A failed synthetic check is not evidence that the
real logind request will fail, and a username-only check is not evidence that
the unit-scoped boundary works.

Validate through the same system D-Bus path the application will use.

## 4. Probe authorization without shutting down

`CanPowerOff()` is a query. It returns `yes`, `no`, `challenge`, or `na` and
does not power off the machine. To make the call from the real service context,
temporarily add a direct `ExecReload=` probe:

```ini
# /etc/systemd/system/<app-unit>.service.d/can-poweroff-probe.conf
[Service]
ExecReload=/usr/bin/busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager CanPowerOff
```

```bash
sudo systemctl daemon-reload
sudo systemctl reload <app-unit>.service
sudo journalctl -u <app-unit>.service --since "2 minutes ago" --no-pager
```

The service-context output should contain:

```text
s "yes"
```

An interactive shell may return `s "challenge"`; that contrast is desirable.
Remove the probe immediately after the test:

```bash
sudo rm /etc/systemd/system/<app-unit>.service.d/can-poweroff-probe.conf
sudo systemctl daemon-reload
systemctl show <app-unit>.service --property=ExecReload
```

On systemd versions that support it, a `ScheduleShutdown("dry-poweroff", ...)`
call through the same real D-Bus path can provide another non-destructive
probe. Confirm the installed systemd API before using it, cancel any scheduled
operation during cleanup, and prefer `CanPowerOff()` when only authorization
needs to be established.

## 5. Enable the application feature last

Treat application enablement and operating-system authorization as two
separate gates. Only after the service-context probe returns `yes` should the
deployment enable its shutdown capability, for example:

```ini
# /etc/systemd/system/<app-unit>.service.d/power.conf
[Service]
Environment=<APP_SHUTDOWN_ENABLED>=1
```

After restarting the service, verify both the effective hardening and the
application's capability endpoint. The backend should invoke a fixed absolute
path and fixed argument list equivalent to:

```text
/usr/bin/systemctl poweroff
```

The browser should be able to request only that operation. Require explicit
confirmation, strengthen the warning when active work will be interrupted,
prevent duplicate submission, and show a terminal shutting-down state after
the backend accepts the request.

## 6. Perform the hardware acceptance test

Automated tests cannot prove that power actually falls cleanly or that the
appliance recovers. Complete one controlled physical test:

1. Confirm the service runs as the intended user with
   `NoNewPrivileges=yes` and that the shutdown capability is enabled.
2. Exercise cancel behavior from both idle and active-work confirmation
   states.
3. From an idle state, confirm one real shutdown in the UI.
4. Observe the display extinguish and network access disappear after an
   orderly shutdown.
5. Restore power through the hardware's supported method.
6. Verify the application service, reverse proxy, and kiosk or display service
   start automatically.
7. Verify the shutdown capability and hardening survived the reboot.
8. Confirm `systemctl --failed` reports no failed units.

Record the pre-shutdown uptime and the new boot timestamp so the evidence
distinguishes a real reboot from a browser or service restart.

## What this design achieves

The completed boundary is intentionally small: a hardened, unprivileged
service can ask logind for normal orderly power-off, and PolicyKit grants that
request only when the trusted D-Bus caller has the expected user, unit, and
`NoNewPrivileges` state. The operating system still owns the shutdown process
and its inhibitors. That is a much safer foundation for an appliance power
button than root application code or broad sudo authority.

## Version and security review

This tutorial was verified for publication against Raspberry Pi OS / Debian 13
(Trixie). Recheck the installed systemd and PolicyKit behavior before applying
it to a different release, and perform a deployment-specific security review
before granting power authority to any application service.
