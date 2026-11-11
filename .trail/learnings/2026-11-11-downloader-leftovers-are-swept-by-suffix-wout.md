---
title: Downloader leftovers are swept by suffix and video ID
author: Wout
date: 2026-11-11
branch: fix/sweep-download-leftovers
id: l-149193dd
scope: repo
status: active
src: agent
touches:
  - "hcr_sync/local_files.py#remove_download_leftovers"
---

Legacy downloaders left thumbnails (.jpg/.webp) and unconverted .webm files next to the audio, and they outlived the audio when a track was deleted, so phones kept syncing them. Apply-mode local scans now remove top-level files whose suffix is in LEFTOVER_EXTENSIONS and whose name ends in a bracketed 11-character video ID, through reconcile._trash_file so HCR_DELETE_MODE applies. Ruled out: deleting only images whose audio sibling is gone, because sibling thumbnails are already embedded in the MP3 and add nothing. Watch out: files without the [id] suffix (cover art, user media) are deliberately left alone.
