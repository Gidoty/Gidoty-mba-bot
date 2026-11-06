# Off-server encrypted backups: owner runbook

Status: DRAFT until the reviewer approves the pull request. The owner runs every command below, one block at a time. Claude has no access to the server and never sees a key. Decisions (owner, 6 Nov): back up every 6 hours; keep the 6-hourly objects 7 days under `recent/`, plus `daily/` 30 days, `weekly/` 12 weeks and `monthly/` 12 months; encrypt with `age` (public key on the server only); alerts through a separate ops Telegram bot plus a weekly "backups OK" message; an external dead-man ping; the private key held by the owner plus one sealed offline copy in another place; keep cron; `env.pre-*` files leave the backups folder and are never uploaded.

## 0. What runs, and what changed

| Piece | Where | What it does |
|---|---|---|
| `/opt/mba-bot/backup_db.sh` (extended) | cron `0 3,9,15,21 * * *` (UTC) | the existing steps unchanged (sqlite3 `.backup`, `integrity_check`, 30-day local delete), plus: an alert if anything fails, two success markers, and a last step that sends the new copy off the server |
| `ops/backup/mba_offsite.py` | called by the script | checks the one named copy, compresses, encrypts with `age`, uploads with `rclone`, verifies size and hash, writes the markers, pings the dead-man service |
| `ops/backup/mba_watchdog.py` | cron `7 * * * *` | read-only; alerts when a marker is stale (8 hours for the 6-hourly ones, 26 hours for the daily copy) |
| `ops/backup/mba_alert.py` | called by the others | Telegram message with fixed text, plus the dead-man ping |
| `ops/backup/mba_restore_test.py` | your laptop | restore drill on a scratch copy |

Nothing in the app changes. The off-server step reads exactly one file, named on its command line, which must be `mba_bot-YYYYMMDD-HHMMSS.db` directly inside `/opt/mba-bot/backups`. It never lists the folder, so `env.pre-*` files cannot be sent, and it deletes nothing locally (the script's own 30-day `find` stays the only local pruner). B2 lifecycle rules do the cloud retention, so the upload key needs no delete permission.

Object layout in the bucket: `mba/recent/`, `mba/daily/`, `mba/weekly/`, `mba/monthly/`, each holding `<UTC time>-mba_bot-<local name>.db.gz.age` and a `.manifest.json` beside it. The first successful run of each UTC day also goes to `daily/` (normally the 03:00 run); on a Sunday that run also goes to `weekly/`, and on the 1st of the month to `monthly/`.

## 1. Four secrets, four places

| Secret | Lives | Never |
|---|---|---|
| B2 **upload** key (list and write only, one bucket, prefix `mba/`) | `/etc/mba-backup/b2.env`, mode 600 | repo, chat, command lines, logs |
| B2 **restore** key (read and list) | your password manager only; typed at the drill | the server |
| `age` **private** key | your password manager plus one sealed offline copy somewhere else | the server, the bucket, the repo |
| ops-bot token, chat id, dead-man URL | `/etc/mba-backup/alert.env`, mode 600 | repo, chat, logs |
The `age` public key (not secret) is `/etc/mba-backup/recipient.txt`. The server therefore cannot decrypt any backup, and a stolen upload key can only add files.
Also keep a copy of `.env` (above all `ENCRYPTION_KEY`) in your password manager. It is not in the database and not in the bucket, and a restored database is useless for trading without it.

## 2. Backblaze (browser)

1. Turn on two-factor authentication for the account.
2. Create a **private** bucket. Write its name down (the name is not secret, but keep it out of the public repo). Note the region you chose: see section 9 for counsel.
3. Create the **upload** application key: restricted to that bucket, file name prefix `mba/`, capabilities **list files and write files only** (no read, no delete). The key ID and key are shown once: copy them straight into your password manager.
4. Create the **restore** application key: same bucket, **read files and list files**. Password manager only.
5. Lifecycle rules, one per prefix (wording on the screen is UNVERIFIED): `mba/recent/` hide after 7 days then delete; `mba/daily/` 30 days; `mba/weekly/` 84 days; `mba/monthly/` 365 days. Optionally turn on Object Lock for extra protection (UNVERIFIED; it limits flexibility).
6. Set a bucket size or cost alert.

## 3. On your computer: the encryption keys

```
age-keygen -o mba-backup-private.key
```
It prints a public key that starts with `age1`. Copy the **private key file** into your password manager AND make one sealed paper or USB copy kept somewhere else. Do not put it on the server or in the bucket. Keep the `age1...` line to type into the server in step 4.5.

## 4. On the server (as botops)

4.1 Report these to the reviewer: `du -h /opt/mba-bot/mba_bot.db`, `df -h /opt/mba-bot /dev/shm`, `free -m`, `command -v age rclone python3`.

4.2 Install `age` and `rclone` (approved, checksums verified).
* `age`: the distribution package is fine: `sudo apt-get install age`, then `age --version`.
* `rclone`: either the distribution package (older, but it has the B2 backend) or the official release. For the release, download the zip and `SHA256SUMS` from the official downloads page, then check: `sha256sum -c --ignore-missing SHA256SUMS` must print `OK` for the zip. Compare the zip's hash with the value shown in a second place (the project's GitHub release page). Unzip, copy the `rclone` file to `/usr/local/bin/`, `rclone version`.

