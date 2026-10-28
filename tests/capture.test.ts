import { afterEach, describe, expect, test } from "bun:test"
import { execFileSync } from "node:child_process"
import { readdir, readFile } from "node:fs/promises"
import { dirname, isAbsolute } from "node:path"
import {
  CaptureError,
  capturePhoto,
  parseAdbDevices,
  parseCaptureOptions,
  selectDevice,
  type CapturePhase,
} from "../src/capture.ts"
import { makeFakes as createFakes, realFfmpeg, type Fakes, type FakeSetup } from "./fakes.ts"

const SLOW = 20_000

const created: Fakes[] = []
const makeFakes = async (setup?: FakeSetup) => {
  const fakes = await createFakes(setup)
  created.push(fakes)
  return fakes
}
afterEach(async () => {
  await Promise.all(created.splice(0).map((fakes) => fakes.cleanup()))
})

async function captureError(promise: Promise<unknown>): Promise<CaptureError> {
  const error = await promise.then(
    () => undefined,
    (reason: unknown) => reason,
  )
  expect(error).toBeInstanceOf(CaptureError)
  return error as CaptureError
}

const isAlive = (pid: number) => {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

const readLines = (path: string) =>
  readFile(path, "utf8").then(
    (text) => text.trim().split("\n"),
    () => [],
  )

describe("options and device selection", () => {
  test("camera defaults to back and rejects unknown cameras", () => {
    expect(parseCaptureOptions(undefined)).toEqual({ serial: undefined, camera: "back" })
    expect(parseCaptureOptions({ serial: " abc ", camera: "front" })).toEqual({ serial: "abc", camera: "front" })
    expect(() => parseCaptureOptions({ camera: "rear" })).toThrow(/back, front, external/)
    expect(() => parseCaptureOptions({ serial: "" })).toThrow(/serial/)
  })

  test("parses adb devices output", () => {
    const stdout = "* daemon started successfully\nList of devices attached\nemulator-5554\tdevice\nR58M\tunauthorized\n\n"
    expect(parseAdbDevices(stdout)).toEqual([
      { serial: "emulator-5554", state: "device" },
      { serial: "R58M", state: "unauthorized" },
    ])
    expect(parseAdbDevices("List of devices attached\n\n")).toEqual([])
  })

  test("never picks one of several devices on its own", () => {
    const devices = [
      { serial: "A", state: "device" },
      { serial: "B", state: "unauthorized" },
    ]
    expect(() => selectDevice(devices)).toThrow(/Multiple adb devices attached: A \(device\), B \(unauthorized\)/)
    expect(selectDevice(devices, "A")).toBe("A")
    expect(() => selectDevice(devices, "B")).toThrow(/accept the USB debugging prompt/)
    expect(() => selectDevice(devices, "C")).toThrow(/No adb device with serial C/)
    expect(() => selectDevice([])).toThrow(/No Android device found/)
    expect(() => selectDevice([{ serial: "A", state: "offline" }])).toThrow(/A is offline/)
  })
})

describe.skipIf(!realFfmpeg || process.platform === "win32")("capturePhoto", () => {
  test(
    "returns a JPEG taken after warmup and removes the recording",
    async () => {
      const fakes = await makeFakes()
      const phases: CapturePhase[] = []
      const photo = await capturePhoto({}, { onPhase: (phase) => void phases.push(phase) }, fakes.runtime)

      expect(isAbsolute(photo)).toBe(true)
      expect(dirname(dirname(photo))).toBe(fakes.tempRoot)
      expect(await readdir(dirname(photo))).toEqual(["photo.jpg"])
      const bytes = await readFile(photo)
      expect([bytes[0], bytes[1]]).toEqual([0xff, 0xd8])
      expect(phases).toEqual(["checking-tools", "selecting-device", "checking-android", "recording", "extracting", "complete"])

      const [r, g, b] = execFileSync(realFfmpeg!, ["-loglevel", "error", "-i", photo, "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
      expect(r!).toBeGreaterThan(200)
      expect(g! + b!).toBeLessThan(80)

      const args = await readLines(fakes.scrcpyLog)
      expect(args).toEqual(
        expect.arrayContaining([
          "--serial=FAKE123",
          "--video-source=camera",
          "--camera-facing=back",
          "--no-audio",
          "--no-playback",
          "--time-limit=2",
          "--video-codec=h264",
          "--max-size=1920",
        ]),
      )
    },
    SLOW,
  )

  test(
    "uses the requested serial and camera among several devices",
    async () => {
      const fakes = await makeFakes({
        devices: [
          { serial: "ONE", state: "device" },
          { serial: "TWO", state: "device" },
        ],
      })
      await capturePhoto({ serial: "TWO", camera: "front" }, {}, fakes.runtime)
      const args = await readLines(fakes.scrcpyLog)
      expect(args).toContain("--serial=TWO")
      expect(args).toContain("--camera-facing=front")
    },
    SLOW,
  )

  test.each([
    [{ devices: [] }, "no-device", /USB debugging/],
    [{ devices: [{ serial: "A", state: "device" }, { serial: "B", state: "device" }] }, "multiple-devices", /Pass `serial`/],
    [{ devices: [{ serial: "A", state: "unauthorized" }] }, "unauthorized", /accept the USB debugging prompt/],
    [{ sdk: "30" }, "unsupported-android", /Android 12 \(API 31\)/],
    [{ scrcpy: "missing" as const }, "missing-dependency", /scrcpy.*\n.*brew install scrcpy/],
    [{ scrcpy: "broken-version" as const }, "missing-dependency", /--version` failed \(exit 134\).*brew install scrcpy`\.\ndyld.*libavformat/],
  ])("fails before recording: %p gives %s", async (setup, code, message) => {
    const fakes = await makeFakes(setup)
    const error = await captureError(capturePhoto({}, {}, fakes.runtime))
    expect(error.code).toBe(code as CaptureError["code"])
    expect(error.message).toMatch(message)
    expect(await readLines(fakes.scrcpyLog)).toEqual([])
    expect(await readdir(fakes.tempRoot)).toEqual([])
  })

  test("removes its temp directory when scrcpy fails", async () => {
    const fakes = await makeFakes({ scrcpy: "fail" })
    const error = await captureError(capturePhoto({ camera: "external" }, {}, fakes.runtime))
    expect(error.code).toBe("recording-failed")
    expect(error.message).toContain("Could not find any camera")
    expect(error.message).toContain("--list-cameras")
    expect(await readdir(fakes.tempRoot)).toEqual([])
  })

  test(
    "force-kills a hung scrcpy and its children on timeout",
    async () => {
      const fakes = await makeFakes({ scrcpy: "hang" })
      const error = await captureError(capturePhoto({}, {}, fakes.runtime))
      expect(error.code).toBe("timeout")
      expect(error.phase).toBe("recording")
      const childPid = Number(await readFile(fakes.childPidFile, "utf8"))
      expect(isAlive(childPid)).toBe(false)
      expect(await readdir(fakes.tempRoot)).toEqual([])
    },
    SLOW,
  )

  test(
    "cancellation stops scrcpy and cleans up",
    async () => {
      const fakes = await makeFakes({ scrcpy: "hang" })
      const controller = new AbortController()
      const pending = capturePhoto(
        {},
        { signal: controller.signal, onPhase: (phase) => void (phase === "recording" && setTimeout(() => controller.abort(), 300)) },
        fakes.runtime,
      )
      const started = Date.now()
      const error = await captureError(pending)
      expect(error.code).toBe("cancelled")
      expect(Date.now() - started).toBeLessThan(4_000)
      const childPid = Number(await readFile(fakes.childPidFile, "utf8"))
      expect(isAlive(childPid)).toBe(false)
      expect(await readdir(fakes.tempRoot)).toEqual([])
    },
    SLOW,
  )
})
