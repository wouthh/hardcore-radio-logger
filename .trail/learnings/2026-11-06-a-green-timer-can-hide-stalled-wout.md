---
title: A green timer can hide stalled YouTube and Spotify work; check the stored holds
author: Wout
date: 2026-11-06
branch: main
cost: { minutes: 2, tokens: 78792 }
id: l-ebd1cb61
scope: repo
status: active
src: agent
---

## Decided
- To check sync health, look at when the last `youtube_downloaded` and `spotify_added` events happened, plus the hold rows in `sync_state`. The systemd result alone tells you nothing: runs exit 0 while both providers stay blocked.

## Watch out
- A YouTube pause of kind `configuration` or `missing_tool` sets `operator_required` and never expires, even after its `until` date and after the tool problem is gone. The only way to clear it is `python -m hcr_sync youtube resume --apply`. Symptom in the logs: `degraded=configuration searched=0` on every run.
- As the Spotify playlist grows, a full scan costs more: `2 + ceil(total/50)` requests. Once that goes over `HCR_SPOTIFY_REQUEST_BUDGET`, every run skips the scan (`budget_deferred=1`, `required_budget=N`) and pending adds pile up. Recovery steps are in `sync_state` key `spotify_degraded:<playlist>`. Raise the budget with headroom, because it will be exceeded again every ~50 tracks, then run `spotify scan --apply`.

In the code:
- hold keys: `youtube_provider_pause`, `spotify_degraded:<playlist_id>` (table `sync_state`)
- logs: `journalctl --user -u hcr-sync.service`
- sqlite3 CLI isn't installed on this machine; read the DB with python `sqlite3` in `mode=ro`
