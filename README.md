# Hardcore Radio Logger Sync

SQLite-backed sync for Hardcore Radio track observations, a local music folder, YouTube downloads, and a Spotify playlist.

The database is the source of truth. Logger files, the local music folder, YouTube, and Spotify are inputs or outputs. Tracks removed from either the local folder or the Spotify playlist become excluded/tombstoned so they are not downloaded or re-added later.

## Installation

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

```bash
mkdir -p ~/.config/hcr-sync
cp .env.example ~/.config/hcr-sync/hcr-sync.env
nano ~/.config/hcr-sync/hcr-sync.env
```

The CLI loads config in this order:

1. Built-in safe defaults.
2. `--config PATH`.
3. `HCR_CONFIG_FILE`.
4. `~/.config/hcr-sync/hcr-sync.env`, if present.
5. Project `.env`, for development.
6. Real process environment variables.

You do not need to `source` the env file for normal CLI usage.

## Database Initialization

```bash
python -m hcr_sync db init
python -m hcr_sync doctor
```

`doctor` is non-destructive. It prints the loaded config file and checks the DB path, music folder, logger producer, Spotify token cache, safety settings, and Git ignore coverage where available.

Set `HCR_AUDIT_VERBOSE=true` temporarily when you want extra DB audit events for every radio poll and logger import row, including whether a radio poll was a duplicate and whether a logger import row added a new observation or was an idempotent duplicate. Leave it off for quieter long-term operation.

## Importing Existing Logger Files

Configured logger files are imported idempotently:

```bash
python -m hcr_sync import-logger --dry-run
python -m hcr_sync import-logger --apply
```

Excluded tracks are never reactivated by logger input. Observations remain append-only in SQLite.

## Backfilling An Existing Music Folder

```bash
python -m hcr_sync backfill-local --dry-run
python -m hcr_sync backfill-local --apply
```

Backfill establishes the local baseline and does not infer deletions. Ongoing scans use:

```bash
python -m hcr_sync scan-local --dry-run
python -m hcr_sync scan-local --apply
```

## Spotify Setup

Spotify uses Spotipy Authorization Code Flow with these scopes:

```text
playlist-read-private
playlist-read-collaborative
playlist-modify-public
playlist-modify-private
```

Set `HCR_SPOTIFY_TOKEN_CACHE` to a path outside the repository.

Set `HCR_SPOTIFY_ENABLED=false` to keep polling, importing, local scanning, and YouTube sync running while Spotify is not ready. Spotify backfill/sync and Spotify removal detection are skipped while disabled.

Authenticate before enabling systemd:

```bash
python -m hcr_sync spotify auth
python -m hcr_sync spotify backfill --dry-run
python -m hcr_sync spotify backfill --apply
```

The systemd timer should not be the first thing that triggers OAuth.

For ongoing operation, `run-once` scans the current Spotify playlist before reconciliation and YouTube sync. Tracks that are added directly to the Spotify playlist are imported into the DB as wanted tracks, then YouTube sync can search for and download matching MP3s. Excluded tracks are not reactivated by this scan.

You can run the ongoing playlist scan directly:

```bash
python -m hcr_sync spotify scan --dry-run
python -m hcr_sync spotify scan --apply
```

Spotify sync uses conservative matching and does not auto-add source rows or candidates that look like full mixes, DJ sets, podcasts, radio shows, compilations, full albums, trailers, interviews, or other non-track items. Those are left for review instead.

When `HCR_SPOTIFY_ADD_REVIEW_MATCHES=true`, matches below `HCR_SPOTIFY_MATCH_THRESHOLD` but at or above `HCR_SPOTIFY_TENTATIVE_ADD_THRESHOLD` are added to Spotify as tentative review assets. If a tentative Spotify asset is removed later, only that Spotify candidate is marked removed; the track is not tombstoned and local audio is not moved to trash.

When Spotify returns a rate limit, sync stores a cooldown in the database and skips Spotify add-sync until that time. If the API client does not expose an exact `Retry-After`, `HCR_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS` is used.

