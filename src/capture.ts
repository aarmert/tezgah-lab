import { mkdtemp, rm, stat } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { CommandAbortedError, CommandNotFoundError, CommandTimeoutError, runCommand } from "./process.ts"

export const CAMERAS = ["back", "front", "external"] as const
export type Camera = (typeof CAMERAS)[number]

export type CaptureOptions = {
  serial?: string
  camera?: Camera
}

export const CAPTURE_PHASES = [
  "checking-tools",
  "selecting-device",
  "checking-android",
  "recording",
  "extracting",
  "complete",
] as const
export type CapturePhase = (typeof CAPTURE_PHASES)[number]

export type CaptureErrorCode =
  | "invalid-options"
  | "missing-dependency"
  | "no-device"
  | "multiple-devices"
  | "device-not-found"
  | "unauthorized"
  | "device-unavailable"
  | "unsupported-android"
  | "recording-failed"
  | "extraction-failed"
  | "timeout"
  | "cancelled"

export class CaptureError extends Error {
  constructor(
    readonly code: CaptureErrorCode,
    message: string,
    readonly phase?: CapturePhase,
  ) {
    super(message)
  }
}

export type Commands = { adb: string; scrcpy: string; ffmpeg: string }

export type CaptureRuntime = {
  commands: Commands
  tempRoot: string
  recordSeconds: number
  timeouts: { probeMs: number; recordGraceMs: number; extractMs: number; killGraceMs: number }
}

export const defaultRuntime = (): CaptureRuntime => ({
  commands: { adb: "adb", scrcpy: "scrcpy", ffmpeg: "ffmpeg" },
  tempRoot: tmpdir(),
  recordSeconds: 2,
  timeouts: { probeMs: 15_000, recordGraceMs: 25_000, extractMs: 30_000, killGraceMs: 2_000 },
})

export type CaptureHooks = {
  signal?: AbortSignal
  onPhase?: (phase: CapturePhase) => void | Promise<void>
}

const MIN_SDK = 31

const INSTALL_HINTS: Record<keyof Commands, string> = {
  adb: "Install Android platform-tools (https://developer.android.com/tools/releases/platform-tools) and put adb on PATH.",
  scrcpy: "Install scrcpy 2.5 or newer (https://github.com/Genymobile/scrcpy#get-the-app), for example `brew install scrcpy`.",
  ffmpeg: "Install ffmpeg (https://ffmpeg.org/download.html), for example `brew install ffmpeg`.",
}

const VERSION_ARGS: Record<keyof Commands, string[]> = {
  adb: ["version"],
  scrcpy: ["--version"],
  ffmpeg: ["-version"],
}

const isCamera = (value: unknown): value is Camera =>
  value === "back" || value === "front" || value === "external"

export function parseCaptureOptions(input: unknown): CaptureOptions & { camera: Camera } {
  const value = input ?? {}
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new CaptureError("invalid-options", "Options must be an object with optional `serial` and `camera`.")
  }
  const serial = "serial" in value ? value.serial : undefined
  const camera = "camera" in value && value.camera !== undefined ? value.camera : "back"
  if (serial !== undefined && (typeof serial !== "string" || serial.trim() === "")) {
    throw new CaptureError("invalid-options", "`serial` must be a non-empty adb serial string.")
  }
  if (!isCamera(camera)) {
    throw new CaptureError("invalid-options", `\`camera\` must be one of ${CAMERAS.join(", ")}.`)
  }
  return { serial: serial?.trim(), camera }
}

export type AdbDevice = { serial: string; state: string }

export function parseAdbDevices(stdout: string): AdbDevice[] {
  const lines = stdout.split(/\r?\n/)
  const start = lines.findIndex((line) => line.startsWith("List of devices attached"))
  return lines
    .slice(start + 1)
    .map((line) => line.trim().split(/\t+/))
    .filter((parts) => parts.length >= 2 && parts[0] !== "")
    .map(([serial, state]) => ({ serial: serial!, state: state!.trim() }))
}

