"""Capture state machine on a fake clock: every transition, timeout and abort."""

from __future__ import annotations

import pytest

from capture import CaptureBusy, CaptureMachine, SaveFailed, Timings, UnknownCapture, WrongState
from capture.clock import FakeClock, settle
from capture.quality import check_frame
from hardware import build_mock_hardware
from hardware.mock import mock_frame


class Hub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def broadcast(self, type_, data=None, **_):
        self.events.append((type_, data))
        return 1

    def states(self) -> list[str]:
        return [d["state"] for t, d in self.events if t == "capture.state"]

    def last(self) -> dict:
        return [d for t, d in self.events if t == "capture.state"][-1]


@pytest.fixture
def rig():
    hw = build_mock_hardware()
    hub = Hub()
    clock = FakeClock()
    kept: list[bytes] = []
    fail_keep = []

    async def on_keep(jpeg: bytes):
        if fail_keep:
            raise fail_keep.pop()
        kept.append(jpeg)
        return "photo-1"

    m = CaptureMachine(hw, hub, on_keep=on_keep, clock=clock, timings=Timings(),
                       threaded_checks=False)
    m.open()
    r = type("Rig", (), {})()
    r.hw, r.hub, r.clock, r.m, r.kept, r.fail_keep = hw, hub, clock, m, kept, fail_keep
    yield r


async def open_cover(r, opened=True):
    r.hw.cover.set_closed(not opened)
    await settle()


async def to_review(r):
    await r.m.start()
    if r.m.state == "cover_wait":
        await open_cover(r)
    await r.clock.advance(1.5)  # preview
    await r.clock.advance(3.0)  # 3-2-1
    await r.clock.advance(0.2)  # flash ramp
    assert r.m.state == "review", (r.m.state, r.m.message)


async def test_happy_path_every_state_and_tick(rig):
    r = rig
    out = await r.m.start()
    assert out["state"] == "cover_wait" and out["capture_id"]
    cid = out["capture_id"]
    cur = r.m.current()
    assert cur["message"] == "Slide the camera cover open" and cur["seconds_left"] == 60
    await r.clock.advance(10)
    assert r.m.current()["seconds_left"] == 50
    await open_cover(r)
    assert r.m.state == "preview" and r.hw.camera.is_open
    await r.clock.advance(1.5)
    assert r.m.state == "countdown" and r.m.current()["seconds_left"] == 3
    await r.clock.advance(1.0)
    assert r.m.current()["seconds_left"] == 2
    await r.clock.advance(1.0)
    assert r.m.current()["seconds_left"] == 1
    await r.clock.advance(1.0)
    assert r.m.state == "flash"
    assert r.hw.leds.history == []  # the screen ramps first
    await r.clock.advance(0.2)
    assert r.hw.leds.history == [(1.0, True), (0.0, False)]  # 100 % duty, then off
    assert r.hw.camera.captures == 1
    assert r.m.state == "review"
    cur = r.m.current()
    assert cur["review_url"].startswith(f"/api/capture/{cid}/image?t=")
    token = cur["review_url"].split("t=")[1]
    assert r.m.image(cid, token) and r.m.image(cid, "wrong") is None
    countdown = [d["seconds_left"] for t, d in r.hub.events
                 if t == "capture.state" and d["state"] == "countdown"]
    assert countdown == [3, 2, 1]
    assert r.hub.states() == ["cover_wait", "preview", "countdown", "countdown", "countdown",
                              "flash", "quality_check", "review"]
    for _t, d in r.hub.events:
        assert set(d) == {"capture_id", "state", "message", "seconds_left", "review_url"}
        assert d["capture_id"] == cid
        assert (d["review_url"] is not None) == (d["state"] == "review")
    photo = await r.m.keep(cid)
    assert photo == "photo-1" and r.kept and r.kept[0] == r.hw.camera.frame or r.kept
    assert r.m.state == "cover_reminder"
    assert r.m.image(cid, token) is None  # the frame is gone after review
    assert not r.hw.camera.is_open
    await open_cover(r, opened=False)
    assert r.m.current() == {"state": "idle"}
    assert r.hub.states()[-2:] == ["cover_reminder", "idle"]


