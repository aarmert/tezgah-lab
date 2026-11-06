import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { defaultRuntime, type CaptureRuntime } from "../src/capture.ts"

export const realFfmpeg = Bun.which("ffmpeg")

export type FakeDevice = { serial: string; state: string }

export type FakeSetup = {
  devices?: FakeDevice[]
  sdk?: string
  scrcpy?: "synthetic" | "fail" | "hang" | "missing" | "broken-version"
}

export type Fakes = {
  runtime: CaptureRuntime
  tempRoot: string
  scrcpyLog: string
  childPidFile: string
  cleanup: () => Promise<void>
}

const quote = (value: string) => `'${value.replaceAll("'", `'\\''`)}'`

async function script(path: string, body: string) {
  await writeFile(path, `#!/bin/sh\n${body}\n`)
  await chmod(path, 0o755)
}

export async function makeFakes(setup: FakeSetup = {}): Promise<Fakes> {
  const { devices = [{ serial: "FAKE123", state: "device" }], sdk = "34", scrcpy = "synthetic" } = setup
  const bin = await mkdtemp(join(tmpdir(), "android-camera-test-bin-"))
  const tempRoot = await mkdtemp(join(tmpdir(), "android-camera-test-tmp-"))
  const scrcpyLog = join(bin, "scrcpy.args")
  const childPidFile = join(bin, "scrcpy.child")
  const deviceLines = devices.map((device) => quote(`${device.serial}\t${device.state}`)).join(" ")

  await script(
    join(bin, "adb"),
    `case "$1" in
  version) echo "Android Debug Bridge version 1.0.41" ;;
  devices) printf '%s\\n' 'List of devices attached' ${deviceLines} ;;
  -s) [ "$3 $4 $5" = "shell getprop ro.build.version.sdk" ] && echo ${quote(sdk)} ;;
  *) exit 1 ;;
esac`,
  )

  const record = `for arg in "$@"; do
  printf '%s\\n' "$arg" >> ${quote(scrcpyLog)}
  case "$arg" in --record=*) out="\${arg#--record=}" ;; esac
done`

  const scrcpyBodies = {
    synthetic: `${record}
exec ${quote(realFfmpeg ?? "ffmpeg")} -nostdin -loglevel error -f lavfi \\
  -i "color=c=black:s=320x240:r=30:d=2,drawbox=c=red:t=fill:enable='gte(t,1)'" \\
  -c:v libx264 -pix_fmt yuv420p "$out"`,
    fail: `${record}
echo "ERROR: Could not find any camera" >&2
exit 1`,
    "broken-version": `${record}`,
    hang: `${record}
trap '' TERM
sleep 60 &
echo $! > ${quote(childPidFile)}
wait`,
  }

  if (scrcpy !== "missing") {
    await script(
      join(bin, "scrcpy"),
      `[ "$1" = "--version" ] && { ${scrcpy === "broken-version" ? `echo "dyld: Library not loaded: libavformat" >&2; exit 134` : `echo "scrcpy 3.3.4"; exit 0`}; }
${scrcpyBodies[scrcpy]}`,
    )
  }

  const base = defaultRuntime()
  return {
    runtime: {
      ...base,
      commands: { adb: join(bin, "adb"), scrcpy: join(bin, "scrcpy"), ffmpeg: realFfmpeg ?? "ffmpeg" },
      tempRoot,
      timeouts: { ...base.timeouts, recordGraceMs: 3_000, killGraceMs: 300 },
    },
    tempRoot,
    scrcpyLog,
    childPidFile,
    cleanup: async () => {
      await rm(bin, { recursive: true, force: true })
      await rm(tempRoot, { recursive: true, force: true })
    },
  }
}