YouTube sync normally treats any known local audio file as already local, including files that were imported without a YouTube video ID. To deliberately test or complete those files into YouTube-ID MP3 downloads, opt in explicitly:

```bash
python -m hcr_sync youtube sync --dry-run --complete-idless-local
python -m hcr_sync youtube sync --apply --complete-idless-local
```

## Using The Built-In Poller

If `HCR_RUN_POLLER=true`, `run-once` polls Hardcore Radio itself before importing logger files. The poller only writes observations. It does not download audio and does not touch Spotify.

```bash
python -m hcr_sync poll-radio --dry-run
python -m hcr_sync poll-radio --apply
```

## Using An External Logger Producer

If `HCR_RUN_POLLER=false`, an external logger producer must keep writing the configured logger files. `doctor` warns if no producer is configured or if logger files are missing or stale.

If another downloader is already managing the same music folder, disable that downloader before running:

```bash
python -m hcr_sync youtube sync --apply
python -m hcr_sync run-once --apply
```

Configured legacy downloader units in `HCR_LEGACY_DOWNLOADER_UNITS` block apply mode unless `HCR_ALLOW_LEGACY_DOWNLOADER_RUNNING=true`.

## Safe First-Run Flow

```bash
mkdir -p ~/.config/hcr-sync
cp .env.example ~/.config/hcr-sync/hcr-sync.env
nano ~/.config/hcr-sync/hcr-sync.env

python -m hcr_sync db init
python -m hcr_sync doctor

python -m hcr_sync import-logger --dry-run
python -m hcr_sync import-logger --apply

python -m hcr_sync backfill-local --dry-run
python -m hcr_sync backfill-local --apply

python -m hcr_sync spotify auth
python -m hcr_sync spotify backfill --dry-run
python -m hcr_sync spotify backfill --apply

python -m hcr_sync report
python -m hcr_sync run-once --dry-run
```

Only enable systemd after `doctor` passes, Spotify auth exists, and the dry-run output looks right.

## Running With systemd

```bash
./scripts/install-systemd-user.sh
systemctl --user daemon-reload
systemctl --user enable --now hcr-sync.timer
systemctl --user list-timers hcr-sync.timer
journalctl --user -u hcr-sync.service -f
```

The installer uses `.venv/bin/python` automatically when it exists. Set `PYTHON_BIN=/path/to/python` before running the installer to override that.

After editing the env file, restart the service or wait for the next timer run. After editing service or timer files, run:

```bash
systemctl --user daemon-reload
systemctl --user restart hcr-sync.timer
```

For service-only config changes:

```bash
systemctl --user restart hcr-sync.service
```

## Safety Model

Destructive commands default to dry-run. Use `--apply` to make changes.

Local file removals default to trash mode with `HCR_DELETE_MODE=trash`. The trash folder is configured by `HCR_TRASH_DIR`.

Reconciliation refuses destructive deletion detection when scans look unsafe, including missing folders, empty scans with known assets, suspicious scan-count drops, incomplete Spotify pagination, missing Spotify snapshot IDs, or too many removals without `--force-mass-delete`.

Two-pass deletion confirmation is enabled by default. The first healthy pass records a suspected deletion. The second healthy pass applies the global exclusion and cascades removal.

## Tombstones And Exclusions

Excluded tracks are not deleted from the database. They retain observations, events, and asset history.

Confirmed exclusion means:

- Do not download from YouTube.
- Do not add to Spotify.
- Remove from Spotify if present.
- Move local file to trash if present.
- Ignore future logger observations for activation.

Manual commands:

```bash
python -m hcr_sync exclude --track-id 123 --reason manual --apply
python -m hcr_sync exclude --artist "Artist" --title "Title" --reason manual --apply
python -m hcr_sync unexclude --track-id 123 --apply
```

Unexclude is explicit, logged, and does not erase history.

## Troubleshooting

Use:

```bash
python -m hcr_sync doctor
python -m hcr_sync report
python -m hcr_sync run-once --dry-run
```

Do not delete legacy logger files until they have been imported and the new system has run successfully. If you archive old runtime files later, prefer moving them to an archive folder outside the repository.
