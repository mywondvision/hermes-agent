import type { ResolvedOwner } from '@/hermes'
import { fetchVoiceClientConfigFor } from '@/lib/voice-client-direct'
import { type DictationStreamSession, openDictationStream } from '@/lib/voice-stream'

/** One PCM tap and its optional configured streaming recognizer, from mic-open until release. */
export function recordingStream(owner: ResolvedOwner, onPartial?: (text: string) => void) {
  let active: DictationStreamSession | null = null
  let opening: Promise<DictationStreamSession | null> | undefined
  let closed = false
  let ready = false
  let buffered: ArrayBuffer[] = []

  const open = (sampleRate: number) => {
    opening ??= (async () => {
      const config = await fetchVoiceClientConfigFor(owner)
      if (closed || !config?.stt.streaming) { return null }

      const socket = await openDictationStream(owner, sampleRate, onPartial)
      if (closed) {
        socket?.cancel()
        return null
      }

      active = socket
      for (const chunk of buffered) { socket?.pushAudio(chunk) }

      return socket
    })().catch(() => null).finally(() => {
      ready = true
      buffered = []
    })
  }

  return {
    open,
    push(chunk: ArrayBuffer) {
      if (closed) { return }
      if (active) { active.pushAudio(chunk) }
      else if (!ready && buffered.length < 64) { buffered.push(chunk) }
    },
    session: () => opening ?? Promise.resolve(null),
    cancel() {
      closed = true
      buffered = []
      active?.cancel()
    }
  }
}