async def test_cover_already_open_skips_cover_wait(rig):
    await open_cover(rig)
    out = await rig.m.start()
    assert out["state"] == "preview"


async def test_cover_wait_times_out(rig):
    await rig.m.start()
    await rig.clock.advance(59.9)
    assert rig.m.state == "cover_wait"
    await rig.clock.advance(0.2)
    assert rig.m.current() == {"state": "idle"}
    assert "cover" in rig.hub.last()["message"]


async def test_only_one_capture_at_a_time(rig):
    await rig.m.start()
    with pytest.raises(CaptureBusy):
        await rig.m.start()


@pytest.mark.parametrize("phase", ["preview", "countdown"])
async def test_cover_closing_aborts(rig, phase):
    await open_cover(rig)
    await rig.m.start()
    if phase == "countdown":
        await rig.clock.advance(2.5)
        assert rig.m.state == "countdown"
    await open_cover(rig, opened=False)
    assert rig.m.current() == {"state": "idle"}
    assert "cover closed" in rig.hub.last()["message"]
    assert rig.hw.camera.captures == 0 and rig.hw.leds.history == []
    assert not rig.hw.camera.is_open


@pytest.mark.parametrize("phase", ["cover_wait", "preview", "countdown", "review"])
async def test_cancel(rig, phase):
    if phase != "cover_wait":
        await open_cover(rig)
    cid = (await rig.m.start())["capture_id"]
    if phase == "countdown":
        await rig.clock.advance(1.6)
    if phase == "review":
        await rig.clock.advance(4.7)
    assert rig.m.state == phase
    out = await rig.m.cancel(cid)
    if phase == "cover_wait":
        assert out["state"] == "idle"
    else:
        assert out["state"] == "cover_reminder"  # the cover is still open
        assert rig.kept == []
        await open_cover(rig, opened=False)
        assert rig.m.current() == {"state": "idle"}


@pytest.mark.parametrize("kind,code", [("dark", "dark"), ("half_covered", "occluded"),
                                       ("blurred", "blurred"),
                                       ("overexposed", "overexposed")])
async def test_quality_rejects_then_retries(rig, kind, code):
    r = rig
    r.hw.camera.frame = mock_frame(kind)
    await open_cover(r)
    await r.m.start()
    await r.clock.advance(4.7)
    assert r.m.state == "quality_check"
    assert r.m.last_quality.code == code
    assert r.m.message and "Checking" not in r.m.message
    assert r.hub.last()["message"] == r.m.message  # the specific message is broadcast
    r.hw.camera.frame = mock_frame("good")
    await r.clock.advance(2.0)  # pause, then back to preview
    assert r.m.state == "preview"
    await r.clock.advance(4.7)
    assert r.m.state == "review" and r.m.attempts == 2


async def test_half_closed_cover_message(rig):
    rig.hw.camera.frame = mock_frame("half_covered")
    await open_cover(rig)
    await rig.m.start()
    await rig.clock.advance(4.7)
    assert "cover fully open" in rig.m.message


async def test_gives_up_after_max_attempts(rig):
    rig.hw.camera.frame = mock_frame("dark")
    await open_cover(rig)
    await rig.m.start()
    await rig.clock.advance(4.7)
    await rig.clock.advance(2.0 + 4.7)
    await rig.clock.advance(2.0 + 4.7)
    assert rig.m.state == "cover_reminder" and rig.m.attempts == 3
    assert rig.m.message.startswith("No photo taken.")


async def test_camera_error_is_a_quality_failure(rig):
    rig.hw.camera.fail_with = RuntimeError("busy")
    await open_cover(rig)
    await rig.m.start()
    await rig.clock.advance(4.7)
    assert rig.m.state == "quality_check" and rig.m.last_quality.code == "camera_error"
    assert rig.hw.leds.level == 0.0  # lights off even though the capture failed


