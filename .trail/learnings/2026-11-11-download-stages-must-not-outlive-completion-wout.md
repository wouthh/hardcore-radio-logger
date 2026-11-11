---
title: Download stages must not outlive completion
author: Wout
date: 2026-11-11
branch: fix/stage-cleanup-and-playlist
id: l-6ffe4479
scope: repo
status: active
src: agent
touches:
  - "hcr_sync/youtube_download.py#discard_stage"
  - hcr_sync/youtube_sync.py
---

Download stages live in the music folder (.hcr-youtube-work/<work_id>/) because publication hard-links output.mp3 into place. Syncthing does not keep hard links, so a stage left behind reaches phones as a duplicate 'output.mp3'. Because of that, discard_stage removes the stage after completion and recover_youtube_pending sweeps completed/cancelled leftovers. Watch out: a completed stage is kept while it is the only copy (published file missing or a different inode), and held/in-flight stages are never touched.
