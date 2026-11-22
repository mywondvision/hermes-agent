import { afterEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/hermes'

import { type VoiceLiveHandlers, VoiceLiveSession } from './voice-live'
import { $liveVoiceOutput } from '@/store/voice-playback'

// A GPT-Live session is created on the chat OWNER's (connection, profile) —
// the Bot that owns the chat — never on the window's active scope, mirroring
// the TTS legs (#117014): two Bots with different voices configured must get
// their own voices. The status probe follows the same rule so a Bot tile
// mounts the engine the Bot's own profile selected.

const handlers: VoiceLiveHandlers = {
  onClosed: () => undefined,
  onDelegation: () => undefined,
  onError: () => undefined
}

function installApi() {
  const api = vi.fn(async (_request: unknown) => ({
    ok: true,
    session: { id: 's1' },
    transport: { sdp: 'answer', type: 'answer' }
  }))

  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })

  return api
}

/** Enough of RTCPeerConnection/getUserMedia for `start()` to reach the POST. */
function installWebRTC() {
  const channel = {
    addEventListener: () => undefined,
    readyState: 'open' as const,
    send: () => undefined,
    close: () => undefined
  }
  const events = new EventTarget()
  let state = 'new'

  const PeerConnection = class {
    addEventListener = events.addEventListener.bind(events)
    get connectionState() {
      return state
    }
    close = () => undefined
    createDataChannel = () => channel
    createOffer = async () => ({})
    iceGatheringState = 'complete'
    localDescription = { sdp: 'v=0\r\n' }
    setLocalDescription = async () => undefined
    setRemoteDescription = async () => undefined
  }

  Object.defineProperty(globalThis, 'RTCPeerConnection', { configurable: true, value: PeerConnection })
  Object.defineProperty(window.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: async () => ({ getAudioTracks: () => [], getTracks: () => [] }) }
  })
  return {
    attach: () => events.dispatchEvent(Object.assign(new Event('track'), { track: { kind: 'audio' } })),
    disconnect: () => {
      state = 'disconnected'
      events.dispatchEvent(new Event('connectionstatechange'))
    }
  }
}

afterEach(() => {
  setApiRequestConnection(null)
  setApiRequestProfile(null)
  Reflect.deleteProperty(window, 'hermesDesktop')
  Reflect.deleteProperty(globalThis, 'RTCPeerConnection')
  Reflect.deleteProperty(window.navigator, 'mediaDevices')
})

describe('GPT-Live owner routing', () => {
  it('holds spoken plan decisions for the lifetime of its synthetic output track', async () => {
    const rtc = installWebRTC()
    installApi()
    vi.stubGlobal('MediaStream', class {})
    const session = new VoiceLiveSession(handlers)
    vi.spyOn(session.audio, 'play').mockResolvedValue(undefined)
    vi.spyOn(session.audio, 'pause').mockImplementation(() => undefined)
    try {
      await session.start([])
      rtc.attach()
      expect($liveVoiceOutput.get()).toBe(true)
      rtc.disconnect()
      expect($liveVoiceOutput.get()).toBe(false)
    } finally {
      rtc.disconnect()
      vi.unstubAllGlobals()
    }
  })
  it('creates the session on the chat owner (connection, profile) ahead of the active scope', async () => {
    installWebRTC()
    const api = installApi()
    setApiRequestConnection('gw-active')
    setApiRequestProfile('research')

    const session = new VoiceLiveSession(handlers, { connectionId: 'gw-bots', profile: 'bot-adam' })
    await session.start([])

    expect(api).toHaveBeenCalledTimes(1)
    const request = api.mock.calls[0][0] as { connectionId?: string; path: string; priority?: string; profile?: string }

    expect(request.path).toBe('/api/audio/voice-live/session')
    expect(request.profile).toBe('bot-adam')
    expect(request.connectionId).toBe('gw-bots')
    expect(request.priority).toBe('foreground')

    // Ownerless chats keep the active scope verbatim (the old bare profileScoped()).
    const ownerless = new VoiceLiveSession(handlers, null)
    await ownerless.start([])
    const plain = api.mock.calls[1][0] as { connectionId?: string; path: string; priority?: string; profile?: string }
    expect(plain.path).toBe('/api/audio/voice-live/session')
    expect(plain.profile).toBe('research')
    expect(plain.connectionId).toBe('gw-active')
    expect(plain.priority).toBeUndefined()
  })
})
