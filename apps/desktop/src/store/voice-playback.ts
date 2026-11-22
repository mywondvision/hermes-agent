import { atom } from 'nanostores'

export type VoicePlaybackSource = 'read-aloud' | 'voice-conversation'
export type VoicePlaybackStatus = 'idle' | 'preparing' | 'speaking'

export interface VoicePlaybackState {
  audioElement: HTMLAudioElement | null
  messageId: string | null
  sequence: number
  source: VoicePlaybackSource | null
  status: VoicePlaybackStatus
}

export const $voicePlayback = atom<VoicePlaybackState>({
  audioElement: null,
  messageId: null,
  sequence: 0,
  source: null,
  status: 'idle'
})

export function setVoicePlaybackState(next: VoicePlaybackState) {
  $voicePlayback.set(next)
}

// A remote audio track may speak at any time while attached, including during local dictation.
const liveOutputs = new Set<HTMLAudioElement>()
export const $liveVoiceOutput = atom(false)

export function setLiveVoiceOutput(element: HTMLAudioElement, attached: boolean) {
  if (attached) {
    liveOutputs.add(element)
  } else {
    liveOutputs.delete(element)
  }
  $liveVoiceOutput.set(liveOutputs.size > 0)
}
