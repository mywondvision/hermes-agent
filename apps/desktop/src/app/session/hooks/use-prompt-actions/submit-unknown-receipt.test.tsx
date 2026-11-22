import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import { createClientSessionState } from '@/lib/chat-runtime'
import { readPendingSubmissions } from '@/store/pending-submissions'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $sessions } from '@/store/session'
import { $sessionStates } from '@/store/session-states'

import { ENTRY_PREFIX } from './prepared-submissions'
import { storedJournal } from './prepared-submissions-test-utils'
import { useSubmitPrompt } from './submit'
import type { GatewayRequest } from './utils'

vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  retainGatewayForSessionTurn: vi.fn(async () => () => undefined)
}))

beforeEach(() => {
  window.localStorage.clear()
  $sessions.set([])
  $sessionStates.set({})
  $connection.set(null)
  $activeGatewayProfile.set('default')
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

type ClientSessionState = ReturnType<typeof createClientSessionState>

function probe(route: 'forward' | 'ordinary' = 'forward') {
  return { route, version: 1, mode: 'shared', ref: { session_id: 'stored-a', profile_id: 'profile-1' }, generation: 'owner-boot' }
}

function setup(requestGateway: GatewayRequest) {
  let state: ClientSessionState = createClientSessionState()
  const deps = {
    activeSessionIdRef: { current: 'runtime-a' as string | null },
    selectedStoredSessionIdRef: { current: 'stored-a' as string | null },
    busyRef: { current: false },
    copy: en.desktop,
    createBackendSessionForSend: vi.fn(async () => null),
    getRoutedStoredSessionId: () => null,
    getRuntimeIdForStoredSession: () => 'runtime-a',
    getRouteToken: () => '/stored-a::',
    requestGateway: requestGateway as GatewayRequest,
    runtimeIdByStoredSessionIdRef: { current: new Map([['stored-a', 'runtime-a']]) },
    resumeStoredSession: vi.fn(),
    syncAttachmentsForSubmit: vi.fn(async (sessionId: string) => ({ sessionId, attachments: [] })),
    updateSessionState: vi.fn((_sid: string, updater: (s: ClientSessionState) => ClientSessionState) => {
      state = updater(state)

      return state
    })
  }

  return { deps, state: () => state }
}

// Adolanium reg-4: the owner answers an exact retry with the EXISTING admission, which may be
// `unknown` (claimed, then the owner died mid-turn). That send was admitted; dropping it as a
// rejection hid the user's message and the retry identity, and invited a duplicate resend.
it('an exact retry answered `unknown` keeps the bubble, retires the retry identity and never replays', async () => {
  const requestGateway = vi.fn(async (_method: string, params?: Record<string, unknown>) => {
    if (requestGateway.mock.calls.length === 1) {
      throw new Error('connection closed')
    }

    return { admission_id: params?.submission_id, session_id: 'runtime-a', status: 'unknown' } as never
  })

  const { deps, state } = setup(requestGateway as GatewayRequest)
  const { result } = renderHook(() => useSubmitPrompt(deps as never))
  await act(async () => {
    expect(await result.current('lost turn', { submission_id: 'lost-1' })).toBe(false)
  })
  await act(async () => {
    expect(await result.current('lost turn', { submission_id: 'lost-1' })).toBe(true)
  })

  expect(requestGateway.mock.calls.filter(([method]) => method === 'prompt.submit')).toHaveLength(2)
  expect(state().messages.filter(message => message.role === 'user')).toHaveLength(1)
  expect(state().busy).toBe(false)
  expect(readPendingSubmissions('stored-a')['lost-1']).toMatchObject({ status: 'unknown', text: 'lost turn' })
  expect(storedJournal()).toEqual({})
})

it('shows the server admission for an SSH prompt only in its original conversation', async () => {
  $connection.set({ mode: 'remote', remoteKind: 'ssh', connectionId: 'machine' } as never)
  const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'prompt.forward_probe') return probe() as never
    if (method !== 'prompt.forward_submit') throw new Error(method)
    return { submission_id: params?.submission_id, admission_id: 'server-id',
      ref: { session_id: 'stored-a', profile_id: 'profile-1' }, status: 'queued' } as never
  })
  const { deps, state } = setup(request as GatewayRequest)
  const hook = renderHook(() => useSubmitPrompt(deps as never))
  await act(async () => { expect(await hook.result.current('start task', { submission_id: 'client-id' })).toBe(true) })
  expect(request.mock.calls.map(([method]) => method)).toEqual(['prompt.forward_probe', 'prompt.forward_submit'])
  expect(deps.updateSessionState).toHaveBeenCalledWith('runtime-a', expect.any(Function), 'stored-a')
  expect(state().messages).toContainEqual(expect.objectContaining({
    id: 'hermes-admission-client-id', role: 'system',
    parts: [expect.objectContaining({ text: expect.stringContaining('server-id') })]
  }))
  expect(storedJournal()).toEqual({})
})

