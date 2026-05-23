# Scheduling local heartbeats (macOS / Linux / Windows)

For runtimes without a cloud scheduling layer (opencode, deep-agents local mode, Codex CLI without `/goal`), use the OS's scheduler. Maestro's heartbeat is a single CLI invocation — easy to wrap.

## Linux / macOS — cron

Edit your crontab:
```bash
crontab -e
```

Add:
```cron
# Maestro hourly heartbeat, 09:00–19:00 local, Mon–Fri.
# Adjust the path to your runtime (claude/codex/opencode) and the prompt invocation.
0 9-19 * * 1-5 cd /path/to/maestro && flock -n /tmp/maestro.lock \
    bash providers/claude-code/run.sh heartbeat \
    >> /tmp/maestro.log 2>&1

# Maestro end-of-day, 20:00 local, Mon–Fri.
0 20 * * 1-5 cd /path/to/maestro && flock -n /tmp/maestro.lock \
    bash providers/claude-code/run.sh eod \
    >> /tmp/maestro.log 2>&1
```

`flock -n /tmp/maestro.lock` guards against overlapping runs if one heartbeat takes longer than 1 hour (rare; the local runner sets its own lock too).

## macOS — launchd

More reliable than cron on macOS (cron doesn't wake the machine from sleep; launchd does).

Save as `~/Library/LaunchAgents/dev.maestro.heartbeat.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>dev.maestro.heartbeat</string>
    <key>WorkingDirectory</key>
    <string>/path/to/maestro</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>providers/claude-code/run.sh</string>
        <string>heartbeat</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <!-- Hourly 9-19 Mon-Fri (1=Mon, 5=Fri) -->
        <dict><key>Hour</key><integer>9</integer><key>Weekday</key><integer>1</integer></dict>
        <dict><key>Hour</key><integer>10</integer><key>Weekday</key><integer>1</integer></dict>
        <!-- … repeat for hours 11..19 and weekdays 2..5 — generate with a script -->
    </array>
    <key>StandardOutPath</key>
    <string>/tmp/maestro.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/maestro.log</string>
</dict>
</plist>
```

Load:
```bash
launchctl load ~/Library/LaunchAgents/dev.maestro.heartbeat.plist
```

## Windows — Task Scheduler

Maestro ships a PowerShell helper for Claude Code's `run.sh` flavor; for Codex/opencode you'd write your own.

```powershell
# providers/claude-code/install-task-scheduler.ps1 (excerpt)
$action = New-ScheduledTaskAction -Execute "C:\Program Files\Git\bin\bash.exe" `
    -Argument "providers/claude-code/run.sh heartbeat" `
    -WorkingDirectory "C:\path\to\maestro"

$trigger = @()
foreach ($hour in 9..19) {
    $trigger += New-ScheduledTaskTrigger -Daily -At "${hour}:00" -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday
}

Register-ScheduledTask -TaskName "Maestro Heartbeat" -Action $action -Trigger $trigger
```

Run as your user account, not SYSTEM, so credentials in `.env` and the AWS profile resolve correctly.

## systemd timer (Linux)

For systemd-based hosts, a timer + service unit is cleaner than cron:

```ini
# /etc/systemd/user/maestro-heartbeat.service
[Unit]
Description=Maestro heartbeat

[Service]
Type=oneshot
WorkingDirectory=/path/to/maestro
ExecStart=/bin/bash providers/claude-code/run.sh heartbeat
```

```ini
# /etc/systemd/user/maestro-heartbeat.timer
[Unit]
Description=Run Maestro heartbeat hourly during business hours

[Timer]
OnCalendar=Mon-Fri 09..19:00:00
Persistent=true

[Install]
WantedBy=default.target
```

Enable:
```bash
systemctl --user daemon-reload
systemctl --user enable --now maestro-heartbeat.timer
```

## Cron equivalents to the Anthropic routine

For users coming from the Anthropic routine docs and wanting equivalent local timing:

| Anthropic UTC cron | Local-time equivalent (Europe/Kiev) | Local cron |
|---|---|---|
| `0 6-16 * * 1-5` | 09:00–19:00 EEST (Mon-Fri) | `0 9-19 * * 1-5` |
| `0 17 * * 1-5` | 20:00 EEST (Mon-Fri) | `0 20 * * 1-5` |

If your local timezone differs, adjust accordingly. Local cron uses the host's local time; Anthropic Routines use UTC.
