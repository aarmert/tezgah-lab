import assert from "node:assert/strict"
import { readdir, readFile, stat } from "node:fs/promises"
import { dirname, isAbsolute } from "node:path"
import { capturePhoto, parseCaptureOptions } from "../src/capture.ts"
import { runCommand } from "../src/process.ts"

const options = parseCaptureOptions({
  serial: process.argv[2],
  camera: process.argv[3] ?? "back",
})
const photo = await capturePhoto(options)
assert(isAbsolute(photo), "capture returns an absolute path")
const bytes = await readFile(photo)
assert(bytes[0] === 0xff && bytes[1] === 0xd8, "capture creates a JPEG")
assert.deepEqual(await readdir(dirname(photo)), ["photo.jpg"], "capture removes the intermediate recording")
const decoded = await runCommand("ffmpeg", ["-nostdin", "-v", "error", "-i", photo, "-f", "null", "-"], {
  timeoutMs: 15_000,
})
assert.equal(decoded.exitCode, 0, `JPEG decodes without errors. ${decoded.stderr}`)
console.error(`Verified JPEG with ${(await stat(photo)).size} bytes. The photo remains local in temp.`)
console.log(photo)