it.each(['timeout', 'wrong session', 'missing profile', 'wrong profile', 'copied input ID'])('retains the original after %s', async first => {
  $connection.set({ mode: 'remote', remoteKind: 'ssh', connectionId: 'machine' } as never)
  const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'prompt.forward_probe') return probe() as never
    if (method === 'prompt.forward_submit' && first !== 'timeout') return {
      ref: { session_id: first === 'wrong session' ? 'other' : 'stored-a',
        profile_id: first === 'missing profile' ? '' : first === 'wrong profile' ? 'foreign' : 'profile-1' },
      submission_id: params?.submission_id, admission_id: first === 'copied input ID' ? params?.submission_id : 'unverified', status: 'queued'
    } as never
    throw new Error(method === 'prompt.receipt' ? 'permission_denied' : 'Request timed out')
  })
  const { deps, state } = setup(request as GatewayRequest)
  const options = { submission_id: 'client-id' }
  let hook = renderHook(() => useSubmitPrompt(deps as never))
  await act(async () => { expect(await hook.result.current('original task', options)).toBe(false) })
  expect(state().messages.some(message => message.error?.includes('UNKNOWN'))).toBe(true)
  hook.unmount()
  hook = renderHook(() => useSubmitPrompt(deps as never))
  await act(async () => { expect(await hook.result.current('edited task', options)).toBe(false) })
  await act(async () => {
    expect(await hook.result.current('original task', { ...options, restoredSubmission: true })).toBe(false)
  })
  expect(request.mock.calls.map(([method]) => method)).toEqual([
    'prompt.forward_probe', 'prompt.forward_submit'
  ])
  expect(deps.resumeStoredSession).not.toHaveBeenCalled()
  expect(Object.values(storedJournal())).toMatchObject([{ id: 'client-id', text: 'original task' }])
})

it('settles the standard SSH voice stop without a ledger lookup or a reported admission', async () => {
  $connection.set({ mode: 'remote', remoteKind: 'ssh', connectionId: 'machine' } as never)
  const request = vi.fn(async (method: string) => (method === 'prompt.forward_probe'
    ? probe('ordinary') : { voice_stopped: true }) as never)
  const { deps, state } = setup(request as GatewayRequest)
  const hook = renderHook(() => useSubmitPrompt(deps as never))
  await act(async () => { expect(await hook.result.current('stop voice chat')).toBe(true) })
  expect(request.mock.calls.map(([method]) => method)).toEqual(['prompt.forward_probe', 'prompt.submit'])
  expect(state().messages).toHaveLength(0)
  expect(storedJournal()).toEqual({})
})

it('holds an unclassified destination until the connection is identified', async () => {
  $connection.set({ mode: 'remote', connectionId: 'machine' } as never)
  const request = vi.fn(async () => ({ admission_id: 'client-id', submission_id: 'client-id' }) as never)
  const { deps } = setup(request as GatewayRequest)
  const hook = renderHook(() => useSubmitPrompt(deps as never))
  await act(async () => { expect(await hook.result.current('unclassified route', { submission_id: 'client-id' })).toBe(false) })
  expect(request).not.toHaveBeenCalled()
})

