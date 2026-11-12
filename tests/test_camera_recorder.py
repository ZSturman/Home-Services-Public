"""Engine gate for the native hls-recorder on synthetic live streams.

Real FFmpeg publishes two live HLS feeds the way the Pi's camera producer
does (2 s segments, rolling 6-segment playlist): one with a moving square
during [10 s, 20 s) and one with only sensor noise. The real recorder runs
against both at once. Checks: video is stored, exactly one motion event in
the motion window and none on the still feed, playback through a session
plays the stored video, two concurrent live viewers, MP4 export, protection
and deletion. No camera device is opened.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import HAS_FFMPEG

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe required")


def _publish(clip: Path, out_dir: Path) -> subprocess.Popen:
    out_dir.mkdir(parents=True)
    return subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", str(clip), "-c", "copy", "-f", "hls",
                             "-hls_time", "2", "-hls_list_size", "6", "-hls_flags", "delete_segments+independent_segments",
                             str(out_dir / "index.m3u8")])


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


@pytest.fixture
def recorder_env(tmp_path):
    from home_services.config import load_config
    from home_services.contract import _fixture_config
    from home_services.devfixtures import create_dev_fixtures
    from home_services.gate.synthetic import ClipSpec, generate
    from home_services.services import build_services

    fx = create_dev_fixtures(tmp_path)
    clips = generate(tmp_path / "clips", ClipSpec(width=640, height=360, fps=10, seconds=30, motion_start=10, motion_end=20))
    cfg_path = Path(_fixture_config(tmp_path))
    text = cfg_path.read_text()
    text = text[:text.index("[[cameras.devices]]")] + f'''[notifications]
enabled = true
[cameras]
engine = "hls-recorder"
event_quiet_seconds = 4
notification_cooldown_seconds = 0
[[cameras.devices]]
id = "motion"
name = "Motion feed"
source = "{tmp_path}/live/motion/index.m3u8"
[[cameras.devices]]
id = "still"
name = "Still feed"
source = "{tmp_path}/live/still/index.m3u8"
''' + text[text.index("[legacy_contract]"):]
    cfg_path.write_text(text)
    s = build_services(load_config(cfg_path))
    yield s, clips, tmp_path, Path(fx["mount"])
    s.db.close()


def test_hls_recorder_gate_on_synthetic_streams(recorder_env):
    from fastapi.testclient import TestClient

    from home_services.api.app import create_app
    from home_services.cameras.recorder import run_cameras
    from home_services.worker import Stop

    s, clips, tmp, mount = recorder_env
    s.db.execute("INSERT INTO camera_state (camera_id, armed, updated_at) VALUES ('motion', 1, 0)")
    feeds = [_publish(clips["motion"], tmp / "live/motion"), _publish(clips["no_motion"], tmp / "live/still")]
    t0 = time.time()
    stop = Stop()
    worker = threading.Thread(target=run_cameras, args=(s, stop), kwargs={"poll_seconds": 0.5}, daemon=True)
    worker.start()
    client = TestClient(create_app(s))
    tok, _ = s.tokens.create("t", ["cameras:read", "cameras:control"])
    h = {"Authorization": f"Bearer {tok}"}
    try:
        while not (tmp / "live/motion/index.m3u8").exists():
            time.sleep(0.2)
        time.sleep(5)
        cam = client.get("/api/v1/cameras/motion", headers=h).json()
        assert cam["status"]["state"] == "recording" and cam["capabilities"]["live_view"] == {"state": "available", "protocol": "hls"}
        live = client.post("/api/v1/cameras/motion/live-sessions", headers=h).json()
        assert live["protocol"] == "hls"

        def view() -> int:  # one live viewer: playlist, then its newest segment
            playlist = client.get(live["url"]).text
            seg = [x for x in playlist.splitlines() if x.endswith(".ts")][-1]
            return len(client.get(live["url"].rsplit("/", 1)[0] + "/" + seg).content)
        viewers = []
        threads = [threading.Thread(target=lambda: viewers.append(view())) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(viewers) == 2 and all(v > 0 for v in viewers)
        for f in feeds:
            f.wait(timeout=60)
        time.sleep(7)  # the last segments, then the quiet window closes the event
    finally:
        stop.event.set()
        worker.join(timeout=30)
        for f in feeds:
            f.kill()

    # Stored video: about the whole 30 s clip, as unmodified MPEG-TS on the recordings root.
    rows = s.db.all("SELECT * FROM camera_chunks WHERE camera_id='motion' ORDER BY start_ts")
    assert rows and 24 <= sum(r["duration"] for r in rows) <= 31
    assert all((mount / "home-services/recordings" / r["path"]).stat().st_size == r["bytes"] for r in rows)

    # Motion: exactly one event on the motion feed, inside the window; none on the still feed.
    events = s.db.all("SELECT * FROM camera_events ORDER BY started_at")
    assert [e["camera_id"] for e in events] == ["motion"], [dict(e) for e in events]
    ev = events[0]
    assert t0 + 7 <= ev["started_at"] <= t0 + 14 and ev["last_motion_at"] <= t0 + 24 and ev["ended_at"] is not None
    assert ev["notified"] == 1 and s.outbox.list()[0]["title"] == "Motion: Motion feed"
    assert "/" not in json.dumps([x["title"] for x in s.outbox.list()])

    listing = client.get("/api/v1/cameras/motion/recordings", headers=h).json()["recordings"]
    kinds = {r["kind"] for r in listing}
    assert kinds == {"motion", "continuous"}
    motion = next(r for r in listing if r["kind"] == "motion")
    assert motion["id"] == ev["id"] and motion["active"] is False and motion["size_bytes"] > 0
    assert client.get("/api/v1/cameras/still/recordings", headers=h, params={"kind": "motion"}).json()["recordings"] == []

    # Playback through a session plays the stored bytes.
    pb = client.post(f"/api/v1/cameras/motion/recordings/{motion['id']}/playback-sessions", headers=h).json()
    playlist = client.get(pb["url"]).text
    assert "#EXT-X-ENDLIST" in playlist
    joined = b"".join(client.get(pb["url"].rsplit("/", 1)[0] + "/" + x).content for x in playlist.splitlines() if x.endswith(".ts"))
    (tmp / "playback.ts").write_bytes(joined)
    assert _duration(tmp / "playback.ts") >= 10

    # Export: an MP4 of the event window, downloadable without the token.
    exp = client.post(f"/api/v1/cameras/motion/recordings/{motion['id']}/exports", headers=h).json()
    body = client.get(exp["download_url"]).content
    (tmp / "export.mp4").write_bytes(body)
    assert exp["content_type"] == "video/mp4" and len(body) == exp["size_bytes"]
    assert 8 <= _duration(tmp / "export.mp4") <= 40
    assert hashlib.sha256(body).hexdigest() == hashlib.sha256(client.get(exp["download_url"]).content).hexdigest()

    # Protection blocks deletion; deleting removes the event and its chunk files only.
    r = client.put(f"/api/v1/cameras/motion/recordings/{motion['id']}/protected", headers=h, json={"protected": True})
    assert r.json()["protected"] is True
    r = client.delete(f"/api/v1/cameras/motion/recordings/{motion['id']}", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "recording_protected"
    client.put(f"/api/v1/cameras/motion/recordings/{motion['id']}/protected", headers=h, json={"protected": False})
    before = {r["path"] for r in s.db.all("SELECT path FROM camera_chunks WHERE camera_id='motion'")}
    assert client.delete(f"/api/v1/cameras/motion/recordings/{motion['id']}", headers=h).status_code == 204
    after = {r["path"] for r in s.db.all("SELECT path FROM camera_chunks WHERE camera_id='motion'")}
    assert after < before and all(not (mount / "home-services/recordings" / p).exists() for p in before - after)
    assert client.get(f"/api/v1/cameras/motion/recordings/{motion['id']}", headers=h).status_code == 404
    assert (mount / "camera/events").is_dir() and any((mount / "camera/events").iterdir())  # legacy media untouched


def test_motion_is_ignored_while_the_stream_warms_up(monkeypatch):
    from home_services.cameras.recorder import MotionDetector
    from home_services.gate.synthetic import ANALYSIS_H, ANALYSIS_W
    dark, bright = bytes(ANALYSIS_W * ANALYSIS_H), bytes([200]) * (ANALYSIS_W * ANALYSIS_H // 10) + bytes(ANALYSIS_W * ANALYSIS_H * 9 // 10)
    seq = [dark, bright] * 20  # 20 s of strong alternating change at 2 fps
    d = MotionDetector(25, 0.005, 0.6, 2, warmup_seconds=10)
    monkeypatch.setattr(d, "frames", lambda _p: seq[:4])
    assert d.analyse(None) == []  # first 2 s: warming up
    for _ in range(4):
        d.analyse(None)  # up to 10 s
    assert d.analyse(None) != []  # after warm-up the same change is motion
    d.reset()  # a producer restart starts a new warm-up
    assert d.analyse(None) == []


def test_recorder_recovers_when_the_producer_stops_and_restarts(recorder_env):
    """The predecessor's stream service restarting (reboot, crash, USB
    reset): the feed goes stale, the monitor raises "Camera not recording",
    then the producer starts again with its segment numbers reset and the
    recorder resumes with a discontinuity instead of stalling."""
    from fastapi.testclient import TestClient

    from home_services.api.app import create_app
    from home_services.cameras.recorder import run_cameras
    from home_services.monitor import Monitor
    from home_services.worker import Stop

    s, clips, tmp, _mount = recorder_env
    live = tmp / "live/motion"
    feed = _publish(clips["no_motion"], live)
    stop = Stop()
    worker = threading.Thread(target=run_cameras, args=(s, stop), kwargs={"poll_seconds": 0.5}, daemon=True)
    worker.start()
    client = TestClient(create_app(s))
    tok, _ = s.tokens.create("t", ["cameras:read"])
    h = {"Authorization": f"Bearer {tok}"}
    try:
        while not (live / "index.m3u8").exists():
            time.sleep(0.2)
        time.sleep(7)
        snap = client.get("/api/v1/cameras/motion/snapshot", headers=h, params={"width": 320})
        assert snap.status_code == 200 and snap.headers["content-type"] == "image/jpeg" and snap.content[:2] == b"\xff\xd8"
        feed.kill()
        feed.wait()
        time.sleep(18)  # past the 15 s staleness threshold
        assert s.cameras.status("motion")["state"] == "stale"
        assert "camera:motion" in {p.key for p in Monitor(s).problems(include_backups=False)}
        r = client.get("/api/v1/cameras/motion/snapshot", headers=h)
        assert r.status_code == 409 and r.json()["error"]["code"] == "capability_unavailable"
        for f in live.iterdir():
            f.unlink()
        feed = _publish(clips["no_motion"], tmp / "live/restart")  # numbering restarts at 0
        while not (tmp / "live/restart/index.m3u8").exists():
            time.sleep(0.2)
        live.rmdir()
        (tmp / "live/restart").rename(live)  # same path, as a restarted service would recreate it
        time.sleep(8)
        assert s.cameras.status("motion")["state"] == "recording"
    finally:
        stop.event.set()
        worker.join(timeout=30)
        feed.kill()
    rows = s.db.all("SELECT * FROM camera_chunks WHERE camera_id='motion' ORDER BY start_ts")
    assert len(rows) >= 2 and rows[-1]["discontinuity"] == 1 and rows[-1]["bytes"] > 0
    hour = client.get("/api/v1/cameras/motion/recordings", headers=h, params={"kind": "continuous"}).json()["recordings"][0]
    tok2, _ = s.tokens.create("t2", ["cameras:read"])
    pb = client.post(f"/api/v1/cameras/motion/recordings/{hour['id']}/playback-sessions", headers={"Authorization": f"Bearer {tok2}"}).json()
    assert "#EXT-X-DISCONTINUITY" in client.get(pb["url"]).text
