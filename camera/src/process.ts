import { spawn } from "node:child_process"

export type RunResult = {
  exitCode: number | null
  signal: NodeJS.Signals | null
  stdout: string
  stderr: string
}

export type RunOptions = {
  timeoutMs: number
  signal?: AbortSignal
  killGraceMs?: number
}

export class CommandNotFoundError extends Error {
  constructor(readonly command: string) {
    super(`Command not found: ${command}`)
  }
}

export class CommandTimeoutError extends Error {
  constructor(
    readonly command: string,
    readonly timeoutMs: number,
    readonly stderr: string,
  ) {
    super(`${command} did not finish within ${timeoutMs} ms and was terminated`)
  }
}

export class CommandAbortedError extends Error {
  constructor(readonly command: string) {
    super(`${command} was cancelled and terminated`)
  }
}

const OUTPUT_LIMIT = 64 * 1024

// Waits for the direct child and its pipes to close. Timeout and abort also signal its process
// group because scrcpy launches adb children; this does not await descendant or device-side exit.
export function runCommand(command: string, args: readonly string[], options: RunOptions): Promise<RunResult> {
  const { timeoutMs, signal, killGraceMs = 2_000 } = options
  if (signal?.aborted) return Promise.reject(new CommandAbortedError(command))

  return new Promise((resolve, reject) => {
    const ownGroup = process.platform !== "win32"
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"], detached: ownGroup })
    let stdout = ""
    let stderr = ""
    let stopReason: "timeout" | "abort" | undefined
    let forceTimer: ReturnType<typeof setTimeout> | undefined

    child.stdout.setEncoding("utf8").on("data", (chunk: string) => (stdout = (stdout + chunk).slice(-OUTPUT_LIMIT)))
    child.stderr.setEncoding("utf8").on("data", (chunk: string) => (stderr = (stderr + chunk).slice(-OUTPUT_LIMIT)))

    const sendSignal = (sig: NodeJS.Signals) => {
      if (child.pid === undefined) return
      try {
        if (ownGroup) process.kill(-child.pid, sig)
        else child.kill(sig)
      } catch {}
    }

    const stop = (reason: "timeout" | "abort") => {
      if (stopReason) return
      stopReason = reason
      sendSignal("SIGTERM")
      forceTimer = setTimeout(() => sendSignal("SIGKILL"), killGraceMs)
    }

    const timeoutTimer = setTimeout(() => stop("timeout"), timeoutMs)
    const onAbort = () => stop("abort")
    signal?.addEventListener("abort", onAbort, { once: true })

    const finish = () => {
      clearTimeout(timeoutTimer)
      clearTimeout(forceTimer)
      signal?.removeEventListener("abort", onAbort)
    }

    child.once("error", (error: NodeJS.ErrnoException) => {
      finish()
      reject(error.code === "ENOENT" ? new CommandNotFoundError(command) : error)
    })

    child.once("close", (exitCode, exitSignal) => {
      if (stopReason) sendSignal("SIGKILL")
      finish()
      if (stopReason === "abort") return reject(new CommandAbortedError(command))
      if (stopReason === "timeout") return reject(new CommandTimeoutError(command, timeoutMs, stderr))
      resolve({ exitCode, signal: exitSignal, stdout, stderr })
    })
  })
}
