# Camera engine and feasibility gate

**Ownership update, October 25, 2026:** the single USB producer now runs as
`home-services-camera-stream` from Home Services' compatibility runtime.
`workspace-camera-stream` is disabled. The recorder still consumes the same
private HLS path. Camera Monitor previews/plays streams; its local-camera
feature neither publishes nor records a Pi USB camera. The producer is required
for this deployment. See [INTEGRATIONS.md](INTEGRATIONS.md) for the complete
flow and MIGRATION.md for the verified handoff/rollback.
The original engine decision below retains its date.


## Decision (October 20, 2026)

Home Services records with its own **`hls-recorder`** engine. Frigate was
the original candidate and is not deployed on the Pi:

- **Device ownership.** `/dev/video0` (Logitech Brio 101) is held by the
  predecessor's `workspace-camera-stream` FFmpeg, which already writes a
  720p/10 fps H.264 HLS stream to `/run/workspace-camera-hls/`. Any engine
  that opens the device would have to replace a live producer. The recorder
  consumes that HLS output read-only instead, so nothing is handed over and
  no rollback of the live feed is ever needed.
- **Resources.** The Pi 5 has 4 GB, about 2.2 GB of which is available with
  Home Platform's containers running, and no detection accelerator. Frigate
  without a detector adds a large container but only motion and recording
  value. The recorder copies segments unchanged and decodes about 2 small
  grey frames per second: about 12% of one core and 55 MB RSS on the Pi.
- **Fit.** Live view, continuous plus motion recording, history, playback,
  MP4 export, protection, deletion, retention, and ntfy notifications are
  what Camera Monitor uses; the recorder provides all of them through the
  existing contract.

Note: Home Platform's go2rtc also declares a `brio` stream that opens
`/dev/video0` on demand. It can only work while the predecessor's stream is
stopped. This is reported, not changed.

## hls-recorder gate (synthetic streams; required before a real camera)

`tests/test_camera_recorder.py` publishes two live HLS feeds with real
FFmpeg the way the Pi's producer does (2 s segments, rolling 6-segment
playlist, `delete_segments`): one with a bright square moving during
[10 s, 20 s) of a 30 s clip and one with only sensor noise. It runs the real
recorder on both at once, plus the API, and checks:

| Check | Requirement |
| --- | --- |
| storage | 24–31 s of video stored as unmodified MPEG-TS; file sizes match the database |
| motion | exactly one event, on the motion feed, starting 7–14 s after the clip starts and with last motion ≤ 24 s; nothing on the still feed |
| notification | one "Motion: <camera>" message for the armed camera; no paths |
| listing | motion and continuous recordings; still feed has no motion recordings |
| playback | a session playlist with ENDLIST whose segments decode to ≥ 10 s |
| live | two concurrent viewers each fetch the playlist and newest segment through a session URL |
| export | MP4 8–40 s long, download without a token, repeatable hash |
| protection/deletion | protected → 409; unprotected → 204, chunk files removed, legacy media untouched |

Results: **pass** on the development Mac (3 consecutive runs) and **pass on
the Pi** (Python 3.13, the Pi's ffmpeg), October 20, 2026.

Real camera, after the gate: enabled on the Pi with
`source = "/run/workspace-camera-hls/index.m3u8"`. Verified over Tailscale:
status `recording`, live view decodes as h264 1280×720 at 10 fps, two
concurrent viewers, continuous recording without gaps (516 s covered with a
concurrent 2 GB upload and a 549 MB restore running), 12% of one core.
**Not yet verified: real motion.** The scene was static during testing
(zero changed pixels between frames, well below the 0.5% threshold); a
person must move in front of the camera to confirm detection and the
notification. Recovery after a predecessor stream restart is handled
(sequence reset starts a new chunk with a discontinuity marker) and covered
by code, but was not exercised on the Pi because it would interrupt the
predecessor's live feed.

Tuning (`[cameras]`): `motion_pixel_threshold = 25`,
`motion_min_fraction = 0.005`, `motion_max_fraction = 0.6` (whole-frame
changes such as lights switching are ignored), `analysis_fps = 2`,
`event_quiet_seconds = 20`, `event_max_seconds = 300`,
`notification_cooldown_seconds = 120`, `retention_days = 30`,
`continuous_retention_hours = 168` (7 days). Cleanup runs every five minutes.
Protected and active motion recordings are exempt. The 200 GB camera quota
can remove unprotected footage earlier if it fills up.

## Frigate gate (kept for a future host)

`home-services gate --out <dir>` still runs the Frigate/go2rtc feasibility
steps: synthetic media, independent ground-truth motion, a loopback RTSP
fixture (mediamtx), a candidate config, and, with `--engine-url`, the engine
steps. On the Pi, with mediamtx v1.21.2 installed at
`/opt/home-services/tools/mediamtx` (official linux_arm64 release, SHA-256
verified), the automated pre-engine steps pass in the test suite
(`HOME_SERVICES_GATE_MEDIAMTX=/opt/home-services/tools/mediamtx`). The engine
steps were not run because Frigate was not installed (see the decision
above).

| Step | What it proves | Automated |
| --- | --- | --- |
| tools | ffmpeg/ffprobe present | yes |
| synthetic_media | motion/no_motion H.264 clips | yes |
| ground_truth_motion | frame differencing finds motion only inside the window | yes |
| rtsp_fixture | clip served over loopback RTSP to two consumers | yes |
| no_device_access | no gate source is a device | yes |
| candidate_config | renders `frigate-candidate.yml` | yes |
| engine_* | engine API, motion signal, live view, playback/export, deletion | with `--engine-url` and evidence files |
| resource_benchmark | MemAvailable ≥ 500 MB and load5 < 4 for 15 min | Linux, `--benchmark-seconds 900` |
