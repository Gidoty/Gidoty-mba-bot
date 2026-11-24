# Health and status: the customer's status line, and an alert when rounds stop

Status: built. The status line is OFF (`STATUS_LINE`, default off: the route answers 404). The watchdog is a script the owner schedules; nothing runs until then. **Depends on nothing** (based on main). **No migration**, no new table.

## The customer's status line
`GET /api/account/status-line` (signed in, current consent) returns one plain sentence: `state` (`running`, `paused`, `waiting`, `on_hold`, `delayed`, `problem`), `headline`, `detail`, and the time of the last check in words ("2 minutes ago"), plus whether that check ended without a problem. The Bot status card shows it above the existing card when the switch is on, and shows nothing when it is off. It reads what the service already holds and changes nothing.

Who is in charge, most specific first: your bot is paused (`is_active` off: "contact support if you did not expect this"); waiting for your Telegram link; waiting for you to press Start Bot; a position needs a safety check; new trades are paused by the service operator (the kill switch, said as applying to everyone); your total loss stop; your daily loss limit ("until tomorrow"); the last check ran into a problem; not checked yet; not checked for a while (behind: three missed rounds, at least 15 minutes) with "this is being looked at"; running with an open position; running. Where new trades are stopped and a position is open the sentence adds that the position stays protected by its stop-loss.

No internal state name, error text, exchange message, address or secret ever appears (`last_error` is only a yes/no; a test scans every sentence). "Last check" is the time of the last attempt: the account row records the last scan whether it worked or not, so the line says "ran into a problem" when the stored error is set and does not claim a last SUCCESSFUL time it does not have.

## The round watchdog (`ops/health/mba_round_watchdog.py`)
Nothing inside the bot can say "the bot has stopped", so this is a separate read-only script, run by cron every 5 minutes (it reuses the existing backup watchdog's alert sender and rules: `ops/backup/mba_alert.py` and `mba_common.py`, the ops Telegram bot configured in `/etc/mba-backup/alert.env`, fixed text, nothing secret in a message, one alert when a problem starts, at most one repeat an hour, one short message when it clears). It alerts on:
* `WORKER_DOWN`: the service is not active (it says how many live trades are open: their stop-losses stay on the exchange, but nothing is managing them);
* `ROUNDS_STALE`: the service is active but no active account has been scanned for `MBA_ROUND_STALE_SECONDS` (default 1200, four rounds);
* `DB_UNREADABLE`: the database cannot be read.
Silent when no account is active or none has ever been scanned (no round is late). It opens the database read-only (a missing file is not created), runs `systemctl is-active` and nothing else, and places no order.

Install (owner): `*/5 * * * * /usr/bin/python3 /opt/mba-bot/ops/health/mba_round_watchdog.py` in the same crontab as the backup watchdog, with the same environment as that job (it needs `/etc/mba-backup/alert.env` readable by that user). Environment overrides: `MBA_DB`, `MBA_SERVICE`, `MBA_ROUND_STALE_SECONDS`.

The admin dashboard already shows the oldest last scan and the number of bots not scanned in 15 minutes (health panel); this alert is the part that reaches the owner without anyone looking.

## Owner steps (nothing here was run by Claude)
1. Deploy when no live trade is open. No migration. Set `STATUS_LINE=true` and restart to show the line.
2. Install the cron line above. Test it by stopping nothing: run it once by hand (`python3 ops/health/mba_round_watchdog.py`); with a healthy bot it prints nothing and sends nothing.
3. Rollback: unset `STATUS_LINE` and restart; remove the cron line.

## Not proven
`systemctl`, Telegram and cron were not involved (fake sender, local temp database; UNVERIFIED on the server). The staleness limits are judgement, not measurement: three missed rounds on the customer line, four on the owner's alert. With a worker that is alive but stuck inside one long round, the line says "behind" only after the limit.
