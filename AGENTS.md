# AGENTS.md - Hardcore Radio Logger Sync

This project is a SQLite-backed sync system for Hardcore Radio observations, a local music folder, YouTube downloads, and Spotify playlist membership.

## Project Rules

- The SQLite database is the source of truth.
- Logger files, local music files, YouTube, and Spotify are inputs or outputs.
- Exclusions and tombstones must never be resurrected by logger imports.
- Destructive operations must default to dry-run and require `--apply`.
- Automatic reconciliation must use baseline checks, anti-mass-delete guards, and two-pass deletion confirmation by default.
- Local file removals default to trash mode.
- Use Spotipy Authorization Code Flow for Spotify.
- Store the Spotipy token cache outside the repository with `HCR_SPOTIFY_TOKEN_CACHE`.
- Use fake Spotify and YouTube clients in tests. Tests must not hit real external APIs.
- systemd files committed to the repo are templates, not rendered local units.
- systemd should run `python -m hcr_sync run-once --apply`.
- If `HCR_RUN_POLLER=true`, `run-once` polls Hardcore Radio before import.
- `run-once` should scan current Spotify playlist entries before reconciliation and YouTube sync so Spotify-added tracks become wanted YouTube download candidates.
- ID-less local audio completion must stay explicit (`--complete-idless-local` or config opt-in); normal timer runs should not suddenly download replacements for every imported local file.
- If `HCR_RUN_POLLER=false`, an external logger producer must continue writing the configured logger files.
- A legacy downloader must not run at the same time as `youtube sync --apply` or `run-once --apply` unless explicitly allowed.

## Privacy And Git Hygiene

Do not commit:

- machine-specific migration notes
- detected systemd units
- process output
- local usernames or hostnames
- local absolute paths
- `.env`
- SQLite DB files
- logs
- runtime JSONL/TSV files
- audio files
- token caches
- rendered local systemd units
- trash or archive folders

Do not ignore `*.sql` globally because schema or migration SQL can be source files.

## Validation

Before committing, verify runtime ignores and fixture exceptions:

```bash
git status --short --ignored
git check-ignore .env
git check-ignore hcr_music.db
git check-ignore hcr_music.db-wal
git check-ignore seen-tracks.jsonl
git check-ignore played-tracks.tsv
git check-ignore youtube-download-catalog.jsonl
git check-ignore youtube-download-success.tsv
git check-ignore hcr-sync.log
git check-ignore .spotipy-cache
git check-ignore LOCAL_MIGRATION.md
git check-ignore .local-migration/example.txt
git check-ignore migration-snapshot/example.jsonl
git check-ignore legacy-archive/example.tsv
! git check-ignore tests/fixtures/example.jsonl
! git check-ignore tests/fixtures/example.tsv
git diff --cached --name-only
```

Stage source files only.