async def test_retake(rig):
    await open_cover(rig)
    await to_review(rig)
    cid = rig.m.capture_id
    out = await rig.m.retake(cid)
    assert out["state"] == "preview"
    await rig.clock.advance(4.7)
    assert rig.m.state == "review" and rig.hw.camera.captures == 2


async def test_retake_with_cover_closed_goes_to_cover_wait(rig):
    await open_cover(rig)
    await to_review(rig)
    await open_cover(rig, opened=False)  # closing during review keeps the review
    assert rig.m.state == "review"
    out = await rig.m.retake(rig.m.capture_id)
    assert out["state"] == "cover_wait"


async def test_keep_with_cover_closed_goes_idle(rig):
    await open_cover(rig)
    await to_review(rig)
    await open_cover(rig, opened=False)
    assert await rig.m.keep(rig.m.capture_id) == "photo-1"
    assert rig.m.current() == {"state": "idle"}


async def test_review_times_out(rig):
    await open_cover(rig)
    await to_review(rig)
    await rig.clock.advance(59)
    assert rig.m.state == "review" and rig.m.current()["seconds_left"] == 1
    await rig.clock.advance(1.1)
    assert rig.m.state == "cover_reminder" and rig.kept == []
    assert "discarded" in rig.m.message


async def test_keep_failure_stays_in_review(rig):
    await open_cover(rig)
    await to_review(rig)
    rig.fail_keep.append(RuntimeError("db down"))
    with pytest.raises(SaveFailed):
        await rig.m.keep(rig.m.capture_id)
    assert rig.m.state == "review" and "Could not save" in rig.m.message
    assert await rig.m.keep(rig.m.capture_id) == "photo-1"


async def test_commands_validate_state_and_id(rig):
    with pytest.raises(UnknownCapture):
        await rig.m.keep("nope")
    cid = (await rig.m.start())["capture_id"]
    with pytest.raises(UnknownCapture):
        await rig.m.cancel("other")
    with pytest.raises(WrongState):
        await rig.m.keep(cid)
    with pytest.raises(WrongState):
        await rig.m.retake(cid)


async def test_cover_reminder_persists_until_closed_and_allows_new_capture(rig):
    await open_cover(rig)
    await to_review(rig)
    first = rig.m.capture_id
    await rig.m.cancel(first)
    assert rig.m.state == "cover_reminder"
    await rig.clock.advance(3600)
    assert rig.m.state == "cover_reminder"  # no timeout
    assert (await rig.m.cancel(first))["state"] == "cover_reminder"  # cancel does not end it
    out = await rig.m.start()  # cover still open: straight to preview
    assert out["state"] == "preview" and out["capture_id"] != first


async def test_close_mid_capture_resets_to_idle(rig):
    """A restart (or shutdown) mid-capture: lights off, camera closed, idle."""
    await open_cover(rig)
    await rig.m.start()
    await rig.clock.advance(4.5)
    assert rig.m.state == "flash"
    await rig.m.close()
    assert rig.m.current() == {"state": "idle"}
    assert rig.hw.leds.level == 0.0 and not rig.hw.camera.is_open
    fresh = CaptureMachine(rig.hw, rig.hub, on_keep=None, clock=rig.clock)
    assert fresh.current() == {"state": "idle"}


def test_quality_frames():
    assert check_frame(mock_frame("good")).ok
    assert check_frame(b"not a jpeg").code == "unreadable"
    assert {check_frame(mock_frame(k)).code for k in
            ("dark", "half_covered", "blurred", "overexposed")} == \
        {"dark", "occluded", "blurred", "overexposed"}


def test_person_detector_seam():
    class Nobody:
        def has_person(self, jpeg):
            return False

    assert check_frame(mock_frame("good"), Nobody()).code == "no_person"
