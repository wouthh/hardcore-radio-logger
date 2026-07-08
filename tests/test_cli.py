from hcr_sync.cli import fatal_reconcile_refusals


def test_spotify_reconcile_refusals_are_nonfatal_for_run_once():
    assert fatal_reconcile_refusals(["spotify: playlist fetch failed"]) == []


def test_local_reconcile_refusals_remain_fatal_for_run_once():
    assert fatal_reconcile_refusals(["local: music dir does not exist"]) == ["local: music dir does not exist"]