export function selectDevice(devices: readonly AdbDevice[], serial?: string): string {
  const listed = devices.map((device) => `${device.serial} (${device.state})`).join(", ")
  const chosen = serial === undefined ? undefined : devices.find((device) => device.serial === serial)

  if (serial !== undefined && !chosen) {
    throw new CaptureError(
      "device-not-found",
      `No adb device with serial ${serial}. Attached: ${listed || "none"}. Check \`adb devices\`.`,
    )
  }
  if (!chosen) {
    if (devices.length === 0) {
      throw new CaptureError(
        "no-device",
        "No Android device found by adb. Connect a device with USB debugging enabled, then check `adb devices`.",
      )
    }
    if (devices.length > 1) {
      throw new CaptureError(
        "multiple-devices",
        `Multiple adb devices attached: ${listed}. Pass \`serial\` to choose one.`,
      )
    }
  }

  const device = chosen ?? devices[0]!
  if (device.state === "unauthorized" || device.state === "authorizing") {
    throw new CaptureError(
      "unauthorized",
      `Device ${device.serial} has not authorized this computer. Unlock it and accept the USB debugging prompt, then retry.`,
    )
  }
  if (device.state !== "device") {
    throw new CaptureError(
      "device-unavailable",
      `Device ${device.serial} is ${device.state}. Reconnect it or run \`adb reconnect\`, then check \`adb devices\`.`,
    )
  }
  return device.serial
}

export function scrcpyArgs(serial: string, camera: Camera, recordPath: string, recordSeconds: number): string[] {
  return [
    `--serial=${serial}`,
    "--video-source=camera",
    `--camera-facing=${camera}`,
    "--no-audio",
    "--no-playback",
    "--no-window",
    "--no-control",
    "--video-codec=h264",
    "--max-size=1920",
    `--time-limit=${recordSeconds}`,
    `--record=${recordPath}`,
  ]
}

// The last frames are used because camera auto-exposure and focus settle during the first second.
export function ffmpegArgs(recordPath: string, photoPath: string): string[] {
  return [
    "-nostdin",
    "-hide_banner",
    "-loglevel",
    "error",
    "-y",
    "-sseof",
    "-0.5",
    "-i",
    recordPath,
    "-frames:v",
    "1",
    "-update",
    "1",
    "-q:v",
    "2",
    photoPath,
  ]
}

const tail = (text: string) => text.trim().split(/\r?\n/).slice(-8).join("\n")

const nonEmptyFile = (path: string) =>
  stat(path).then(
    (info) => info.isFile() && info.size > 0,
    () => false,
  )

