"""Cloud submit spacing and retry, against a fake provider (no network, no sleeping)."""

from __future__ import annotations

import pytest

from lazytools.code_bridge import _cloud, _store
from lazytools.code_bridge._cloud import (  # real functions, kept before conftest patches them
    _submit_with_retry,
    _wait_for_submit_turn,
)


class FakeProvider:
    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        self.calls = 0

    def submit(self, **_kwargs):
        self.calls += 1
        if self.errors:
            raise _cloud.CloudError(self.errors.pop(0))
        return _cloud.Submission(handle="h1", url=None, env_id="env_x", raw="ok")


@pytest.fixture
def clock(monkeypatch):
    """A fake clock: sleep() advances time and records the requested delays."""
    state = {"now": 1000.0, "sleeps": []}
    monkeypatch.setattr(_cloud.time, "time", lambda: state["now"])
    monkeypatch.setattr(_cloud.time, "monotonic", lambda: state["now"])

    def sleep(seconds: float) -> None:
        state["sleeps"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(_cloud.time, "sleep", sleep)
    return state


def test_config_defaults_and_keys(tmp_path):
    assert _cloud.CloudConfig().min_submit_interval == _cloud.DEFAULT_MIN_SUBMIT_INTERVAL
    path = tmp_path / "c.toml"
    path.write_text("[cloud]\nmin_submit_interval = 5\nsubmit_retries = 0\n", encoding="utf-8")
    config = _cloud.load_config(path)
    assert config.min_submit_interval == 5.0 and config.submit_retries == 0


@pytest.mark.parametrize("line, message", [
    ("min_submit_interval = -1", "min_submit_interval must be a number"),
    ('min_submit_interval = "fast"', "min_submit_interval must be a number"),
    ("submit_retries = -1", "submit_retries must be an integer"),
    ("submit_retries = 1.5", "submit_retries must be an integer"),
])
def test_config_rejects_bad_values(tmp_path, line, message):
    path = tmp_path / "c.toml"
    path.write_text(f"[cloud]\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        _cloud.load_config(path)


def test_submits_are_spaced_by_the_minimum_interval(tmp_path, clock):
    store = _store.build_store(tmp_path / "s.sqlite")
    _wait_for_submit_turn(store, "codex", 3.0)
    assert clock["sleeps"] == []  # the first submit goes straight away
    _wait_for_submit_turn(store, "codex", 3.0)
    _wait_for_submit_turn(store, "codex", 3.0)
    assert clock["sleeps"] == [3.0, 3.0]  # a burst is spread out
    _wait_for_submit_turn(store, "claude", 3.0)
    assert clock["sleeps"] == [3.0, 3.0]  # providers are spaced independently


def test_zero_interval_disables_spacing(tmp_path, clock):
    store = _store.build_store(tmp_path / "s.sqlite")
    for _ in range(3):
        _wait_for_submit_turn(store, "codex", 0)
    assert clock["sleeps"] == []


def test_immediate_error_is_retried_then_succeeds(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(_cloud, "SUBMIT_RETRY_BASE_DELAY", 2.0)
    store = _store.build_store(tmp_path / "s.sqlite")
    prov = FakeProvider(["codex cloud exec exited 1: busy"])
    sub = _submit_with_retry(prov, store, _cloud.CloudConfig(min_submit_interval=0, submit_retries=2), "codex", {})
    assert sub.handle == "h1" and prov.calls == 2
    assert len(clock["sleeps"]) == 1 and 1.5 <= clock["sleeps"][0] <= 2.5


def test_retries_are_bounded_and_the_provider_text_is_reported(tmp_path, clock):
    store = _store.build_store(tmp_path / "s.sqlite")
    prov = FakeProvider(["codex cloud exec exited 1: rate limited"] * 5)
    with pytest.raises(_cloud.CloudError, match=r"rate limited.*after 3 attempts"):
        _submit_with_retry(prov, store, _cloud.CloudConfig(min_submit_interval=0, submit_retries=2), "codex", {})
    assert prov.calls == 3


def test_no_retry_when_disabled(tmp_path, clock):
    store = _store.build_store(tmp_path / "s.sqlite")
    prov = FakeProvider(["codex cloud exec exited 1: nope"] * 2)
    with pytest.raises(_cloud.CloudError, match="nope") as info:
        _submit_with_retry(prov, store, _cloud.CloudConfig(min_submit_interval=0, submit_retries=0), "codex", {})
    assert "attempts" not in str(info.value) and prov.calls == 1


def test_timeouts_are_not_retried(tmp_path, clock):
    store = _store.build_store(tmp_path / "s.sqlite")
    prov = FakeProvider(["codex cloud exec timed out after 300s"] * 2)
    with pytest.raises(_cloud.CloudError, match="timed out"):
        _submit_with_retry(prov, store, _cloud.CloudConfig(min_submit_interval=0, submit_retries=2), "codex", {})
    assert prov.calls == 1


def test_slow_failures_are_not_retried(tmp_path, clock):
    store = _store.build_store(tmp_path / "s.sqlite")

    class Slow(FakeProvider):
        def submit(self, **kwargs):
            clock["now"] += _cloud.SUBMIT_RETRY_WINDOW + 1  # the call itself took a long time
            return super().submit(**kwargs)

    prov = Slow(["codex cloud exec exited 1: late"] * 2)
    with pytest.raises(_cloud.CloudError, match="late"):
        _submit_with_retry(prov, store, _cloud.CloudConfig(min_submit_interval=0, submit_retries=2), "codex", {})
    assert prov.calls == 1