4.3 Create the folders (once):
```
sudo install -d -m 700 -o botops -g botops /etc/mba-backup /var/lib/mba-backup
```

4.4 Put the Backblaze upload key in, WITHOUT typing it on a command line (the values never reach the shell history or `ps`):
```
umask 077
read -rsp "B2 upload key ID: " B2ID; echo
read -rsp "B2 upload application key: " B2KEY; echo
read -rp "B2 bucket name: " BUCKET
printf 'RCLONE_CONFIG_B2_TYPE=b2\nRCLONE_CONFIG_B2_ACCOUNT=%s\nRCLONE_CONFIG_B2_KEY=%s\nMBA_BACKUP_BUCKET=%s\n' "$B2ID" "$B2KEY" "$BUCKET" > /etc/mba-backup/b2.env
unset B2ID B2KEY BUCKET
ls -l /etc/mba-backup/b2.env
```
The listing must show `-rw-------` and owner `botops`. Never `cat` this file.

4.5 The public key (not secret, so it may be typed):
```
read -rp "age public key (starts with age1): " PUB
printf '%s\n' "$PUB" > /etc/mba-backup/recipient.txt; unset PUB
```

4.6 Alerts. Create a **separate** Telegram bot with BotFather (this is the ops bot, not the customer bot) and press Start on it once. Find your numeric chat id without exposing the token (for a private chat it is your user id; a bot such as userinfobot shows it). Create a check in your dead-man service with a period of 6 hours and a grace of 2 hours, and copy its ping URL. Then:
```
umask 077
read -rsp "ops bot token: " T; echo
read -rp  "your chat id: " C
read -rsp "dead-man ping URL (press Enter to skip): " D; echo
{ printf 'OPS_BOT_TOKEN=%s\nOPS_CHAT_ID=%s\n' "$T" "$C"; [ -n "$D" ] && printf 'DEADMAN_URL=%s\n' "$D"; } > /etc/mba-backup/alert.env
unset T C D
chmod 600 /etc/mba-backup/*; ls -l /etc/mba-backup
```

