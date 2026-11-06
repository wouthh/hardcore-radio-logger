---
title: "Tool holds expire on their own and playlist reads pay for themselves, so neither can stall the sync"
author: Wout
date: 2026-11-06
branch: auto-clear-sync-holds
id: l-c1d889ea
scope: repo
status: active
replaces: l-ebd1cb61
src: agent
---

## Decided
- YouTube missing-tool and configuration pauses are ordinary timed pauses on the 1/2/4/6-hour backoff, not operator holds. An operator hold once stalled downloads for three weeks after a tool hiccup had already gone away. `youtube resume --apply` can still clear one early.
- A full Spotify playlist read raises the invocation's request budget by its own cost. A fixed budget once blocked every run when a playlist of ~1,000 entries needed 22 requests against a budget of 20, and pending adds piled up.

## Tried and ruled out
- Raising `HCR_SPOTIFY_REQUEST_BUDGET` by hand: it breaks again every ~50 playlist entries.

## Watch out
- A successful timer exit doesn't prove progress. Check the dates of the last `youtube_downloaded` / `spotify_added` events and the `youtube_provider_pause` row.
- A YouTube `restriction` pause (sign-in/bot check, 12-72h) is a real provider block, not a config issue.

In the code:
- `TOOL_HOLDS` and `pause_active` in the YouTube queue; the self-extending budget in `SpotipyClient.playlist_snapshot`
