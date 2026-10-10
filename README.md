# Hardcore Radio Logger

Local-first radio discovery and library reconciliation with SQLite provenance, dry-run planning, and guarded synchronization.

> **Maintained**
>
> The project is designed for an explicitly configured local library. External synchronization is optional, authorization-dependent, and guarded by dry-run and apply boundaries.

The database is the source of truth. Logger files, the local music folder, YouTube, and Spotify are inputs or outputs. Tracks removed from either the local folder or the Spotify playlist become excluded/tombstoned so they are not downloaded or re-added later. Destructive synchronization operations default to dry-run and require an explicit `--apply` boundary. Setup commands such as `db init` and `spotify auth` can create local state without `--apply`.

Tested with Python 3.11, 3.12, 3.13, and 3.14.

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
2. `~/.config/hcr-sync/hcr-sync.env`, if present.
3. Project `.env`, for development.
4. `HCR_CONFIG_FILE`.
5. `--config PATH`.
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

Supported input is the format written by the shipped poller:

- JSONL: one JSON object per nonblank line, with a nonempty string `track` and a string `first_seen_at`.
- TSV: exactly two columns, timestamp then track; alternatively, a named header with exactly one timestamp column (`timestamp`, `time`, or `played_at`) and one track column (`track`, `title`, `name`, `artist_title`, `song`, or `query`). Header order may vary and other named columns are ignored; data rows must match the header width. Fields are literal tab-separated text as written by the poller: quotation marks in a title are preserved, not interpreted as CSV escaping.
- Source timestamps require a valid date, time including seconds, and `Z` or a numeric timezone offset, for example `2026-01-01T12:34:56Z` or `2026-01-01T12:34:56.123+02:30`. Fractional seconds are accepted. Timestamp spelling is preserved after trimming; ingestion time remains separate in `imported_at`.

Blank lines are ignored. Both configured files are validated before any import writes. A malformed nonblank record, missing/invalid timestamp, or ambiguous header rejects the entire invocation, including valid rows in the other file. The CLI returns exit status 1 and a fixed source label, line number, and reason without echoing row contents or configured paths (line 0 denotes an unreadable file). Dry-run performs the same validation without importing records. In `run-once`, an import error stops the later synchronization stages; optional polling happens before import.

Bare lines and timestamp-less historical records that were previously accepted now require correction from a trustworthy source before import. The importer does not invent an observation time or convert those records automatically.

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

Spotify comparison removes only recognized trailing generic version labels from both titles: `Original Mix`, `Original Version`, `Extended`, `Extended Mix`, `Extended Version`, `Radio Edit`, `Radio Mix`, and `Radio Version`, in parentheses, square brackets, or after a spaced dash. Repeated labels are removed until stable. Named remix/refix, year, live, acoustic, cover, and other non-generic bracketed qualifiers remain significant: differing or missing qualifiers reject even when a long base title is very similar. `2026 Edit` and `2026 Mix` retain the same year; `Nosferatu Remix Edit` and `Nosferatu Remix` retain the same remixer. Unmarked version wording stays literal and requires equal comparison titles. Ordinary title words are not removed. Unknown subtitles are retained rather than silently discarded.

Source artist lists can use commas or spaced `&`; complete provider credit names are preserved, including compound names and MC/collaborator credits. Missing source credits reject. Legacy candidates retain conservative display-based comparison. These rules affect Spotify scoring only, leaving original metadata, canonical identities, ownership, established provenance, unchanged. YouTube applies its own comparison and artist-evidence rules described below. The default thresholds remain 0.85 for automatic tentative additions and 0.90 for confident additions; text similarity does not prove identical audio. Old algorithmic review results are reconsidered on their next eligible retry when tentative additions are enabled. Retry dates, user exclusions/removals, and ownership holds remain authoritative; there is no bulk reclassification or addition. Saved partial searches keep their query sequence and lane and use current scoring when completed.