it('rejects a readback from another canonical profile even with matching input and conversation', async () => {
  $connection.set({ mode: 'remote', remoteKind: 'ssh', connectionId: 'machine' } as never)
  const request = vi.fn(async (method: string) => {
    if (method === 'prompt.forward_probe') return { ...probe(), ref: { session_id: 'stored-a', profile_id: 'owner-profile' } } as never
    if (method === 'prompt.forward_submit') throw new Error('timeout')
    return { submission_id: 'client-id', admission_id: 'other-profile-id', status: 'terminal',
      ref: { session_id: 'runtime-a', profile_id: 'another-profile' } } as never
  })
  const { deps, state } = setup(request as GatewayRequest)
  const hook = renderHook(() => useSubmitPrompt(deps as never))
  await act(async () => { expect(await hook.result.current('task', { submission_id: 'client-id' })).toBe(false) })
  const [[key, entry]] = Object.entries(storedJournal())
  delete entry.forwardSessionId
  window.localStorage.setItem(ENTRY_PREFIX + key, JSON.stringify(entry))
  request.mockClear()
  request.mockImplementation(async (method: string) => (method === 'runtime.describe'
    ? { profile_id: 'owner-profile' }
    : { ref: { session_id: 'runtime-a', profile_id: 'another-profile' }, submission_id: 'client-id',
      admission_id: 'foreign', status: 'terminal' }) as never)
  await act(async () => {
    expect(await hook.result.current('task', { submission_id: 'client-id', restoredSubmission: true })).toBe(false)
  })
  expect(request.mock.calls.map(([method]) => method)).toEqual(['runtime.describe', 'prompt.receipt'])
  expect(state().messages.some(message => message.error?.includes('UNKNOWN'))).toBe(true)
  expect(state().messages.some(message => message.id === 'hermes-admission-client-id')).toBe(false)
  expect(Object.values(storedJournal())).toMatchObject([{ id: 'client-id', profileId: 'owner-profile' }])
})

it('holds a legacy server without a route probe before either text write', async () => {
  $connection.set({ mode: 'remote', remoteKind: 'ssh', connectionId: 'machine' } as never)
  const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'prompt.forward_probe') throw Object.assign(Error('Method not found'), { code: -32601 })
    if (params?.submission_id) throw Object.assign(Error('Unknown parameter submission_id'), { code: 4000 })
    throw Error('connection closed')
  })
  const { deps } = setup(request as GatewayRequest)
  const options = { submission_id: 'old-server-input' }
  const hook = renderHook(() => useSubmitPrompt(deps as never))
  await act(async () => { expect(await hook.result.current('legacy input', options)).toBe(false) })
  await act(async () => { expect(await hook.result.current('legacy input', options)).toBe(false) })
  expect(request.mock.calls.map(([method]) => method)).toEqual(['prompt.forward_probe', 'prompt.forward_probe'])
  expect(Object.values(storedJournal())).toEqual([])
})

it.each(['old entry', 'no entry'])('holds an SSH draft with %s and no original profile before reading any receipt', async kind => {
  $connection.set({ mode: 'remote', remoteKind: 'ssh', connectionId: 'machine' } as never)
  const request = vi.fn(async (method: string) => {
    if (method === 'prompt.forward_probe') return probe() as never
    throw Error('offline')
  })
  const { deps } = setup(request as GatewayRequest)
  let hook = renderHook(() => useSubmitPrompt(deps as never))
  const id = 'old-input'
  if (kind === 'old entry') {
    await act(async () => { expect(await hook.result.current('original words', { submission_id: id })).toBe(false) })
    const [[key, entry]] = Object.entries(storedJournal())
    delete entry.forwardSessionId
    delete entry.profileId
    window.localStorage.setItem(ENTRY_PREFIX + key, JSON.stringify(entry))
  }
  hook.unmount()
  request.mockClear()
  request.mockImplementation(async (method: string) => (method === 'runtime.describe'
    ? { profile_id: 'current-profile' }
    : { submission_id: id, admission_id: 'different-home', ref: { session_id: 'runtime-a', profile_id: 'current-profile' },
        status: 'queued' }) as never)
  hook = renderHook(() => useSubmitPrompt(deps as never))
  await act(async () => {
    expect(await hook.result.current('original words', { submission_id: id, restoredSubmission: true })).toBe(false)
  })
  expect(request).not.toHaveBeenCalled()
  if (kind === 'old entry') expect(Object.values(storedJournal())).toMatchObject([{ id }])
})
