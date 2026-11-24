# Owner-run deploy scripts and the CI workflow

Status: built, not used. **Depends on nothing** (based on main). No application code changes, no switch, no migration. The scripts are in `ops/deploy/` (see its README for the commands); the workflow is `.github/workflows/ci.yml`.

## The scripts
`deploy.sh`, `preflight.sh`, `backup_check.sh`, `rollback.sh` and `common.sh` generalise the deploy blocks used so far: the live-trade gate (`python -m mba_bot.restart_gate`, the same read-only module the earlier runbooks call; it refuses on an open live trade AND on an unreadable database), the expected-HEAD and ancestor checks, the pre-deploy backup with an integrity check of the copy, fast-forward only, the import check, Alembic only when a migration file arrives, the post-checks, and a one-line rollback. There is no override for the gate. They print no secret (a test runs them with secret-looking values in the environment and scans the output, and scans the scripts for reads of the environment file, `printenv`, `env`, `set -x` and `sudo`).

Decisions made, so they can be changed on purpose:
* A failure after the service is stopped puts the code back and starts the service; it never restores the database by itself.
* The backup is taken AFTER the stop (a consistent copy) with the SQLite online backup API through the service's own Python, so no `sqlite3` command-line tool is needed.
* The import check runs with an in-memory database URL, so it needs no secret.
* `alembic upgrade heads` (plural) so a repository with two heads still upgrades; the preflight prints whether a migration arrives.

## The CI workflow
On every pull request and every push to main: the full Python suite on Python 3.11 and 3.13 (it also runs the frontend helper tests through Node), and a separate job that installs the frontend, runs every `frontend/src/**/*.test.js` file and builds. Read-only permissions (`contents: read`), no secrets, no deploy step, no `pull_request_target`. A test checks the workflow file for those properties and that it runs every JavaScript test file in the repository.

## Not proven
* The workflow has not run on GitHub yet; whether the suite is green on Python 3.11 is UNVERIFIED (the suite was run locally on 3.13 only). If 3.11 is not what the server runs, drop it from the matrix.
* The scripts were exercised against shims, never against systemd, the real service, Nginx or Binance.
* A full run of the suite takes about 7 minutes locally; the workflow allows 40.