export async function capturePhoto(
  options: CaptureOptions = {},
  hooks: CaptureHooks = {},
  runtime: CaptureRuntime = defaultRuntime(),
): Promise<string> {
  const { serial: requestedSerial, camera } = parseCaptureOptions(options)
  const { commands, timeouts } = runtime
  let phase: CapturePhase = CAPTURE_PHASES[0]
  let workDir: string | undefined

  const enter = async (next: CapturePhase) => {
    phase = next
    await hooks.onPhase?.(next)
  }

  const run = async (tool: keyof Commands, args: string[], timeoutMs: number) => {
    try {
      return await runCommand(commands[tool], args, { timeoutMs, signal: hooks.signal, killGraceMs: timeouts.killGraceMs })
    } catch (error) {
      if (error instanceof CommandNotFoundError) {
        throw new CaptureError("missing-dependency", `${tool} is not installed. ${INSTALL_HINTS[tool]}`, phase)
      }
      if (error instanceof CommandAbortedError) {
        throw new CaptureError("cancelled", `Photo capture was cancelled during the ${phase} phase.`, phase)
      }
      if (error instanceof CommandTimeoutError) {
        const detail = tail(error.stderr)
        throw new CaptureError(
          "timeout",
          `${tool} did not finish within ${timeoutMs} ms during the ${phase} phase and was terminated.${detail ? `\n${detail}` : ""}`,
          phase,
        )
      }
      throw error
    }
  }

  try {
    await enter("checking-tools")
    const tools = Object.keys(commands) as (keyof Commands)[]
    const checks = await Promise.allSettled(tools.map((tool) => run(tool, VERSION_ARGS[tool], timeouts.probeMs)))
    const missing = tools.filter((_, index) => {
      const check = checks[index]!
      return check.status === "rejected" && check.reason instanceof CaptureError && check.reason.code === "missing-dependency"
    })
    if (missing.length > 0) {
      throw new CaptureError(
        "missing-dependency",
        `Missing required command${missing.length > 1 ? "s" : ""}: ${missing.join(", ")}.\n${missing.map((tool) => INSTALL_HINTS[tool]).join("\n")}`,
        phase,
      )
    }
    const failed = checks.find((check) => check.status === "rejected")
    if (failed) throw failed.reason
    for (const [index, tool] of tools.entries()) {
      const check = checks[index]!
      if (check.status === "fulfilled" && check.value.exitCode !== 0) {
        const detail = tail(check.value.stderr || check.value.stdout)
        throw new CaptureError(
          "missing-dependency",
          `\`${commands[tool]} ${VERSION_ARGS[tool].join(" ")}\` failed (exit ${check.value.exitCode ?? check.value.signal}), so ${tool} is not usable. ` +
            `Run that command to see the problem, then reinstall or fix it. ${INSTALL_HINTS[tool]}${detail ? `\n${detail}` : ""}`,
          phase,
        )
      }
    }

    await enter("selecting-device")
    const listing = await run("adb", ["devices"], timeouts.probeMs)
    if (listing.exitCode !== 0) {
      throw new CaptureError("no-device", `\`adb devices\` failed.\n${tail(listing.stderr)}`, phase)
    }
    const serial = selectDevice(parseAdbDevices(listing.stdout), requestedSerial)

    await enter("checking-android")
    const sdkResult = await run("adb", ["-s", serial, "shell", "getprop", "ro.build.version.sdk"], timeouts.probeMs)
    const sdk = Number.parseInt(sdkResult.stdout.trim(), 10)
    if (sdkResult.exitCode !== 0 || Number.isNaN(sdk)) {
      throw new CaptureError(
        "device-unavailable",
        `Could not read the Android version of ${serial}.\n${tail(sdkResult.stderr)}`,
        phase,
      )
    }
    if (sdk < MIN_SDK) {
      throw new CaptureError(
        "unsupported-android",
        `Device ${serial} runs Android API ${sdk}. Camera capture through scrcpy needs Android 12 (API ${MIN_SDK}) or newer.`,
        phase,
      )
    }

    await enter("recording")
    workDir = await mkdtemp(join(resolve(runtime.tempRoot), "opencode-android-camera-"))
    const recordPath = join(workDir, "recording.mp4")
    const photoPath = join(workDir, "photo.jpg")
    const recording = await run(
      "scrcpy",
      scrcpyArgs(serial, camera, recordPath, runtime.recordSeconds),
      runtime.recordSeconds * 1_000 + timeouts.recordGraceMs,
    )
    if (recording.exitCode !== 0 || !(await nonEmptyFile(recordPath))) {
      throw new CaptureError(
        "recording-failed",
        `scrcpy could not record the ${camera} camera of ${serial} (exit ${recording.exitCode ?? recording.signal}). ` +
          `Check that the device has a ${camera} camera (\`scrcpy --serial=${serial} --list-cameras\`) and that scrcpy is 2.5 or newer.` +
          `\n${tail(recording.stderr)}`,
        phase,
      )
    }

    await enter("extracting")
    const extraction = await run("ffmpeg", ffmpegArgs(recordPath, photoPath), timeouts.extractMs)
    if (extraction.exitCode !== 0 || !(await nonEmptyFile(photoPath))) {
      throw new CaptureError("extraction-failed", `ffmpeg could not extract a JPEG frame.\n${tail(extraction.stderr)}`, phase)
    }
    await rm(recordPath, { force: true })

    await enter("complete")
    return photoPath
  } catch (error) {
    if (workDir) await rm(workDir, { recursive: true, force: true })
    throw error
  }
}
