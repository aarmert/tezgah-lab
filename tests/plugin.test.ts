import { expect, test } from "bun:test"
import plugin from "../.opencode/plugins/android-camera/index.ts"

test("registers android_take_photo with an optional serial and a camera enum", async () => {
  const added: any[] = []
  const ctx = { tool: { transform: async (callback: (editor: unknown) => void) => callback({ add: (tool: unknown) => added.push(tool) }) } }
  await plugin.setup(ctx as never)

  expect(added).toHaveLength(1)
  const [tool] = added
  expect(tool.name).toBe("android_take_photo")
  expect(tool.input.required).toBeUndefined()
  expect(tool.input.properties.camera.enum).toEqual(["back", "front", "external"])

  const context = { signal: new AbortController().signal, progress: async () => {} }
  await expect(tool.execute({ camera: "rear" }, context)).rejects.toThrow(/back, front, external/)
})
