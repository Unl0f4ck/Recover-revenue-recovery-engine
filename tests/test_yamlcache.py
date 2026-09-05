"""The config cache, and the control it must not break.

Caching config was a performance fix: `may_open` consults the per-workflow
exposure floor for every item on every pass, and `floor_for` re-read and
re-parsed `workflows.yaml` each time. A 50-case batch played forward spent 91
of its 105 seconds in the YAML parser.

The reason these tests exist is the way that fix could have gone wrong. An
`lru_cache` would have been faster still and would have silently disabled the
global kill switch, which is read from `policy.yaml` on every pass precisely so
an operator can set it while a run is in flight. This project has already had
one kill switch that was documented, plumbed and dead; a second one broken by
an optimisation would be worse, because the file would look right.
"""
from __future__ import annotations

import time

import pytest

from src import yamlcache
from src.policy import load_policy
from src.recovery.channels import load_workflows
from src.recovery.declines import load_declines


def test_an_edit_is_seen_by_the_next_read(tmp_path):
    """The property the kill switch depends on."""
    f = tmp_path / "c.yaml"
    f.write_text("kill: false\n", encoding="utf-8")
    assert yamlcache.load_yaml(f)["kill"] is False
    # A same-second rewrite: the cache key carries mtime in nanoseconds and
    # size, so it must not need a clock tick to notice.
    f.write_text("kill: true\n", encoding="utf-8")
    assert yamlcache.load_yaml(f)["kill"] is True


def test_a_same_length_edit_is_still_seen(tmp_path):
    """Size alone would miss this one, which is why the key is mtime AND size."""
    f = tmp_path / "c.yaml"
    f.write_text("mode: aaa\n", encoding="utf-8")
    assert yamlcache.load_yaml(f)["mode"] == "aaa"
    time.sleep(0.01)
    f.write_text("mode: bbb\n", encoding="utf-8")
    assert yamlcache.load_yaml(f)["mode"] == "bbb"


def test_an_unchanged_file_is_not_reparsed(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("a: 1\n", encoding="utf-8")
    first = yamlcache.load_yaml(f)
    assert yamlcache.load_yaml(f) is first, "the cache did not hold"


def test_a_missing_file_still_raises(tmp_path):
    """A cache that swallowed a missing config would turn a loud startup
    failure into a silent one, which is the wrong direction for a file that
    holds safety limits."""
    with pytest.raises(FileNotFoundError):
        yamlcache.load_yaml(tmp_path / "nope.yaml")


def test_the_real_kill_switch_survives_a_mid_run_edit(tmp_path, monkeypatch):
    """End to end, against the actual policy file.

    Written as a copy rather than by editing `config/policy.yaml` in place: a
    test that mutates a real safety config and then restores it leaves the
    switch armed if it fails in between.
    """
    import src.policy as P
    src = (P.CONFIG / "policy.yaml").read_text(encoding="utf-8")
    d = tmp_path / "config"
    d.mkdir()
    (d / "policy.yaml").write_text(src, encoding="utf-8")
    monkeypatch.setattr(P, "CONFIG", d)

    assert load_policy()["bounds"]["global_kill_switch"] is False
    (d / "policy.yaml").write_text(
        src.replace("global_kill_switch: false", "global_kill_switch: true"),
        encoding="utf-8")
    assert load_policy()["bounds"]["global_kill_switch"] is True, (
        "an operator setting the kill switch mid-run would not be seen")


def test_the_loaders_return_what_they_always_returned():
    assert load_declines()["policy_version"]
    assert "channels" in load_workflows()
    assert "bounds" in load_policy()