4.7 Move the `env.pre-*` files out of the backups folder (they hold secrets; they are never uploaded either way):
```
install -d -m 700 /home/botops/deploy-private
mv /opt/mba-bot/backups/env.pre-* /home/botops/deploy-private/
chmod 600 /home/botops/deploy-private/env.pre-*
ls -l /home/botops/deploy-private /opt/mba-bot/backups | head -20
```
From now on the deploy checklist writes new `env.pre-*` copies into `/home/botops/deploy-private/`, and deletes old ones there by hand (the script's 30-day `find` only matches `mba_bot-*.db`). Do not `cat` them.

4.8 Install the files. After the reviewer has approved the pull request and it is merged and deployed, the files are in `/opt/mba-bot/ops/backup/`. Check each file's SHA-256 against the values in the pull request, then compare the script with the one that runs today. The repository copy was written from the description of the old script, so this diff is the check that nothing else differs:
```
sha256sum /opt/mba-bot/ops/backup/*.py /opt/mba-bot/ops/backup/backup_db.sh
diff /opt/mba-bot/backup_db.sh /opt/mba-bot/ops/backup/backup_db.sh
```
The diff must show only the additions described in the script's header. If anything else differs, stop and tell the reviewer. Then keep the old script and switch:
```
cp -p /opt/mba-bot/backup_db.sh /home/botops/backup_db.sh.before-offsite
cp /opt/mba-bot/ops/backup/backup_db.sh /opt/mba-bot/backup_db.sh
chmod 755 /opt/mba-bot/backup_db.sh
python3 /opt/mba-bot/ops/backup/mba_offsite.py --check-config
```
The last command must print `config: ok`. Our code passes `--no-check-bucket` on every rclone call, because a key restricted to one bucket may not be allowed to look up or create buckets, and rclone otherwise tries to. This is UNVERIFIED until the first run below; if it still fails with an authorization error, add the `listBuckets` capability to the upload key and try again.

4.9 First run, by hand, with cron's bare environment:
```
env -i PATH=/usr/bin:/bin /opt/mba-bot/backup_db.sh; echo "exit status: $?"
ls -l /var/lib/mba-backup
```
Expect exit status 0, one `offsite: sent ...` line, the marker files `last_local_success`, `last_offsite_success`, `last_daily_success`, `last_daily_date`, no Telegram alert, and (if configured) a dead-man ping. In the Backblaze console you should see `mba/recent/` and `mba/daily/` each holding one `.gz.age` and one `.manifest.json`, and nothing else. No `env` file, no `.db` file.

4.10 Cron (`crontab -e` as botops). Replace the existing backup line and add the watchdog:
```
0 3,9,15,21 * * * /opt/mba-bot/backup_db.sh >> /opt/mba-bot/backups/backup.log 2>&1
7 * * * * /usr/bin/python3 /opt/mba-bot/ops/backup/mba_watchdog.py >> /var/lib/mba-backup/watchdog.log 2>&1
```
These times are UTC (the server's zone). The 03:00 run stays the daily one.

## 5. Tests before you trust it

* **T1, restore drill (on your laptop, required).** Copy `mba_restore_test.py` to your laptop. Run `python3 mba_restore_test.py --key mba-backup-private.key --tier daily`. It asks, at hidden prompts, for the bucket name and the RESTORE key. It must print `sha256 matches the manifest`, `integrity_check: ok`, `foreign_key_check problems: 0`, row counts, and `RESTORE TEST PASSED`. Compare the counts with the live server (`customers`, `trading_accounts`, `trades`): the live numbers may be higher by whatever happened since the backup. Until this passes, backups are NOT considered done.
* **T2, failure alert.** On the server: `mv /etc/mba-backup/recipient.txt /etc/mba-backup/recipient.txt.off`, run `/opt/mba-bot/backup_db.sh; echo $?` (expect `3`), confirm ONE Telegram alert arrived naming the step `config`, then `mv` the file back and run it again (expect 0).
* **T3, watchdog.** `python3 /opt/mba-bot/ops/backup/mba_watchdog.py --now $(( $(date +%s) + 30*3600 ))` should send one alert; the next normal hourly run then sends a short "cleared" message.
* **T4, dead-man.** After the first scheduled runs, the ping service shows pings every 6 hours. To see it alert, pause cron for a day.
* **T5, secrets.** Read `/opt/mba-bot/backups/backup.log` and `/var/lib/mba-backup/watchdog.log` by eye once: they must contain only lines like `offsite: sent ...`. Nothing that looks like a key, a token or a URL.

## 6. Running it

* Alert text always says which step failed. Read `/opt/mba-bot/backups/backup.log`, `/var/lib/mba-backup/watchdog.log` and, for upload errors, `/var/lib/mba-backup/offsite_last_error.txt` (known secrets are replaced there).
* Before every deploy: `/opt/mba-bot/backup_db.sh` by hand, then check the two markers are fresh (`ls -l /var/lib/mba-backup`).
* Monthly: restore drill (T1). Quarterly: rotate the B2 keys (create new, replace `b2.env`, delete the old ones) and re-check the lifecycle rules.
* Turning on WAL later changes nothing here; the backup interface copies a WAL database safely (tested). Do not make `/opt/mba-bot` read-only for the backup user: WAL readers need to write a small shared-memory file.
* A copy that is less than half the size of the last good upload is refused. If you really shrank the database (for example a clean-up), run `python3 /opt/mba-bot/ops/backup/mba_offsite.py /opt/mba-bot/backups/<that file> --accept-shrink` once.

## 7. A real restore (owner only, rare)

Use the restore drill's steps to get a checked, decrypted `restored.db` on a safe machine first. Then, on the server: stop the service, move the live database aside, put the restored file in place, start, read the journal, and run the kill switch `status` command: **a restore brings back the old kill switch state.** If trading must stay stopped, create the break-glass file first. The restored database needs the original `.env`. Never restore over the live file without a fresh backup of it.

## 8. Rollback of this change

```
cp -p /home/botops/backup_db.sh.before-offsite /opt/mba-bot/backup_db.sh
crontab -e     # put back:  0 3 * * * /opt/mba-bot/backup_db.sh >> /opt/mba-bot/backups/backup.log 2>&1   and delete the watchdog line
```
Nothing else was changed. Uploaded objects stay and expire through the lifecycle rules. The files in `/etc/mba-backup` and `/var/lib/mba-backup` can stay or be removed.

## 9. For counsel

* **Region and cross-border transfer.** The bucket is in a Backblaze region the owner chose, reported to be in the EU. Customer personal data (emails, trade history, encrypted exchange credentials) leaves the server and is stored in that region. Data is encrypted before upload with a key Backblaze never holds, but whether encrypted personal data still counts as personal data for transfer rules, and which transfer rules apply to the customers' countries (for example Nigeria's data protection law, and the GDPR for any EU customers), is a legal question and UNVERIFIED here.
* **Retention.** A customer who is deleted from the live database remains in backups for up to 12 months (monthly tier). The Privacy Policy should say so.
* **Purpose and access.** Backups exist only for recovery. Only the owner holds the decryption key.

## 10. Not verified by Claude

Backblaze screen wording and lifecycle options; rclone's behaviour with a bucket-restricted key (`--no-check-bucket`); the `lsjson` hash field names; the exact rclone and age release file names and checksums; the Telegram and dead-man service behaviour. The tests use a stand-in for the upload and the real `age`. They prove this code's checks, ordering, alerts and secret handling, not Backblaze.
