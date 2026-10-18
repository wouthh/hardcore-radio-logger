# AGENTS.md - Hardcore Radio Logger Sync

This project is a SQLite-backed sync system for Hardcore Radio observations, a local music folder, YouTube downloads, and Spotify playlist membership.

## Project Rules

- The SQLite database is the source of truth.
- YouTube download starts require durable source/video intent, bounded subprocess work, and verified audio before local completion. Failed searches are not completed no-match decisions; exclusions/manual holds and provider pauses remain authoritative.
- Spotify writes require a committed pending intent and verified membership before completion; do not bypass pending recovery or the shared request budget.
- Unidentified playlist entries cannot prove removal. Source scheduling must not change another track's recording ownership or match provenance.
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
- Tentative Spotify review additions (`status='review'` and `in_playlist=1`) must not trigger global tombstone cascades when removed from Spotify because they are uncertain matches.
- ID-less local audio completion must stay explicit (`--complete-idless-local` or config opt-in); normal timer runs should not suddenly download replacements for every imported local file.
- If `HCR_RUN_POLLER=false`, an external logger producer must continue writing the configured logger files.
- A legacy downloader must not run at the same time as `youtube sync --apply` or `run-once --apply` unless explicitly allowed.
- `python -m hcr_sync` relaunches itself through a running VPN's split-tunnel launcher (`HCR_VPN_BYPASS=auto`, Mullvad for now) and falls back to the default route; keep providers generic in `hcr_sync/vpn.py`, and never exec a launcher from tests.
- `HCR_AUDIT_VERBOSE=true` is a temporary diagnostic mode that records per-poll and per-import audit events. Keep it generic and do not log secrets.

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

<!-- trail:start -->
## Trail — repo context graph

This repo is indexed in `.trail/cache/`: small linked markdown nodes that explain each
system and carry exact file:line spans, kept in sync with the code through git.

For ANY task here — understanding how something works, finding where code lives,
or scoping a change — get context from the graph before grepping or opening
source files. Re-ask freely (it's cheap) and reuse literal identifiers you
already have (symbol, error string, file name) as the query. New to this repo?
Run `trail map` first — a token-budgeted orientation (dir clusters, hubs,
hotspots), no LLM, no key.

- Run `trail ask "<your question>" --source` → ranked nodes with the relevant
  code spans inlined (each hit's ≤8-line crux by default; `--full` for whole
  definitions when the crux isn't enough). Match the tool to the task shape:
  for understanding or editing, the top node IS the answer — cite its
  `covers:` file:line spans and edit straight from `--source`. For
  exhaustive tasks ("every occurrence / every caller of this pattern"), ranked
  results are top-N, not complete — run `trail grep "<literal>"` instead
  (exhaustive over indexed files, grouped by enclosing symbol), falling back
  to raw `grep -rn` only for unindexed files.
- `trail skeleton <file>` → every definition's signature + span, ~10× cheaper
  than reading the file; use it to skim an API surface.
- `trail callers <symbol>` gives precomputed, exact edges — who calls this.
  Add `--direction out` for what it calls, or `--depth N` to walk
  transitively for the full blast radius. For structural questions, skip
  ranking and use this directly.
- Or browse: `.trail/cache/INDEX.md` lists every node; follow the links.
- Monorepos and folders of multiple repos rank fairly across sub-projects —
  hits carry `[scope/]` labels naming which one they're from. Narrow with
  `trail ask "<task>" --in <scope>/` once you know where you're working.

If a returned span is truncated ("+N more lines"), open the file at that exact
range before finalizing. Only open source files when a node genuinely lacks a
needed detail, and then at the exact file:line the node points to — never
re-read whole files.

After big code changes, refresh the graph with `trail build` (deterministic,
no API key, $0).
<!-- trail:end -->
