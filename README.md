<img src="assets/icon.svg" width="64" height="64" alt="">

# autonomous-lab-test

Created in [T3 Code](https://t3.codes).

## Android camera photo tool

`.opencode/plugins/android-camera/` is an OpenCode plugin. OpenCode loads it automatically from this repository. It adds one tool, `android_take_photo`, which records two seconds of an Android device's camera with scrcpy and saves one frame as a JPEG.

The tool returns only the absolute path of that JPEG, for example `/var/folders/.../T/opencode-android-camera-Ab12Cd/photo.jpg`. The photo stays on this computer. Nothing is uploaded. The file is left in the OS temp directory for the caller or the OS to clean up.

The JPEG is a video frame, capped at 1920 pixels on the long side. It does not have the resolution, HDR, or processing of the phone's camera app. The tool never captures the device screen.

### Requirements

- Android 12 (API 31) or newer, with USB debugging enabled and this computer authorized.
- `adb` from [Android platform-tools](https://developer.android.com/tools/releases/platform-tools).
- [scrcpy](https://github.com/Genymobile/scrcpy#get-the-app) 2.5 or newer (`brew install scrcpy`). The tool uses `--no-window`, which scrcpy added in 2.5.
- [ffmpeg](https://ffmpeg.org/download.html) (`brew install ffmpeg`).
- [Bun](https://bun.sh) to install dependencies and run the tests.

All three commands must be on `PATH`. The tool checks for them before it touches the device and names every missing one.

### Install

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

The capture runs these steps in order: `checking-tools`, `selecting-device`, `checking-android`, `recording`, `extracting`, `complete`. Each step is reported as tool progress. Device-selection errors explain what to do, for example accepting the USB debugging prompt or passing `serial`; other capture errors also identify the step.

Timeout and cancellation terminate the local process group on macOS and Linux and wait for the direct child to close. They do not wait for every descendant or the device-side helper to exit. The helper may release the camera shortly afterward. On Windows, only the direct process is terminated.

The recording command is:

```sh
scrcpy --serial=<serial> --video-source=camera --camera-facing=<camera> --no-audio --no-playback \
  --no-window --no-control --video-codec=h264 --max-size=1920 --time-limit=2 --record=<tmp>/recording.mp4
```

ffmpeg then takes a frame from the last half second of the recording, after auto-exposure and focus have settled.

### Using the module directly

```ts
import { capturePhoto } from "./src/capture.ts"

const path = await capturePhoto({ camera: "back" }, { signal: AbortSignal.timeout(60_000) })
```

### Tests

```sh
bun test
bun run typecheck
```

The tests use fake `adb` and `scrcpy` scripts, so they need no device. The fake scrcpy writes a synthetic video with the real ffmpeg: one black second, then one red second. The test checks that the photo is red, which proves the frame comes from after warmup. The ffmpeg-based tests are skipped when ffmpeg is missing.
