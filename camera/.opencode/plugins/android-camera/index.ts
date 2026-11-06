import { Plugin } from "@opencode/plugin"
import { CAMERAS, capturePhoto, parseCaptureOptions } from "../../../src/capture.ts"

export default Plugin.define({
  id: "android-camera",
  async setup(ctx) {
    await ctx.tool.transform((editor) => {
      editor.add({
        name: "android_take_photo",
        description:
          "Take a photo with an attached Android device's camera (Android 12+, via scrcpy and adb). " +
          "The JPEG is a frame from a short camera video, not a full-resolution still. " +
          "Returns the absolute path of the JPEG in the local temp directory. " +
          "Pass `serial` when more than one adb device is attached.",
        input: {
          type: "object",
          properties: {
            serial: { type: "string", description: "adb serial of the device, as listed by `adb devices`." },
            camera: { type: "string", enum: [...CAMERAS], default: "back", description: "Camera facing to use." },
          },
          additionalProperties: false,
        },
        execute: async (input, context) => {
          const path = await capturePhoto(parseCaptureOptions(input), {
            signal: context.signal,
            onPhase: (phase) => context.progress({ status: phase }),
          })
          return { content: path }
        },
      })
    })
  },
})
