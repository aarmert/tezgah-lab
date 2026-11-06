# tezgah camera tool

The phone camera part of [tezgah](../README.md). `bench.py` runs `scripts/record-video.ts` from this folder for the `cam_blink` step.

## Android camera photo tool

`.opencode/plugins/android-camera/` in the tezgah root is an OpenCode plugin. OpenCode loads it automatically when started in the tezgah root. It adds one tool, `android_take_photo`, which records two seconds of an Android device's camera with scrcpy and saves one frame as a JPEG.

The tool returns only the absolute path of that JPEG, for example `/var/folders/.../T/opencode-android-camera-Ab12Cd/photo.jpg`. The photo stays on this computer. Nothing is uploaded. The tool leaves the file in the OS temp directory for the caller or the OS to clean up.

The JPEG is a video frame, capped at 1920 pixels on the long side. It does not have the resolution, HDR, or processing of the phone's camera app. The tool never captures the device screen.

### Requirements

- Android 12 (API 31) or newer, with USB debugging enabled and this computer authorized.
- `adb` from [Android platform-tools](https://developer.android.com/tools/releases/platform-tools).
- [scrcpy](https://github.com/Genymobile/scrcpy#get-the-app) 2.5 or newer (`brew install scrcpy`). The tool uses `--no-window`, which scrcpy added in 2.5.
- [ffmpeg](https://ffmpeg.org/download.html) (`brew install ffmpeg`).
- [Bun](https://bun.sh) to install dependencies and run the tests.

All three commands must be on `PATH`. The tool checks for them before it touches the device and names every missing one.

### Install

Run this once in the tezgah root, not here. It installs this folder's dependencies into the root `node_modules`, where the OpenCode plugin also finds them:

```sh
bun install
```

### Invocation

Ask the agent to take a photo, or call the tool with these inputs:

| Input | Type | Default | Meaning |
| --- | --- | --- | --- |
| `serial` | string | none | adb serial from `adb devices`. Required when more than one device is attached. |
| `camera` | `"back"`, `"front"`, or `"external"` | `"back"` | Which camera to open. |

```json
{ "camera": "front", "serial": "R58M123ABC" }
```

The tool never picks a device on its own when several are attached. It fails and lists them instead.

The capture runs these steps in order: `checking-tools`, `selecting-device`, `checking-android`, `recording`, `extracting`, `complete`. The tool reports each step as progress. Device-selection errors explain what to do, for example accepting the USB debugging prompt or passing `serial`; other capture errors also identify the step.

Timeout and cancellation terminate the local process group on macOS and Linux and wait for the direct child to close. They do not wait for every descendant or the device-side helper to exit. The helper may release the camera shortly afterward. On Windows, only the direct process is terminated.

The recording command is:

```sh
scrcpy --serial=<serial> --video-source=camera --camera-facing=<camera> --no-audio --no-playback \
  --no-window --no-control --video-codec=h264 --max-size=1920 --time-limit=2 --record=<tmp>/recording.mp4
```

ffmpeg then takes a frame from the last half second of the recording, after auto-exposure and focus have settled.

### Keeping a video

`captureVideo` records the camera and keeps the MP4 instead of extracting a photo. It accepts the same `serial` and `camera`, plus:

| Input | Type | Default | Meaning |
| --- | --- | --- | --- |
| `seconds` | number, 1 to 60 | `4` | Recording length (`--time-limit`). |
| `fps` | integer, 1 to 240 | camera default | Camera frame rate (`--camera-fps`). The camera must support it; `scrcpy --list-camera-sizes` lists rates. |

It returns the absolute path of `recording.mp4` in a new temp directory, and the caller owns that file. The frame timestamps in the MP4 are the device capture times. The phases are the same as for a photo, without `extracting`.

From the shell, the last stdout line is the MP4 path:

```sh
bun scripts/record-video.ts <serial> back 4 30     # serial "" picks the only attached device
```

`capturePhoto` records 2 seconds without `--camera-fps`, then deletes the recording.

### Using the module directly

```ts
import { capturePhoto } from "./src/capture.ts"

const path = await capturePhoto({ camera: "back" }, { signal: AbortSignal.timeout(60_000) })
```

### Tests

Run these in this folder:

```sh
bun test
bun run typecheck
```

The tests use fake `adb` and `scrcpy` scripts, so they need no device. The fake scrcpy writes a synthetic video with the real ffmpeg: one black second, then one red second. The test checks that the photo is red, which proves the frame comes from after warmup. The ffmpeg-based tests are skipped when ffmpeg is missing.