When `HCR_SPOTIFY_ADD_REVIEW_MATCHES=true`, matches below `HCR_SPOTIFY_MATCH_THRESHOLD` but at or above `HCR_SPOTIFY_TENTATIVE_ADD_THRESHOLD` are added to Spotify as tentative review assets. If a tentative Spotify asset is removed later, only that Spotify candidate is marked removed; the track is not tombstoned and local audio is not moved to trash.

Playlist scans and backfill establish membership, not match correctness. Existing associations retain their confidence and review classification; uncertain or unknown-confidence matches remain under review when they reappear. Presence still refreshes provider metadata and last-seen time and clears missing suspicion. The database keeps one `spotify_playlist_recordings` row per playlist recording, while `spotify_assets` retains the logical track's primary provenance. Additional recordings are linked only when their nonempty Spotify artist-ID sets and canonical full titles match an anchored recording; album, ISRC, and duration are retained as distinguishing metadata. Unresolved recordings stay visible as review diagnostics without being assigned to a song, and never replace another song's recording ID. Removing one recording leaves the logical song present while any linked recording remains; removal confirmation and exclusion checks still use the existing two-pass and mass-delete safeguards. Removal safety continues to use the configured confidence threshold: raising it above an asset's retained score keeps removal candidate-only, even when its membership status is `added`. Excluded parents remain excluded. If a recording ID is already owned by a different logical song, the snapshot import is rejected transactionally as an association conflict. Dry-run checks the same associations without creating or migrating the database.

These maintenance safeguards prevent future provenance changes and unstable timestamp-less imports. They do not reconstruct confidence already overwritten in an existing database or undo earlier exclusions, duplicate observations, or file movement. No automatic repair or library recovery is performed; any existing-state recovery needs a separate, evidence-based decision.

When Spotify returns HTTP 429, the database stores its `Retry-After` cooldown. Playlist scans, backfill, reconciliation requests, and add-sync all wait until it expires; local scanning, logger import, and YouTube sync continue. A successful playlist read cannot clear an active cooldown. If no usable `Retry-After` is available, `HCR_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS` is used. HTTP server errors retain their actual status rather than being mistaken for rate limits.

Spotify sync persists a playlist-scoped rotation: three completed first-time search opportunities, then one eligible retry (`HCR_SPOTIFY_FIRST_TIME_WEIGHT=3`, `HCR_SPOTIFY_RETRY_WEIGHT=1`). Each lane uses oldest eligible attempt and track ID; an empty lane yields capacity. Budget/cooldown interruptions retain their lane and completed search variants across restarts. `HCR_SPOTIFY_SYNC_LIMIT=15` remains the track limit. Unsuccessful matches and track-specific HTTP 400/404 failures retry after seven days initially and fourteen days thereafter. Failed requests record scheduling without replacing recording ownership or confidence; unsuccessful candidate IDs stay in diagnostics. Known ownership conflicts remain local review holds. Genuine 429s preserve provider reason, including quota exhaustion, without counting as unsuccessful matches; authentication, transport, and server failures remain visible.

`HCR_SPOTIFY_REQUEST_BUDGET=20` caps actual Web API sends across the whole invocation, including pagination, search variants, writes, and verification. Request/status retries must remain zero; redirects are refused. Playlist items use the documented maximum of 50 per page. A checked scan costs `2 + max(1, ceil(total / 50))`: metadata before, all pages, metadata after. For 416 entries that is 11 requests. Source work reserves up to six requests (four search variants, addition, possible compensation); the normal minimum configured budget is nine. Full before/after scans plus this worst-case work need 28 requests, so budget 20 can defer verification. OAuth token requests are outside this Web API budget.

Membership caches are reusable only after a counted fresh snapshot-version/total check. Counts, offsets, pagination, and final version must agree. Identifiable unavailable recordings remain present. Unidentifiable entries hold absence-based writes, removal confirmation, and re-addition; positive IDs can still confirm presence. If a scan fits only by itself, one bounded scan-only pass is allowed before protected source progress, with that allowance surviving snapshot changes. If the complete scan cannot fit, `spotify_degraded:<playlist>` reports required scan/work budgets and the recovery command; repeated runs do not repeatedly start an impossible scan. Raise the configured budget explicitly and run `spotify scan --apply` to recover. Cached or partial membership never authorizes negative decisions.

