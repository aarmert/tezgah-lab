// Records camera video and keeps it. The last stdout line is the absolute MP4 path.
//   bun scripts/record-video.ts <serial> [back|front|external] [seconds=4] [fps]
// An empty serial ("") lets the tool pick the only attached device.
import assert from "node:assert/strict"
import { stat } from "node:fs/promises"
import { isAbsolute } from "node:path"
import { captureVideo, parseVideoOptions } from "../src/capture.ts"
import { runCommand } from "../src/process.ts"

const [serial, camera = "back", seconds, fps] = process.argv.slice(2)
const options = parseVideoOptions({
  serial: serial || undefined,
  camera,
  seconds: seconds === undefined ? undefined : Number(seconds),
  fps: fps === undefined ? undefined : Number(fps),
})
const video = await captureVideo(options)
assert(isAbsolute(video), "capture returns an absolute path")
const probe = await runCommand(
  "ffprobe",
  ["-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries", "stream=nb_read_frames,width,height", "-of", "csv=p=0", video],
  { timeoutMs: 30_000 },
)
assert.equal(probe.exitCode, 0, `MP4 decodes without errors. ${probe.stderr}`)
console.error(`Verified MP4 with ${(await stat(video)).size} bytes (width,height,frames: ${probe.stdout.trim()}). The video remains local in temp.`)
console.log(video)