`spotify_pending_work` journals searches and writes. Addition/removal intent commits before dispatch. A lost response or failed local commit leaves pending evidence; recovery reads provider membership before any uncertain retry. Acknowledged writes are not blindly repeated while verification is outstanding. Failed compensation removals remain queued. Success counters and `spotify_added`/`spotify_tentatively_added` events mean membership was verified and committed; `acknowledged`, `pending`, and `budget_deferred` are separate. Standalone apply commands and timer runs share the existing sync lock. Concurrent external playlist changes can still occur; this does not promise exactly-once delivery.

Scheduling repair is evidence-only and optional. It neither transfers recording ownership nor reconstructs confidence, reverses exclusions, or moves audio. Only non-deduplicated, source/playlist-attributed scheduling evidence can fill an older or absent schedule. Ambiguous legacy records are reported and skipped. Preview first; apply requires a new owner-only SQLite backup, verified before any repair:

```bash
python -m hcr_sync spotify repair-scheduling --dry-run
python -m hcr_sync spotify repair-scheduling --apply --backup /path/to/private/rollback.sqlite
```

To check recovery, compare radio observations, `youtube_downloaded`, completed first-time/retry searches, acknowledged/pending work, verified Spotify additions, and failure events separately. A successful timer exit does not prove additions. Recheck the stored cooldown before a controlled sync. Spotify budget/cooldown deferrals allow radio discovery, imports, and YouTube processing to continue. Keep verbose auditing disabled during ordinary operation.

YouTube sync checks existing files against recording and artist evidence before treating them as local, including files imported without a YouTube video ID. Different meaningful versions cannot satisfy each other; ambiguous metadata remains held. Saved local decisions authorize missing-file handling only at the same configured YouTube match threshold. To deliberately test or complete those files into YouTube-ID MP3 downloads, opt in explicitly:

```bash
python -m hcr_sync youtube sync --dry-run --complete-idless-local
python -m hcr_sync youtube sync --apply --complete-idless-local
```

## Using The Built-In Poller

If `HCR_RUN_POLLER=true`, `run-once` polls Hardcore Radio itself before importing logger files. The poller only writes observations. It does not download audio and does not touch Spotify.

The poller requests the configured Icecast status endpoint first and falls back to the official player webpage when Icecast is unavailable or has no usable track metadata. Each run starts with Icecast again. Every HTTP attempt uses a unique request URL and no-cache/no-store headers, so the client does not reuse a previous response; the broadcaster may itself serve an older webpage track. Poll timestamps mean **observed at**; they do not establish when the broadcaster first played or published the track. The broadcaster's exact unavailable-information placeholder is rejected as missing metadata and is never searched as a song. If neither source provides usable metadata, standalone `poll-radio` fails. `run-once` records a warning and continues its remaining stages without adding a radio observation; with `HCR_AUDIT_VERBOSE=true`, apply-mode runs also record a per-poll unavailable event with sanitized failure reasons. Local file, logger, database, and synchronization safety errors still fail the run.

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

Use synchronization and download features only for media you are authorized to access and retain, and review the terms of each connected service. This project does not grant rights to third-party content.

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

## Local validation

Run `python -m pytest -q` with disposable fixtures and fake provider clients. The supported Python versions are 3.11 through 3.14; run the suite locally for each version. Before committing, also run the privacy/ignore checks in `AGENTS.md` and `git diff --check`. Hosted Actions are not required for delivery.

YouTube search, retry, and download work use a persistent 1:1:1 queue and bounded subprocess limits. See [YouTube scheduling and recovery](docs/youtube-scheduling-recovery.md) for automatic matching, failure categories, backlog repair, pauses, and safe interrupted-download recovery.
