import { HermesGateway } from '@/api/client'
import { probedRoute, type ForwardRoute } from '@/api/forward-probe'
import { buildGatewayWsUrl } from '../../../../../electron/connection-config'
import { taskWebSocket } from '../../../../../e2e/support/symphony-task-socket'
import { atom, type WritableAtom } from 'nanostores'
import { useStore } from '@nanostores/react'
import { Thread } from '@/components/assistant-ui/thread'
import type { ChatMessage } from '@/lib/chat-messages'
import { toRuntimeMessage } from '@/lib/chat-runtime'
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react'
import { act, fireEvent, isInaccessible, render, waitFor, within } from '@testing-library/react'
import { execFileSync } from 'node:child_process'
import { createHash, generateKeyPairSync, randomUUID } from 'node:crypto'
import { EventEmitter } from 'node:events'
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { MemoryRouter, useLocation } from 'react-router'
import { routeSessionId, sessionRoute } from '@/app/routes'
import { expect, it, vi } from 'vitest'
import { I18nProvider } from '@/i18n'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $connection, $gatewayState } from '@/store/session'
import { $busyInputConfig, busyInputOwnerKey } from '@/store/busy-input-mode'
import { clearQueuedPrompts, getQueuedPrompts } from '@/store/composer-queue'
import { $activeGatewayProfile } from '@/store/profile'
import { $notifications, dismissNotification } from '@/store/notifications'
import { recordSessionEventScope } from '@/store/session-states'
import { Harness, type HarnessHandle } from '@/app/session/hooks/use-prompt-actions/index-test-utils'
import type { GatewayRequest } from '@/app/session/hooks/use-prompt-actions/utils'
import { storedJournal } from '@/app/session/hooks/use-prompt-actions/prepared-submissions-test-utils'
import { symphonyPlanBridge } from '../../../../../electron/symphony-plan-bridge'
import { registerSymphonyRoute } from '../../../../../electron/register-symphony-route'
import { registerSymphonyOperator } from '../../../../../electron/register-symphony-operator'
import { symphonyOperatorBridge } from '../../../../../electron/symphony-operator-bridge'
import { openOperatorChannel } from '../../../../../electron/symphony-operator-channel'
import { operatorPrivateKey, SymphonyOperatorJournal, type OperatorAddress, type OperatorBinding, type OperatorInstallation } from '../../../../../electron/symphony-operator-journal'
import type { OperatorReply } from '../../../../../electron/symphony-operator-types'
import { exchangeOperator, operatorPublicKey } from '../../../../../electron/symphony-operator-wire'
import { RoutePeer } from '../../../../../e2e/support/symphony-route-peer'
import { readTaskFailure, taskLaunchAccepted, taskWorkerSettlement, type TaskDiagnosticCounts } from '../../../../../e2e/support/symphony-task-diagnostics'
import { waitForReadyPlan } from '../../../../../e2e/support/forward-plan-display'
import { ChatBar } from '..'
import { RICH_INPUT_SLOT } from '../rich-editor'
import type { ChatBarProps } from '../types'

const ipc = vi.hoisted(() => ({
  handle: vi.fn(), window: vi.fn(), routed: vi.fn(), userData: vi.fn(),
  storage: {
    isEncryptionAvailable: () => true,
    encryptString: (text: string) => Buffer.from(text).reverse(),
    decryptString: (bytes: Buffer) => Buffer.from(bytes).reverse().toString('utf8')
  }
}))
vi.mock('electron', () => ({
  ipcMain: { handle: ipc.handle }, BrowserWindow: { fromWebContents: ipc.window },
  app: { getPath: ipc.userData }, safeStorage: ipc.storage,
  clipboard: { writeText: vi.fn() }, dialog: { showMessageBox: vi.fn() }
}))
vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  requestGatewayForAgent: ipc.routed,
  retainGatewayForSessionTurn: vi.fn(async () => () => undefined)
}))

const wait = { timeout: 25000 }
function Field({
  chat,
  submit,
  transcript,
  busy
}: {
  chat: string
  submit: ChatBarProps['onSubmit']
  transcript: WritableAtom<ChatMessage[]>
  busy: boolean
}) {
  const messages = useStore(transcript)
  const runtime = useExternalStoreRuntime({
    convertMessage: toRuntimeMessage,
    isRunning: false,
    messages,
    onNew: async () => {}
  })
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <I18nProvider configClient={null} initialLocale="en">
        <Thread sessionId={chat} sessionKey={chat} />
        <ChatBar
          busy={busy}
          disabled={false}
          gateway={null}
          sessionId={chat}
          queueSessionKey={chat}
          onCancel={() => {
            throw new Error('Unexpected interrupt')
          }}
          onSubmit={submit}
          state={{
            model: { canSwitch: false, model: '', provider: '' },
            tools: { enabled: false, label: '' },
            voice: { enabled: false, active: false }
          }}
        />
      </I18nProvider>
    </AssistantRuntimeProvider>
  )
}

async function enter(container: HTMLElement, text?: string) {
  const editor = container.querySelector<HTMLElement>(`[data-slot="${RICH_INPUT_SLOT}"]`)!
  if (text !== undefined) {
    await act(async () => {
      editor.focus()
      editor.textContent = text
      fireEvent.input(editor)
      fireEvent.keyUp(editor, { key: text.at(-1) })
    })
  }
  await act(async () => {
    fireEvent.keyDown(editor, { key: 'Enter', code: 'Enter', keyCode: 13 })
  })
}

function openChat(chat: string, request: GatewayRequest, pathname = sessionRoute(chat), busy = false, runtime = chat) {
  const transcript = atom<ChatMessage[]>([])
  let actions: HarnessHandle
  function Conversation() {
    const location = useLocation()
    return (
      <>
        <Harness
          activeSessionId={runtime}
          storedSessionId={chat}
          getRouteToken={() => [location.pathname, location.search, location.hash].join(':')}
          getRoutedStoredSessionId={() => routeSessionId(location.pathname)}
          getRuntimeIdForStoredSession={() => runtime}
          onReady={handle => {
            actions = handle
          }}
          refreshSessions={async () => {}}
          requestGateway={request}
          onSeedState={state => transcript.set(state.messages as ChatMessage[])}
          rawAdmissionReceipts
        />
        <Field busy={busy} transcript={transcript} chat={chat} submit={(text, options) => actions.submitTextRaw(text, options)} />
      </>
    )
  }
  const root = render(<MemoryRouter initialEntries={[pathname]}><Conversation /></MemoryRouter>)
  return {
    ...root,
    enter: (text?: string) => enter(root.container, text),
    messages: () => actions.state().messages,
    editorText: () => root.container.querySelector(`[data-slot="${RICH_INPUT_SLOT}"]`)?.textContent
  }
}

async function clientWithFixture(respond: (method: string, params: Record<string, unknown>) => unknown,
  afterReply?: (method: string) => void) {
  const writes: { method: string; params: Record<string, unknown> }[] = []
  const dialUrl = buildGatewayWsUrl('http://127.0.0.1', 'synthetic-ssh-token')
  const Wire = taskWebSocket(async (url, protocols, receive) => {
      expect(url).toBe(dialUrl)
      expect(protocols).toEqual([])
      return {
        close: async () => {},
        send: async raw => {
          const frame = JSON.parse(raw)
          if (!frame.method) return
          const params = frame.params ?? {}
          writes.push({ method: frame.method, params })
          try {
            const result = await respond(frame.method, params)
            receive(JSON.stringify({ jsonrpc: '2.0', id: frame.id, result }))
            afterReply?.(frame.method)
          } catch (error) {
            receive(JSON.stringify({ jsonrpc: '2.0', id: frame.id, error }))
          }
        }
      }
    })
  let wire!: InstanceType<typeof Wire>
  vi.stubGlobal('WebSocket', class extends Wire {
    constructor(url: string, protocols?: string | string[]) { super(url, protocols); wire = this }
  })
  const gateway = new HermesGateway()
  await gateway.connect(dialUrl)
  return {
    writes,
    beginClosing: () => { wire.readyState = 2 },
    request: gateway.request.bind(gateway),
    async reconnect() {
      gateway.close()
      await gateway.connect(dialUrl)
    },
    close() {
      gateway.close()
      vi.unstubAllGlobals()
    }
  }
}

function routeProbe(chat: string, route: 'forward' | 'ordinary' = 'forward') {
  return { version: 1, route, generation: 'fixture-owner', mode: 'shared', ref: { session_id: chat, profile_id: 'offline-profile' } }
}

it('submits and reads personal input, status and STOP only through the native bridge', async () => {
  const chat = 'operator-personal-chat'
  const prior = $connection.get()
  const desktop = window.hermesDesktop
  const notices = new Set($notifications.get().map(notice => notice.id))
  const profile = $activeGatewayProfile.get() || 'default'
  $connection.set({ connectionId: 'ssh-machine', profile, remoteKind: 'ssh', mode: 'remote' } as never)
  recordSessionEventScope({ session_id: chat, profile, connectionId: 'ssh-machine' })
  const stops = [
    { attemptId: 'task-1', commandId: 'private-stop-1', observation: 'CANCEL_REQUESTED',
      facts: { dispatched: true, localStopped: false }, expected: ['подтверждено', 'без подтверждения'] },
    { attemptId: 'task-2', commandId: 'private-stop-2', observation: 'REVOKED_BEFORE_CANCEL',
      facts: { dispatched: false, localStopped: false }, expected: ['нет', 'без подтверждения'] },
    { attemptId: 'task-3', commandId: 'private-stop-3', observation: 'UNKNOWN',
      facts: {}, expected: ['неизвестно', 'неизвестно'] }
  ]
  const request = vi.fn<NonNullable<NonNullable<Window['hermesDesktop']>['symphonyOperator']>['request']>()
  request.mockImplementation(async (_address, call) => {
    if (call.action === 'receipt' && call.submissionId) return { state: 'ADMITTED', receipt: {
      submission_id: call.submissionId, admission_id: 'per-window-receipt', status: 'queued'
    } }
    if (call.action === 'status' && call.attemptId) return {
      state: 'STATUS', attemptId: call.attemptId, phase: 'RUNNING', observedAt: 3, evidence: 'private-proof'
    }
    if (call.action === 'stop') {
      const sample = stops.find(stop => stop.attemptId === call.attemptId)
      if (sample) return {
        state: 'STOP', attemptId: sample.attemptId, commandId: sample.commandId, observation: sample.observation,
        ...sample.facts
      }
    }
    if (call.action === 'stop-reconcile') return {
      state: 'STOP', attemptId: call.attemptId, commandId: 'private-stop-3', observation: 'NOT_ACCEPTED',
      dispatched: false, localStopped: false
    }
    if (call.action === 'stop-replace') return {
      state: 'STOP', attemptId: call.attemptId, commandId: 'private-stop-new', previousCommandId: 'private-stop-3',
      observation: 'CANCEL_REQUESTED', dispatched: true, localStopped: false
    }
    return { state: 'UNKNOWN' }
  })
  window.hermesDesktop = { ...desktop, symphonyOperator: {
    request, restoreWindow: vi.fn(), copyInstallationKey: vi.fn(), onClear: () => () => undefined
  } } as typeof window.hermesDesktop
  const client = await clientWithFixture(() => ({ ...routeProbe(chat), mode: 'personal' }))
  ipc.routed.mockImplementation((_connection, _profile, method, params, timeout) => client.request(method, params, timeout))
  let ui = openChat(chat, client.request)
  const quick = { timeout: 8000 }
  try {
    await ui.enter('Private test input')
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1), quick)
    const protectedInput = request.mock.calls[0]?.[1]
    expect(protectedInput).toMatchObject({ action: 'submit', text: 'Private test input' })
    if (protectedInput?.action !== 'submit') throw new Error('Private input did not reach the native bridge')
    await waitFor(() => expect(ui.editorText()).toBe('Private test input'), quick)
    expect(Object.values(storedJournal())).toContainEqual(expect.objectContaining({ forwardSessionId: chat, personalForward: true }))
    ui.unmount()
    ui = openChat(chat, client.request)
    await waitFor(() => expect(ui.editorText()).toBe('Private test input'), quick)
    await ui.enter()
    await waitFor(() => expect($notifications.get().some(notice => notice.message?.includes('per-window-receipt'))).toBe(true), quick)
    expect(request.mock.calls[1][1]).toMatchObject({ submissionId: protectedInput.submissionId })
    await ui.enter('/symphony status task-1')
    await waitFor(() => expect($notifications.get().some(notice => notice.message?.includes('private-proof'))).toBe(true), quick)
    for (const sample of stops) {
      await ui.enter(`/symphony stop ${sample.attemptId}`)
      const [dispatch, local] = sample.expected
      const message = `${sample.commandId} · ${sample.observation} · Обращение к исполнителю — ${dispatch} · Локальная остановка — ${local}`
      await waitFor(() => expect($notifications.get().some(notice => notice.message?.startsWith(message))).toBe(true), quick)
    }
    await ui.enter('/symphony stop-reconcile task-3')
    await waitFor(() => expect($notifications.get().some(notice =>
      notice.message?.includes('Для нового STOP введите /symphony stop-replace task-3'))).toBe(true), quick)
    await ui.enter('/symphony stop-replace task-3')
    await waitFor(() => expect($notifications.get().some(notice =>
      notice.message?.startsWith('private-stop-new · CANCEL_REQUESTED · Исходный STOP private-stop-3'))).toBe(true), quick)
    expect(request.mock.calls.map(([, call]) => call.action)).toEqual([
      'submit', 'receipt', 'status', 'stop', 'stop', 'stop', 'stop-reconcile', 'stop-replace'
    ])
    expect(request.mock.calls.slice(-2).map(([, call]) => call)).toEqual([
      { action: 'stop-reconcile', attemptId: 'task-3' }, { action: 'stop-replace', attemptId: 'task-3' }
    ])
    expect(client.writes.map(frame => frame.method).filter(method =>
      /^(?:prompt\.|operator\.|command\.dispatch$|slash\.exec$)/.test(method)
    )).toEqual(['prompt.forward_probe'])
    expect(ui.container.textContent).not.toContain('per-window-receipt')
    expect(ui.container.textContent).not.toContain('private-proof')
    expect(ui.container.textContent).not.toContain('private-stop')
  } finally {
    ui.unmount()
    ipc.routed.mockReset()
    client.close()
    $connection.set(prior)
    window.hermesDesktop = desktop
    for (const notice of $notifications.get()) if (!notices.has(notice.id)) dismissNotification(notice.id)
  }
}, 45_000)

it.each([false, true])('shows the forwarded admission after Enter with busy=%s', async busy => {
  const chat = 'ordinary-chat'
  const original = $connection.get()
  $connection.set({ connectionId: 'ssh-machine', remoteKind: 'ssh', mode: 'remote' } as never)
  const client = await clientWithFixture((method, params) => {
    if (method === 'prompt.forward_probe') return routeProbe(chat)
    if (method === 'prompt.forward_submit')
      return {
        submission_id: params.submission_id,
        admission_id: 'admitted',
        ref: { session_id: chat, profile_id: 'offline-profile' },
        status: 'queued'
      }
    return {}
  })
  const ui = openChat(chat, client.request, sessionRoute(chat), busy)
  try {
    await ui.enter('Inspect the offline fixture.')
    await waitFor(() => expect(ui.messages().some(m => m.id.startsWith('hermes-admission-'))).toBe(true), wait)
    expect(
      isInaccessible(
        await within(ui.container).findByText('Hermes принял сообщение · admission_id: admitted', {}, wait)
      )
    ).toBe(false)
    expect(client.writes.some(frame => frame.method === 'prompt.submit')).toBe(false)
    const sent = client.writes.filter(frame => frame.method === 'prompt.forward_submit')
    expect(sent).toHaveLength(1)
    expect(sent[0].params).toMatchObject({
      text: 'Inspect the offline fixture.',
      session_id: chat,
      submission_id: expect.any(String)
    })
  } finally {
    ui.unmount()
    client.close()
    $connection.set(original)
  }
})

it('keeps ordinary legacy chat available and displays the absence of a Symphony receipt', async () => {
  const chat = 'legacy-chat'
  const original = $connection.get()
  $connection.set({ connectionId: 'ssh-machine', remoteKind: 'ssh', mode: 'remote' } as never)
  const client = await clientWithFixture((method, params) => {
    if (method === 'prompt.forward_probe') return routeProbe(chat, 'ordinary')
    if (method === 'prompt.submit' && params.submission_id)
      throw { code: 4000, message: 'unknown parameter: submission_id' }
    if (method === 'prompt.submit') return { status: 'streaming' }
    return {}
  })
  const ui = openChat(chat, client.request)
  try {
    await ui.enter('Ordinary chat input.')
    await waitFor(() => expect(ui.messages().some(m => m.role !== 'user')).toBe(true), wait)
    expect(
      ui.messages().map(m => m.error ?? m.id),
      JSON.stringify(client.writes.map(f => f.method))
    ).toContainEqual(expect.stringMatching(/^hermes-chat-send-/))
    const message = await within(ui.container).findByText(/Квитанция постановки в Symphony недоступна/, {}, wait)
    expect(isInaccessible(message)).toBe(false)
    expect(
      client.writes.filter(frame => frame.method === 'prompt.submit').map(frame => frame.params.submission_id)
    ).toEqual([undefined])
    expect(ui.container.textContent).not.toContain('admission_id:')
  } finally {
    ui.unmount()
    client.close()
    $connection.set(original)
  }
})

it.each(['new chat', 'unavailable registry', 'personal mode', 'wrong conversation', 'empty generation'])(
  'keeps the draft before either text frame for %s', async scenario => {
    const chat = 'held-chat'
    const original = $connection.get()
    $connection.set({ connectionId: 'ssh-machine', remoteKind: 'ssh', mode: 'remote' } as never)
    const client = await clientWithFixture(method => {
      if (method !== 'prompt.forward_probe') throw Error(method)
      if (scenario === 'unavailable registry') throw { code: -32601, message: 'method unavailable' }
      const answer = routeProbe(chat)
      if (scenario === 'personal mode') return { ...answer, mode: 'personal' }
      if (scenario === 'empty generation') return { ...answer, generation: '' }
      return { ...answer, ref: { ...answer.ref, session_id: 'other' } }
    })
    const ui = openChat(chat, client.request, scenario === 'new chat' ? '/' : sessionRoute(chat))
    try {
      await ui.enter('Keep this original draft.')
      await waitFor(() => expect(ui.editorText()).toBe('Keep this original draft.'), wait)
      expect(client.writes.map(frame => frame.method)).toEqual(scenario === 'new chat' ? [] : ['prompt.forward_probe'])
    } finally {
      ui.unmount()
      client.close()
      $connection.set(original)
    }
  }
)

it('keeps busy ordinary Enter in its editable local queue', async () => {
  const chat = 'busy-ordinary'
  const previous = $connection.get()
  const previousState = $gatewayState.get()
  const previousMode = $busyInputConfig.get()
  $connection.set({ connectionId: 'ssh-machine', remoteKind: 'ssh', mode: 'remote' } as never)
  $gatewayState.set('open')
  $busyInputConfig.set({ connection: $connection.get(), owner: busyInputOwnerKey('ssh-machine', $activeGatewayProfile.get()), mode: 'queue' })
  const client = await clientWithFixture(() => routeProbe(chat, 'ordinary'))
  const ui = openChat(chat, client.request, sessionRoute(chat), true)
  try {
    await ui.enter('Editable queued message')
    await waitFor(() => expect(getQueuedPrompts(chat)).toMatchObject([{ text: 'Editable queued message' }]), wait)
    expect(getQueuedPrompts(chat)[0].serverStatus).toBeUndefined()
    expect(client.writes.map(row => row.method)).toEqual(['prompt.forward_probe'])
  } finally {
    ui.unmount()
    clearQueuedPrompts(chat)
    client.close()
    $busyInputConfig.set(previousMode)
    $gatewayState.set(previousState)
    $connection.set(previous)
  }
})

it('keeps the draft when the socket enters CLOSING before the write, then probes again on Enter', async () => {
  const chat = 'closing-chat'
  const previous = $connection.get()
  $connection.set({ connectionId: 'ssh-machine', remoteKind: 'ssh', mode: 'remote' } as never)
  let closeOnProbe = true
  const client = await clientWithFixture((method, params) => method === 'prompt.forward_probe' ? routeProbe(chat) : {
    ref: routeProbe(chat).ref, submission_id: params.submission_id, admission_id: 'accepted-second', status: 'queued'
  }, method => {
    if (method === 'prompt.forward_probe' && closeOnProbe) client.beginClosing()
  })
  const ui = openChat(chat, client.request)
  try {
    await ui.enter('Keep through pre-write close')
    await waitFor(() => expect(ui.editorText()).toBe('Keep through pre-write close'), wait)
    expect(ui.messages().some(row => row.error?.includes('HOLD'))).toBe(true)
    expect(client.writes.map(row => row.method)).toEqual(['prompt.forward_probe'])
    closeOnProbe = false
    await client.reconnect()
    await ui.enter()
    await within(ui.container).findByText(/admission_id: accepted-second/, {}, wait)
    expect(client.writes.map(row => row.method)).toEqual(['prompt.forward_probe', 'prompt.forward_probe', 'prompt.forward_submit'])
  } finally {
    ui.unmount()
    client.close()
    $connection.set(previous)
  }
})

it.each([
  ['admission_conflict', 'Конфликт ввода'],
  ['FORWARD_UNKNOWN', 'UNKNOWN'],
  ['FORWARD_HOLD', 'UNKNOWN']
])('displays the owner response %s after Enter', async (response, label) => {
  const previous = $connection.get()
  const chat = 'owner-reply-' + response
  $connection.set({ connectionId: 'ssh-machine', remoteKind: 'ssh', mode: 'remote' } as never)
  const client = await clientWithFixture(method => {
    if (method === 'prompt.forward_probe') return routeProbe(chat)
    throw { code: 4090, message: response }
  })
  let ui = openChat(chat, client.request)
  try {
    await ui.enter('Edited input for an existing conversation.')
    await waitFor(() => {
      expect(within(ui.container).queryAllByText(new RegExp(label)).some(notice => !isInaccessible(notice))).toBe(true)
    }, wait)
    expect(ui.container.textContent).not.toContain('admission_id:')
    expect(client.writes.map(frame => frame.method)).toEqual(['prompt.forward_probe', 'prompt.forward_submit'])
    const entry = Object.values(storedJournal()).find(row => row.id === client.writes[1].params.submission_id)
    expect(entry?.forwardSessionId).toBe(chat)
    if (response === 'FORWARD_HOLD') {
      ui.unmount()
      await client.reconnect()
      ui = openChat(chat, client.request)
      await waitFor(() => expect(ui.editorText()).toBe('Edited input for an existing conversation.'), wait)
      await ui.enter()
      await waitFor(() => expect(ui.editorText()).toBe('Edited input for an existing conversation.'), wait)
      expect(Object.values(storedJournal())).toContainEqual(expect.objectContaining({ id: entry!.id, forwardSessionId: chat }))
      expect(client.writes.map(frame => frame.method)).toEqual(['prompt.forward_probe', 'prompt.forward_submit'])
    }
    if (response === 'admission_conflict') {
      expect(entry?.forwardConflict).toBe(true)
      await waitFor(() => expect(ui.editorText()).toBe('Edited input for an existing conversation.'), wait)
      await ui.enter()
      expect(client.writes).toHaveLength(2)
    }
  } finally {
    ui.unmount()
    client.close()
    $connection.set(previous)
  }
})

it.each(['ordinary', 'forward'] as const)('rejects a %s probe after redialing the same address before any text frame', async route => {
  const client = await clientWithFixture(() => routeProbe('stored-chat', route))
  try {
    const result = await client.request<ForwardRoute>('prompt.forward_probe', { session_id: 'runtime' })
    await client.reconnect()
    const method = route === 'forward' ? 'prompt.forward_submit' : 'prompt.submit'
    await expect(client.request(method, {
      session_id: 'runtime', text: 'held', [probedRoute]: { route: result, current: () => true }
    })).rejects.toThrow('HOLD')
    expect(client.writes.map(row => row.method)).toEqual(['prompt.forward_probe'])
  } finally {
    client.close()
  }
})

it('retains the original ID across a socket loss and reopening the ordinary field', async () => {
  const chat = 'restored-chat'
  const previous = $connection.get()
  $connection.set({ connectionId: 'ssh-machine', remoteKind: 'ssh', mode: 'remote' } as never)
  let deliver!: (ack: unknown) => void
  const pending = new Promise(resolve => { deliver = resolve })
  const client = await clientWithFixture(method => {
    if (method === 'prompt.forward_probe') return routeProbe(chat)
    if (method === 'prompt.forward_submit') return pending
    throw Error(method)
  })
  let ui = openChat(chat, client.request)
  try {
    await ui.enter('Recover the original receipt.')
    await waitFor(() => expect(client.writes.at(-1)?.method).toBe('prompt.forward_submit'), wait)
    const id = client.writes.at(-1)!.params.submission_id
    expect(Object.values(storedJournal())).toContainEqual(expect.objectContaining({ id, forwardSessionId: chat }))
    await act(async () => { await client.reconnect() })
    deliver({ submission_id: id, ref: routeProbe(chat).ref, admission_id: 'late', status: 'queued' })
    await waitFor(() => expect(ui.messages().some(m => m.error?.includes('UNKNOWN'))).toBe(true), wait)
    await waitFor(() => expect(ui.editorText()).toBe('Recover the original receipt.'), wait)
    ui.unmount()
    ui = openChat(chat, client.request)
    await waitFor(() => expect(ui.editorText()).toBe('Recover the original receipt.'), wait)
    await ui.enter()
    await waitFor(() => expect(ui.editorText()).toBe('Recover the original receipt.'), wait)
    expect(Object.values(storedJournal())).toContainEqual(expect.objectContaining({ id, forwardSessionId: chat }))
    expect(client.writes.map(frame => frame.method)).toEqual(['prompt.forward_probe', 'prompt.forward_submit'])
    expect(ui.container.textContent).not.toContain('admission_id: late')
  } finally {
    ui.unmount()
    client.close()
    $connection.set(previous)
  }
})

const personalTaskMode = process.env.SYMPHONY_TASK_CAMPAIGN === '1'
const followupTaskMode = process.env.SYMPHONY_TASK_A4_A5_CAMPAIGN === '1'
if ((personalTaskMode || followupTaskMode) && (personalTaskMode && followupTaskMode ||
    [process.env.SYMPHONY_FORWARD_CAMPAIGN, process.env.SYMPHONY_ROUTE_CAMPAIGN].includes('1'))) {
  throw new Error('Each TASK campaign requires its own run and receipt')
}

function taskWindows(peer: RoutePeer, userData: string) {
  const route = peer.registration
  const url = 'file:///hermes/index.html'
  const getURL = () => url + '#/' + encodeURIComponent(route.storedConversation)
  const managed = new Map<object, object>()
  let nextId = 0
  let restored: ReturnType<typeof create> | undefined
  let selectedSubmission = ''
  let replacement: { attemptId: string; commandId: string } | undefined
  let confirmedReplacements = 0
  function create() {
    const signals = new EventEmitter()
    let ended = false
    const sender = Object.assign(new EventEmitter(), {
      id: ++nextId, mainFrame: {}, getURL, isDestroyed: () => ended,
      send: (name: string, ...args: unknown[]) => signals.emit(name, {}, ...structuredClone(args))
    })
    const host = Object.assign(new EventEmitter(), {
      webContents: sender, isDestroyed: () => ended, isFocused: () => !ended,
      destroy() { ended = true; sender.emit('destroyed'); host.emit('closed') }
    })
    managed.set(sender, host)
    const bridge = {
      invoke: async (name: string, ...args: unknown[]) => {
        const handler = ipc.handle.mock.calls.findLast(row => row[0] === name)?.[1]
        if (!handler) throw new Error('Missing Desktop IPC handler')
        return structuredClone(await handler({ sender, senderFrame: sender.mainFrame }, ...structuredClone(args)))
      },
      on: signals.on.bind(signals), removeListener: signals.removeListener.bind(signals)
    } as Parameters<typeof symphonyPlanBridge>[0]
    return { host, plan: symphonyPlanBridge(bridge), operator: symphonyOperatorBridge(bridge) }
  }
  const matches = (address: { connectionId: string; profile: string; conversation: string }) =>
    address.connectionId === route.connectionId && address.profile === route.profile && address.conversation === route.conversation
  ipc.userData.mockReturnValue(userData)
  ipc.window.mockImplementation(sender => managed.get(sender))
  registerSymphonyRoute(() => url, (_id, address) => matches(address), peer.registry)
  registerSymphonyOperator({
    rendererUrl: () => url, matchesWindow: (_window, address) => matches(address), environment: peer.registry,
    createWindow: () => { restored = create(); return restored.host as never },
    showMessageBox: (async (_source: object, options: { title?: string; detail?: string; buttons?: string[] }) => {
      if (options.title === 'Symphony STOP') {
        const requested = replacement
        replacement = undefined
        expect(options.detail).toBe(`Attempt: ${requested?.attemptId}\nOriginal STOP: ${requested?.commandId}`)
        expect(options.buttons).toEqual(['Cancel', 'Send new STOP'])
        confirmedReplacements++
        return { response: requested ? 1 : 0, checkboxChecked: false }
      }
      return { response: Math.max(0, options.buttons?.indexOf(selectedSubmission) ?? -1), checkboxChecked: false }
    }) as never
  })
  const original = create()
  const neighbor = create()
  return {
    original, neighbor,
    allowReplacement(attemptId: string, commandId: string) { replacement = { attemptId, commandId } },
    get confirmedReplacements() { return confirmedReplacements },
    async restore(submissionId: string) {
      selectedSubmission = submissionId
      const prior = restored
      await original.operator.restoreWindow()
      if (!restored || restored === prior) throw new Error('Native recovery did not create a replacement window')
      return restored
    },
    close() { for (const window of managed.values()) (window as ReturnType<typeof create>['host']).destroy() }
  }
}

interface TaskCounts extends TaskDiagnosticCounts {
  admissions: { request_id: string; admission_id: string }[]
  requests: { request_id: string }[]
  attempts: { attempt_id: string; project: string; job: string; revision: string }[]
  decisions: number
  stop_commands: number
  stop_receipts: { command_id: string; state: string; attempt_id: string }[]
  stop_reconciliations: { command_id: string; state: string; attempt_id: string }[]
  stale_stop_targets: number
  ipc: Record<string, { entered: number; returned: number; conflicts: number }>
  cancel_causes: number
  executor_cancels: number
}

interface FollowupTask {
  routePeer: RoutePeer
  controls: ReturnType<typeof taskWindows>
  notice(text: string): Promise<void>
  decision(view: ReturnType<typeof openChat>, admissionId: string, approve: boolean,
    beforeAnswer?: (binding: TaskCounts['plan_inputs'][number]) => Promise<void>): Promise<TaskCounts['plan_inputs'][number]>
  snapshot(stage: string): Promise<TaskCounts>
  observe(stage: string, facts: object): void
  counts(): Promise<TaskCounts>
  onFailure(readback: () => Promise<object>): void
  journal: SymphonyOperatorJournal
  scopedAddress: OperatorAddress
  ordinaryMethods: string[]
  replaceView(owner: ReturnType<typeof taskWindows>['original']): Promise<ReturnType<typeof openChat>>
  campaign: string
}

async function observeTaskA4A5(task: FollowupTask) {
  const { routePeer, controls, notice, decision, snapshot, observe, counts, journal, scopedAddress, replaceView } = task
  const { connectionId, profile, conversation } = scopedAddress
  const address = { connectionId, profile, conversation }
  const noStops = { stop_commands: 0, stop_receipts: [], stop_reconciliations: [], cancel_causes: 0, executor_cancels: 0 }
  const worker = (action: 'arm' | 'state' | 'release') => routePeer.control('worker', { action })
  const held = () => waitFor(async () => expect(await worker('state')).toEqual({ state: 'held' }), wait)
  const submit = async (view: ReturnType<typeof openChat>, label: string) => {
    const start = routePeer.operatorTrace.length
    await view.enter(`Personal TASK ${label} ${task.campaign}`)
    const entry = await waitFor(() => {
      const reply = routePeer.operatorTrace.slice(start).find(row => row.action === 'submit' && row.stage === 'reply')
      expect(reply?.admission_id).toBeTruthy()
      return reply!
    }, wait)
    return { admissionId: entry.admission_id!, submissionId: entry.submission_id }
  }

  const first = await replaceView(controls.original)
  const a = await submit(first, 'A')
  await notice(`Квитанция ${a.admissionId}`)
  await worker('arm')
  const planA = await decision(first, a.admissionId, true)
  await held()
  const beforeB = await snapshot('A approved through Desktop and held without STOP')
  expect(beforeB).toMatchObject({ ...noStops, launches: 1, reservations: 1, stale_stop_targets: 0 })
  expect(beforeB.attempts).toHaveLength(1)
  expect(beforeB.by_request[planA.request_id]).toMatchObject({ decision: 'APPROVED', plan_digest: planA.plan_digest })
  const oldAttempt = beforeB.attempts[0].attempt_id
  await worker('release')
  const previous = await controls.restore(a.submissionId)
  expect(await previous.operator.request(address, { action: 'receipt', submissionId: a.submissionId }))
    .toMatchObject({ state: 'ADMITTED', receipt: { admission_id: a.admissionId } })
  const terminal = await waitFor(async () => {
    const result = await previous.operator.request(address, { action: 'status', attemptId: oldAttempt }) as OperatorReply
    expect(result).toMatchObject({ state: 'STATUS', attemptId: oldAttempt })
    expect(result.state === 'STATUS' && ['COMPLETED', 'STOPPED'].includes(result.phase)).toBe(true)
    return result
  }, wait)
  expect(journal.find(scopedAddress, a.submissionId)?.stop).toBeUndefined()
  const terminalCounts = await counts().catch(() => undefined)
  const atTerminal = taskWorkerSettlement(terminalCounts, oldAttempt, planA.request_id)
  observe('A terminal with restored binding and no prior STOP', {
    attempt: beforeB.attempts[0], terminal,
    worker_counts_read: terminalCounts ? 'fulfilled' : 'unavailable',
    worker_outcome: atTerminal.worker_outcome, launch_at_terminal: atTerminal.launch
  })
  const settledA = atTerminal.settled ? atTerminal : await waitFor(async () => {
    const evidence = taskWorkerSettlement(await counts(), oldAttempt, planA.request_id)
    expect(evidence, 'A must finish and release its workspace before B').toMatchObject({ settled: true })
    return evidence
  }, wait)
  observe('A finished and workspace hold cleared before B', {
    attempt_id: oldAttempt, request_id: planA.request_id, ...settledA
  })

  let delayedA: Promise<OperatorReply> | undefined
  const second = await replaceView(controls.neighbor)
  const b = await submit(second, 'B')
  const answerStart = routePeer.planAnswerReplies.length
  const firstAnswer = () => routePeer.planAnswerReplies.slice(answerStart).find(row => row.request_id === 'task-' + b.admissionId)
  let digestB: string | undefined
  task.onFailure(() => readTaskFailure(b.admissionId, digestB, firstAnswer(), beforeB, counts, () => worker('state')))
  await notice(`Квитанция ${b.admissionId}`)
  expect(b.admissionId).not.toBe(a.admissionId)
  expect(b.submissionId).not.toBe(a.submissionId)
  await worker('arm')
  const planB = await decision(second, b.admissionId, true, async binding => {
    digestB = binding.plan_digest
    routePeer.interceptNextStop('hold')
    delayedA = previous.operator.request(address, { action: 'stop', attemptId: oldAttempt })
    await waitFor(() => expect(routePeer.operatorTrace).toContainEqual(expect.objectContaining({
      action: 'stop', stage: 'held-before', attempt_id: oldAttempt, submission_id: a.submissionId,
    })), wait)
  })
  const answerB = firstAnswer()
  observe('B first plan answer at transport', { answer: answerB ?? null })
  expect(answerB?.plan_digest).toBe(planB.plan_digest)
  expect(taskLaunchAccepted(answerB), 'B first answer must confirm a fresh submission').toBe(true)
  await held()
  const runningB = await snapshot('B approved and held, signed A STOP still outside owner IPC')
  expect(runningB).toMatchObject({ ...noStops, launches: 2, reservations: 2, decisions: 2, permits: 2 })
  expect(runningB.attempts).toHaveLength(2)
  expect(runningB.by_request[planB.request_id]).toMatchObject({ decision: 'APPROVED', plan_digest: planB.plan_digest })
  const attemptB = runningB.attempts.find(row => row.attempt_id !== oldAttempt)!
  expect(attemptB.project).toBe(beforeB.attempts[0].project)
  expect(attemptB.job).not.toBe(beforeB.attempts[0].job)
  expect(attemptB.revision).toBeTruthy()
  const attempt = attemptB.attempt_id
  const storedA = journal.find(scopedAddress, a.submissionId)!.stop!
  expect(storedA.attemptId).toBe(oldAttempt)
  await routePeer.releaseInterceptedStop()
  expect(await delayedA).toEqual({ state: 'HOLD' })
  expect(routePeer.operatorTrace).toContainEqual(expect.objectContaining({ action: 'stop', stage: 'refused',
    error: 'OPERATOR_HOLD', attempt_id: oldAttempt, command_id: storedA.id, submission_id: a.submissionId }))
  const refusedA = await snapshot('delayed A STOP reached owner while B was held and was refused')
  expect(refusedA).toMatchObject(noStops)
  expect(refusedA.ipc['operator-execute'].entered - runningB.ipc['operator-execute'].entered).toBe(1)
  expect(refusedA.stale_stop_targets - runningB.stale_stop_targets).toBe(1)
  expect(await controls.neighbor.operator.request(address, { action: 'receipt', submissionId: b.submissionId }))
    .toMatchObject({ state: 'ADMITTED', receipt: { admission_id: b.admissionId } })
  expect(journal.find(scopedAddress, b.submissionId)?.attemptId).toBe(attempt)
  expect(await controls.neighbor.operator.request(address, { action: 'stop', attemptId: oldAttempt })).toEqual({ state: 'HOLD' })
  const wrongStatus = await controls.neighbor.operator.request(address, { action: 'status', attemptId: oldAttempt })
  expect(['HOLD', 'UNKNOWN']).toContain(wrongStatus.state)
  expect(journal.find(scopedAddress, b.submissionId)?.stop).toBeUndefined()
  await second.enter(`/symphony status ${attempt}`)
  await notice(`${attempt} · RUNNING`)

  const beforeLoss = await snapshot('A has no STOP effects and current B remains RUNNING')
  expect(beforeLoss).toMatchObject(noStops)
  routePeer.interceptNextStop('lose')
  await second.enter(`/symphony stop ${attempt}`)
  await notice('UNKNOWN · Исход уточняется по квитанции.')
  const afterLoss = await snapshot('B STOP lost before owner IPC')
  expect(afterLoss).toMatchObject(noStops)
  expect(afterLoss.ipc['operator-execute'].entered).toBe(beforeLoss.ipc['operator-execute'].entered)
  const oldCommand = journal.find(scopedAddress, b.submissionId)!.stop!.id
  expect(routePeer.operatorTrace).toContainEqual(expect.objectContaining({ action: 'stop', stage: 'lost-before',
    command_id: oldCommand, attempt_id: attempt, submission_id: b.submissionId }))
  await second.enter(`/symphony stop ${attempt}`)
  await notice(`${oldCommand} · UNKNOWN`)
  expect(await controls.neighbor.operator.request(address, { action: 'stop-replace', attemptId: attempt }))
    .toMatchObject({ state: 'STOP', commandId: oldCommand, observation: 'UNKNOWN' })
  expect(controls.confirmedReplacements).toBe(0)

  await second.enter(`/symphony stop-reconcile ${attempt}`)
  await notice(`${oldCommand} · NOT_ACCEPTED`)
  const sealed = { command_id: oldCommand, attempt_id: attempt, state: 'NOT_ACCEPTED' }
  expect(await counts()).toMatchObject({ ...noStops, stop_reconciliations: [sealed] })
  await routePeer.releaseInterceptedStop()
  await waitFor(() => expect(routePeer.operatorTrace).toContainEqual(expect.objectContaining({
    action: 'stop', stage: 'reply', ...sealed, dispatched: false, local_stopped: false,
  })), wait)
  expect(await snapshot('late B STOP read the seal without dispatch')).toMatchObject({ ...noStops, stop_reconciliations: [sealed] })

  const recovered = await controls.restore(b.submissionId)
  const restored = await replaceView(recovered)
  expect(await recovered.operator.request(address, { action: 'stop', attemptId: attempt })).toMatchObject({
    state: 'STOP', commandId: oldCommand, observation: 'NOT_ACCEPTED', dispatched: false, localStopped: false,
  })
  controls.allowReplacement(attempt, oldCommand)
  routePeer.lostOperatorAcks.add('stop_replace')
  await restored.enter(`/symphony stop-replace ${attempt}`)
  const replacement = await waitFor(() => {
    const write = routePeer.operatorTrace.find(row => row.action === 'stop_replace' && row.stage === 'dropped')
    expect(write).toMatchObject({ attempt_id: attempt, command_id: expect.any(String), previous_command_id: oldCommand,
      state: 'CANCEL_REQUESTED', dispatched: true, local_stopped: false })
    return write!
  }, wait)
  expect(replacement.command_id).not.toBe(oldCommand)
  expect(controls.confirmedReplacements).toBe(1)
  const reopened = await controls.restore(b.submissionId)
  const finalView = await replaceView(reopened)
  await finalView.enter(`/symphony stop ${attempt}`)
  await notice(`${replacement.command_id} · CANCEL_REQUESTED · Исходный STOP ${oldCommand}`)
  const final = await snapshot('replacement STOP committed once, lost ACK read by new ID')
  expect(final).toMatchObject({ launches: 2, stop_commands: 1, cancel_causes: 1, executor_cancels: 1,
    stop_reconciliations: [sealed], stop_receipts: [{ command_id: replacement.command_id, attempt_id: attempt, state: 'CANCEL_REQUESTED' }] })
  expect(journal.find(scopedAddress, b.submissionId)?.stop).toMatchObject({ id: replacement.command_id, attemptId: attempt,
    previous: { id: oldCommand, attemptId: attempt } })
  expect(routePeer.operatorTrace.filter(row => row.action === 'stop_replace' && row.stage === 'sent')).toHaveLength(1)
  expect(routePeer.operatorTrace.filter(row => row.action === 'stop_result' && row.stage === 'reply').at(-1))
    .toMatchObject({ command_id: replacement.command_id, previous_command_id: oldCommand, attempt_id: attempt })
  expect(task.ordinaryMethods.filter(method => /^(?:prompt\.(?!forward_probe$)|operator\.|command\.dispatch$|slash\.exec$)/.test(method))).toEqual([])
  observe('open acceptance criteria', { native_stop_proof: 'requires native host evidence', external_effects: 'not observed' })
}

async function verifyTaskStopReadIsolation(
  peer: RoutePeer, address: OperatorAddress, installation: OperatorInstallation,
  previous: OperatorBinding, current: OperatorBinding, attempt: string, command: string
) {
  const actors = [
    { name: 'retired generation on a new socket', key: operatorPrivateKey(previous.windowKey), generation: previous.generation },
    { name: 'separate window key on a new socket', key: generateKeyPairSync('ed25519').privateKey, generation: current.generation }
  ]
  for (const actor of actors) {
    const channel = await openOperatorChannel(peer.registry, address)
    try {
      const response = exchangeOperator({ channel, installationKey: installation.key, windowKey: actor.key,
        submissionId: current.submissionId, windowGeneration: actor.generation, current: () => true },
      'stop_result', { attempt_id: attempt, command_id: command })
      await expect(response).rejects.toThrow(/OPERATOR_(HOLD|UNKNOWN)/)
    } finally {
      channel.socket.close()
    }
  }
  return actors.map(actor => actor.name)
}

function registerPersonalTaskCampaign(followup: boolean) {
  const name = followup ? 'observes personal TASK A4 and A5 through stdio' : 'observes personal TASK admission, recovery, status and STOP through stdio'
  const run = async () => {
    const { SYMPHONY_TASK_PEER: linux, SYMPHONY_TASK_RECEIPT: dir, HERMES_PYTHON: python } = process.env
    if (!linux || !dir || !python || !existsSync(python) ||
        !existsSync(path.join(linux, 'tests/plugins/symphony_operator_peer.py'))) throw new Error('TASK fixture inputs are missing')
    const shas = [path.resolve('../..'), linux].map(root =>
      execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim())
    expect(shas).toEqual([process.env.SYMPHONY_TASK_WINDOWS_SHA, process.env.SYMPHONY_TASK_LINUX_SHA])
    mkdirSync(dir, { recursive: true })
    const campaign = randomUUID()
    const receipt = { campaign, windows_sha: shas[0], linux_sha: shas[1], state: 'IN_PROGRESS',
      started: new Date().toISOString(), stages: [] as object[] }
    const receiptName = followup ? 'joint-task-a4-a5' : 'joint-task'
    const file = path.join(dir, receiptName + '.json')
    const save = (flag = 'w') => writeFileSync(file, JSON.stringify(receipt, null, 2) + '\n', { flag })
    const observe = (stage: string, facts: object) => { receipt.stages.push(structuredClone({ stage, ...facts })); save() }
    save('wx')
    const previous = window.hermesDesktop
    const previousConnection = $connection.get()
    const previousRegistry = $connectionsRegistry.get()
    const priorNoticeIds = new Set($notifications.get().map(notice => notice.id))
    const ordinary = new HermesGateway()
    let peer: RoutePeer | undefined
    let windows: ReturnType<typeof taskWindows> | undefined
    let ui: ReturnType<typeof openChat> | undefined
    let closed: Promise<void> | undefined
    let failureReadback: (() => Promise<object>) | undefined
    try {
      const temp = mkdtempSync(path.join(tmpdir(), 'hermes-personal-task-'))
      const userData = path.join(temp, 'desktop')
      const journal = new SymphonyOperatorJournal(userData, ipc.storage)
      const installation = journal.installation()
      const token = randomUUID()
      const routePeer = new RoutePeer(python, linux, temp, 'symphony_operator_peer')
      peer = routePeer
      await routePeer.start(token, operatorPublicKey(installation.key))
      const address = routePeer.registration
      const scopedAddress = { connectionId: address.connectionId, profile: address.profile,
        conversation: address.conversation, storedConversation: address.storedConversation }
      const chat = address.conversation
      const controls = taskWindows(routePeer, userData)
      windows = controls
      const counts = (): Promise<TaskCounts> => routePeer.control('counts')
      const snapshot = async (stage: string) => {
        const value = await counts()
        const { worker_diagnostics: _workerDiagnostics, ...receiptCounts } = value
        observe(stage, { counts: receiptCounts, operator: routePeer.operatorTrace })
        return value
      }
      const fresh = await snapshot('fresh personal route')
      expect(fresh).toMatchObject({ admissions: [], requests: [], decisions: 0,
        permits: 0, reservations: 0, launches: 0, stop_commands: 0, cancel_causes: 0, executor_cancels: 0 })
      if (followup) expect(fresh).toMatchObject({ stop_reconciliations: [], stale_stop_targets: 0 })
      const dialUrl = buildGatewayWsUrl('http://127.0.0.1', token)
      const ordinaryMethods: string[] = []
      let socketId = 0
      vi.stubGlobal('WebSocket', taskWebSocket(async (url, protocols, receive) => {
        expect([url, protocols]).toEqual([dialUrl, []])
        const socket = await routePeer.openOrdinary('task-renderer-' + ++socketId, url, protocols, receive)
        return {
          send: (raw: string) => {
            const frame = JSON.parse(raw)
            if (frame.method) ordinaryMethods.push(frame.method)
            return socket.send(raw)
          },
          close: () => (closed = socket.close())
        }
      }))
      const request: GatewayRequest = (method, params, timeout) => ordinary.request(method, params, timeout)
      ipc.routed.mockImplementation((connection, profile, method, params, timeout) => {
        expect([connection, profile]).toEqual([address.connectionId, address.profile])
        return request(method, params, timeout)
      })
      $connection.set({ mode: 'remote', remoteKind: 'ssh', connectionId: address.connectionId, profile: address.profile } as never)
      $connectionsRegistry.set({ connections: [{ id: address.connectionId, kind: 'ssh' }] } as never)
      const open = async (owner: typeof controls.original) => {
        await ordinary.connect(dialUrl)
        const result = await ordinary.request<{ session_id: string; session_key: string }>('session.resume', { session_id: chat, lazy: true })
        expect(result.session_key).toBe(chat)
        for (const session_id of [chat, result.session_id]) recordSessionEventScope({ session_id, profile: address.profile, connectionId: address.connectionId })
        window.hermesDesktop = { ...previous, symphonyPlan: owner.plan, symphonyOperator: owner.operator } as typeof window.hermesDesktop
        const view = openChat(chat, request, sessionRoute(chat), false, result.session_id)
        ui = view
        return view
      }
      const notice = async (text: string) => waitFor(() => expect($notifications.get().some(row => row.message?.includes(text))).toBe(true), wait)
      const decision: FollowupTask['decision'] = async (view, admissionId, approve, beforeAnswer) => {
        const binding = await waitFor(async () => {
          const value = (await counts()).plan_inputs.find(item => item.admission_id === admissionId)
          expect(value).toBeDefined()
          return value!
        }, wait)
        expect(binding.request_id).toBe('task-' + admissionId)
        await waitForReadyPlan(view.container, binding.request_id, binding.plan_digest)
        await beforeAnswer?.(binding)
        await view.enter(approve ? 'подтверждаю' : 'отклоняю')
        await waitFor(() => {
          const card = within(within(view.container).getByRole('region', { name: 'План Symphony' }))
          expect(card.getByText('План Symphony · ' + binding.request_id)).toBeTruthy()
          expect(card.getByText('SHA-256 ' + binding.plan_digest)).toBeTruthy()
          expect(card.getByRole('status').textContent).toBe(approve ? 'План подтверждён.' : 'План отклонён.')
        }, wait)
        return binding
      }

      if (followup) {
        await observeTaskA4A5({ routePeer, controls, notice, decision, snapshot, observe, counts, journal, scopedAddress,
          onFailure: readback => { failureReadback = readback },
          ordinaryMethods, campaign, replaceView: async owner => {
            ui?.unmount()
            ui = undefined
            ordinary.close()
            await closed
            return open(owner)
          } })
        return
      }

      const firstWindow = await open(controls.original)
      await firstWindow.enter('Delivered personal TASK ' + campaign)
      const first = await waitFor(() => {
        const row = routePeer.operatorTrace.find(item => item.action === 'submit' && item.stage === 'reply')
        expect(row?.admission_id).toBeTruthy()
        return row!
      }, wait)
      expect(first.admission_id).not.toBe(first.submission_id)
      await notice(`Квитанция ${first.admission_id}`)
      const deny = await decision(firstWindow, first.admission_id!, false)
      const firstCounts = await snapshot('delivered ACK and exact plan denied')
      expect(firstCounts).toMatchObject({ decisions: 1, permits: 0, reservations: 0, launches: 0 })
      expect(firstCounts.by_request[deny.request_id]).toMatchObject({ decision: 'DENIED', plan_digest: deny.plan_digest })
      expect(firstCounts.admissions).toContainEqual(expect.objectContaining({ request_id: first.submission_id, admission_id: first.admission_id }))

      routePeer.lostOperatorAcks.add('submit')
      const text = 'Recover personal TASK ' + campaign
      await firstWindow.enter(text)
      const second = await waitFor(() => {
        const row = routePeer.operatorTrace.find(item => item.action === 'submit' && item.stage === 'dropped')
        expect(row?.admission_id).toBeTruthy()
        return row!
      }, wait)
      expect(second.submission_id).not.toBe(first.submission_id)
      await waitFor(() => expect(firstWindow.editorText()).toBe(text), wait)
      expect(Object.values(storedJournal())).toContainEqual(expect.objectContaining({ id: second.submission_id, personalForward: true }))
      const retired = journal.find(scopedAddress, second.submission_id)
      expect(retired?.generation).toBe(1)
      firstWindow.unmount()
      const replacement = await controls.restore(second.submission_id)
      ordinary.close()
      await closed
      const restored = await open(replacement)
      await waitFor(() => expect(restored.editorText()).toBe(text), wait)
      await restored.enter()
      await notice(`Квитанция ${second.admission_id}`)
      await waitFor(async () => expect((await counts()).requests.filter(item => item.request_id === 'task-' + second.admission_id)).toHaveLength(1), wait)
      const beforeApproval = await snapshot('personal ACK recovered once on replacement window')
      expect(beforeApproval).toMatchObject({ permits: 0, reservations: 0, launches: 0 })
      expect(beforeApproval.admissions.filter(row => row.request_id === second.submission_id)).toEqual([
        expect.objectContaining({ admission_id: second.admission_id })
      ])

      await routePeer.control('worker', { action: 'arm' })
      const allow = await decision(restored, second.admission_id!, true)
      await waitFor(async () => expect(await routePeer.control('worker', { action: 'state' })).toMatchObject({ state: 'held' }), wait)
      const launched = await snapshot('approved exact plan and one held synthetic attempt')
      expect(launched).toMatchObject({ decisions: 2, permits: 1, reservations: 1, launches: 1 })
      expect(launched.by_request[allow.request_id]).toMatchObject({ decision: 'APPROVED', plan_digest: allow.plan_digest })
      expect(launched.attempts).toHaveLength(1)
      const attempt = launched.attempts[0].attempt_id
      await restored.enter(`/symphony status ${attempt}`)
      await notice(`${attempt} · RUNNING`)
      const status = routePeer.operatorTrace.find(item => item.action === 'status' && item.stage === 'reply')
      expect(status).toMatchObject({ attempt_id: attempt, phase: 'RUNNING', observed_at: expect.any(Number), evidence: expect.any(String) })
      expect((await counts()).launches).toBe(1)

      const privateAddress = { connectionId: address.connectionId, profile: address.profile, conversation: chat }
      for (const actor of [controls.original, controls.neighbor]) {
        const verdict: OperatorReply = await actor.operator.request(privateAddress, { action: 'receipt', submissionId: second.submission_id })
        expect(verdict.state).toBe('HOLD')
      }
      expect((await controls.neighbor.operator.request(privateAddress, { action: 'stop', attemptId: attempt })).state).toBe('HOLD')
      const isolated = await snapshot('known IDs outside the controlling window are held')
      expect(isolated).toMatchObject({ decisions: 2, launches: 1, stop_commands: 0, cancel_causes: 0, executor_cancels: 0 })

      routePeer.lostOperatorAcks.add('stop')
      await restored.enter(`/symphony stop ${attempt}`)
      await notice('UNKNOWN · Исход уточняется по квитанции.')
      const dropped = routePeer.operatorTrace.find(row => row.action === 'stop' && row.stage === 'dropped')
      expect(dropped).toMatchObject({ attempt_id: attempt, command_id: expect.any(String), state: 'CANCEL_REQUESTED' })
      await restored.enter(`/symphony stop ${attempt}`)
      await notice(`${dropped!.command_id} · CANCEL_REQUESTED`)
      const final = await snapshot('STOP ACK lost after owner write and read back by original command ID')
      expect(final).toMatchObject({ decisions: 2, launches: 1, stop_commands: 1, cancel_causes: 1, executor_cancels: 1 })
      expect(final.stop_receipts).toContainEqual(expect.objectContaining({ command_id: dropped!.command_id, attempt_id: attempt }))
      const command = dropped!.command_id!
      const current = journal.find(scopedAddress, second.submission_id)
      expect(current?.stop?.id).toBe(command)
      expect(current!.generation).toBeGreaterThan(retired!.generation)
      const actors = await verifyTaskStopReadIsolation(routePeer, scopedAddress, installation, retired!, current!, attempt, command)
      for (const mismatch of [{ profile: 'another-profile' }, { conversation: 'another-conversation' }]) {
        expect((await replacement.operator.request({ ...privateAddress, ...mismatch },
          { action: 'status', attemptId: attempt })).state).toBe('HOLD')
      }
      const stale = await replacement.operator.request(privateAddress, { action: 'status', attemptId: attempt + '-mismatch' })
      expect(['HOLD', 'UNKNOWN']).toContain(stale.state)
      const allowedAgain = await replacement.operator.request(privateAddress, { action: 'stop', attemptId: attempt })
      expect(allowedAgain).toMatchObject({ state: 'STOP', commandId: command, attemptId: attempt, observation: 'CANCEL_REQUESTED' })
      const readback = await snapshot('known STOP remains accessible only to controlling window')
      for (const key of ['decisions', 'launches', 'stop_commands', 'cancel_causes', 'executor_cancels'] as const)
        expect(readback[key]).toBe(final[key])
      observe('isolation controls', { actors, command_id: command, attempt_id: attempt,
        other_profile: 'HOLD', other_conversation: 'HOLD', mismatched_attempt: stale.state })
      const transmitted = routePeer.operatorTrace.filter(row => row.stage === 'sent')
      expect(transmitted.filter(row => row.action === 'submit').map(row => row.submission_id)).toEqual([first.submission_id, second.submission_id])
      expect(transmitted.filter(row => row.action === 'stop')).toHaveLength(1)
      expect(transmitted.filter(row => row.action === 'stop_result').every(row => row.command_id === command && row.attempt_id === attempt)).toBe(true)
      expect(routePeer.operatorTrace.filter(row => row.stage === 'reply' && row.action === 'stop_result'))
        .toEqual([expect.objectContaining({ command_id: command }), expect.objectContaining({ command_id: command })])
      expect(ordinaryMethods.filter(method => /^(?:prompt\.(?!forward_probe$)|operator\.|command\.dispatch$|slash\.exec$)/.test(method))).toEqual([])
      observe('open acceptance criteria', { stop_before_write_rearm: 'HOLD', delivered_duplicate_and_conflict: 'separate server evidence',
        legacy_personal_receipt: 'OPERATOR denies shared read', native_stop_proof: 'requires native host evidence' })
    } catch (error) {
      receipt.state = 'INCOMPLETE'
      observe('failure', { kind: error instanceof Error ? error.name : 'error', operator: peer?.operatorTrace ?? [] })
      if (failureReadback) {
        const readback = await failureReadback().catch(() => ({ readback: 'unavailable' }))
        observe('B failure readback before cleanup', readback)
      }
      throw error
    } finally {
      try {
        try {
          ui?.unmount()
          windows?.close()
          ordinary.close()
          await closed
        } finally {
          ipc.routed.mockReset()
          ipc.userData.mockReset()
          ipc.window.mockReset()
          window.hermesDesktop = previous
          $connection.set(previousConnection)
          $connectionsRegistry.set(previousRegistry)
          for (const row of $notifications.get()) if (!priorNoticeIds.has(row.id)) dismissNotification(row.id)
          vi.unstubAllGlobals()
          if (peer) {
            await peer.shutdown()
            observe('peer cleanup completed', { exit: peer.operatorExitStatus })
          }
        }
        if (receipt.state === 'IN_PROGRESS') receipt.state = followup ? 'TASK_A4_A5_OBSERVED_WITH_OPEN_CRITERIA' : 'TASK_PERSONAL_OBSERVED_WITH_OPEN_CRITERIA'
      } catch (error) {
        receipt.state = 'INCOMPLETE'
        observe('fixture cleanup failed', { kind: error instanceof Error ? error.name : 'error', exit: peer?.operatorExitStatus ?? null })
        throw error
      } finally {
        save()
        writeFileSync(path.join(dir, receiptName + '.sha256'), createHash('sha256')
          .update(JSON.stringify(receipt, null, 2) + '\n').digest('hex') + `  ${receiptName}.json\n`, { flag: 'wx' })
      }
    }
  }
  it.runIf(followup ? followupTaskMode : personalTaskMode)(name, run, 300_000)
}

registerPersonalTaskCampaign(false)
registerPersonalTaskCampaign(true)

it.runIf(process.env.SYMPHONY_FORWARD_CAMPAIGN === '1')(
  'submits and reconciles the ordinary SSH conversation through stdio',
  async () => {
    const linux = process.env.SYMPHONY_FORWARD_PEER!
    const dir = process.env.SYMPHONY_FORWARD_RECEIPT!
    const shas = [path.resolve('../..'), linux].map(root =>
      execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim()
    )
    expect(shas).toEqual([process.env.SYMPHONY_FORWARD_WINDOWS_SHA, process.env.SYMPHONY_FORWARD_LINUX_SHA])
    mkdirSync(dir, { recursive: true })
    const campaign = randomUUID()
    const stages: object[] = []
    const receipt = {
      campaign,
      windows_sha: shas[0],
      linux_sha: shas[1],
      started: new Date().toISOString(),
      state: 'IN_PROGRESS',
      stages
    }
    const save = (flag = 'w') =>
      writeFileSync(path.join(dir, 'joint-forward.json'), JSON.stringify(receipt, null, 2) + '\n', { flag })
    const observe = (stage: string, facts: object) => {
      stages.push(structuredClone({ stage, ...facts }))
      save()
    }
    save('wx')
    const peer = new RoutePeer(
      process.env.HERMES_PYTHON!,
      linux,
      mkdtempSync(path.join(tmpdir(), 'hermes-forward-')),
      'symphony_forward_peer'
    )
    const previous = window.hermesDesktop
    peer.onAnswerFrame = observe
    const signals = new EventEmitter()
    let ui: ReturnType<typeof openChat> | undefined
    let sender: (EventEmitter & { id: number }) | undefined
    const token = randomUUID()
    const ordinary = new HermesGateway()
    let closed: Promise<void> | undefined
    try {
      const manifest = await peer.start(token)
      const chat = manifest.conversation_id
      const r = peer.registration
      const initial = await peer.control('counts')
      observe('fresh database', initial)
      expect(initial).toMatchObject({
        admissions: [],
        requests: [],
        decisions: 0,
        permits: 0,
        launches: 0,
        cancel_causes: 0,
        executor_cancels: 0
      })
      ipc.window.mockReturnValue({})
      registerSymphonyRoute(
        () => 'file:///hermes/index.html',
        (_windowId, address) => address.connectionId === r.connectionId && address.profile === r.profile,
        peer.registry
      )
      const installWindow = (id: number) => {
        const host = Object.assign(new EventEmitter(), {
          id,
          mainFrame: {},
          getURL: () => 'file:///hermes/index.html#/' + encodeURIComponent(r.storedConversation),
          isDestroyed: () => host !== sender,
          send: (name: string, ...args: unknown[]) => signals.emit(name, {}, ...structuredClone(args))
        })
        sender = host
        const bridge = symphonyPlanBridge({
          invoke: async (name, ...args) => {
            const handle = ipc.handle.mock.calls.findLast(row => row[0] === name)?.[1]
            if (!handle) throw Error('Missing IPC handler')
            return structuredClone(
              await handle({ sender: host, senderFrame: host.mainFrame }, ...structuredClone(args))
            )
          },
          on: signals.on.bind(signals),
          removeListener: signals.removeListener.bind(signals)
        } as Parameters<typeof symphonyPlanBridge>[0])
        window.hermesDesktop = { ...previous, symphonyPlan: bridge } as typeof window.hermesDesktop
      }
      installWindow(1)
      $connection.set({ mode: 'remote', remoteKind: 'ssh', connectionId: r.connectionId, profile: r.profile } as never)
      $connectionsRegistry.set({ connections: [{ id: r.connectionId, kind: 'ssh' }] } as never)
      recordSessionEventScope({ session_id: chat, profile: r.profile, connectionId: r.connectionId })
      const frames = { probes: 0, submits: [] as string[], legacy: 0, receiptReads: 0, operatorWrites: 0 }
      const counts = () => peer.control('counts')
      const snapshot = async (name: string) => {
        const owner = await counts()
        observe(name, { frames, owner })
        return owner
      }
      expect(initial.ipc).toBeDefined()
      expect(initial.ordinary_turns).toBe(0)
      let dropAck = false
      const lostReply = new AbortController()
      type Ack = { submission_id: string; admission_id: string; ref: { session_id: string; profile_id: string } }
      const acks: Ack[] = []
      let dials = 0
      vi.stubGlobal(
        'WebSocket',
        taskWebSocket(async (url, protocols, receive) => {
          const requestById = new Map<number, { method: string; params: Record<string, unknown> }>()
          const address = new URL(url)
          expect(address.pathname).toBe('/api/ws')
          expect(address.searchParams.get('token') === token).toBe(true)
          expect([...address.searchParams.keys()]).toEqual(['token'])
          expect(protocols).toEqual([])
          const socket = await peer.openOrdinary('ordinary-' + ++dials, url, protocols, raw => {
            const reply = JSON.parse(raw)
            const sentFrame = requestById.get(reply.id)
            requestById.delete(reply.id)
            if (sentFrame?.method === 'prompt.forward_submit' && reply.result) {
              acks.push(reply.result)
              if (dropAck) {
                dropAck = false
                observe('dropped owner reply', { ack: reply.result, frames })
                lostReply.abort()
                return
              }
            }
            receive(raw)
          })
          observe('ordinary socket upgrade', { socket: dials, ...socket.upgrade })
          return {
            send: (raw: string) => {
              const frame = JSON.parse(raw)
              if (frame.method) requestById.set(frame.id, { method: frame.method, params: frame.params ?? {} })
              if (frame.method === 'prompt.forward_probe') frames.probes++
              if (frame.method === 'prompt.forward_submit') frames.submits.push(frame.params.submission_id)
              if (frame.method === 'prompt.submit') frames.legacy++
              if (frame.method === 'prompt.receipt') frames.receiptReads++
              if (['slash.exec', 'command.dispatch'].includes(frame.method)) frames.operatorWrites++
              return socket.send(raw)
            },
            close: () => (closed = socket.close())
          }
        })
      )
      const dialUrl = buildGatewayWsUrl('http://127.0.0.1', token)
      const resume = async (stored: string) => {
        const rpc = await ordinary.request<{ session_id: string; session_key?: string; resumed?: string; stored_session_id?: string }>(
          'session.resume', { session_id: stored, lazy: true }
        )
        const durable = rpc.session_key ?? rpc.resumed ?? rpc.stored_session_id
        expect(durable).toBe(stored)
        expect(rpc.session_id).toBeTruthy()
        return rpc.session_id
      }
      const request: GatewayRequest = (method, params, timeout) =>
        ordinary.request(method, params, timeout, method === 'prompt.forward_submit' && dropAck ? lostReply.signal : undefined)
      ipc.routed.mockImplementation((connection, profile, method, params) => {
        expect([connection, profile]).toEqual([r.connectionId, r.profile])
        return request(method, params)
      })
      await ordinary.connect(dialUrl)
      const ordinaryChat = manifest.ordinary_session_id
      expect(ordinaryChat).toBeTruthy()
      const ordinaryRuntime = await resume(ordinaryChat)
      ui = openChat(ordinaryChat, request, '/', false, ordinaryRuntime)
      await ui.enter('Hold fresh draft ' + campaign)
      await waitFor(() => expect(ui!.editorText()).toBe('Hold fresh draft ' + campaign), wait)
      expect(frames).toMatchObject({ probes: 0, submits: [], legacy: 0 })
      const fresh = await snapshot('new chat held before text')
      expect(fresh.admissions).toHaveLength(0)
      expect(fresh.ipc['forward-submit'].entered).toBe(0)
      ui.unmount()
      ui = openChat(ordinaryChat, request, sessionRoute(ordinaryChat), false, ordinaryRuntime)
      await ui.enter('Ordinary fixture ' + campaign)
      await within(ui.container).findByText(/Квитанция постановки в Symphony недоступна/, {}, wait)
      await waitFor(async () => expect((await counts()).ordinary_turns).toBe(1), wait)
      const ordinaryCounts = await snapshot('verified ordinary chat')
      expect(ordinaryCounts.admissions).toHaveLength(0)
      expect(frames).toMatchObject({ probes: 1, legacy: 1, submits: [] })
      ui.unmount()

      const runtime = await resume(chat)
      recordSessionEventScope({ session_id: runtime, profile: r.profile, connectionId: r.connectionId })
      await peer.control('busy', { action: 'arm' })
      ui = openChat(chat, request, sessionRoute(chat), false, runtime)
      const accepted = async (i: number) => {
        await waitFor(() => expect(acks).toHaveLength(i + 1), wait)
        const ack = acks[i]
        expect(ack.ref).toEqual({ profile_id: manifest.profile_id, session_id: chat })
        expect(ack.submission_id).toBe(frames.submits[i])
        expect(ack.admission_id).toBeTruthy()
        expect(ack.admission_id).not.toBe(ack.submission_id)
        const visible = await within(ui!.container).findByText('Hermes принял сообщение · admission_id: ' + ack.admission_id, {}, wait)
        expect(isInaccessible(visible)).toBe(false)
        return ack
      }
      await ui.enter('Inspect fixture one ' + campaign)
      const first = await accepted(0)
      await waitFor(async () => expect(await peer.control('busy', { action: 'state' })).toMatchObject({
        state: 'occupied', admission_id: first.admission_id
      }), wait)
      const beforeSecond = await snapshot('first admission occupies owner')
      expect(beforeSecond.admissions).toHaveLength(1)
      expect(beforeSecond.admissions).toContainEqual(expect.objectContaining({
        request_id: first.submission_id, admission_id: first.admission_id
      }))
      ui.unmount()
      ui = openChat(chat, request, sessionRoute(chat), true, runtime)
      await ui.enter('Inspect fixture two ' + campaign)
      const second = await accepted(1)
      const busyCounts = await snapshot('second admission while occupied')
      expect(busyCounts.admissions).toHaveLength(2)
      for (const ack of [first, second]) {
        expect(busyCounts.admissions).toContainEqual(expect.objectContaining({
          request_id: ack.submission_id, admission_id: ack.admission_id
        }))
      }
      expect(new Set(frames.submits).size).toBe(2)
      expect(busyCounts.ipc['forward-submit']).toMatchObject({ entered: 2, returned: 2, conflicts: 0 })
      await peer.control('busy', { action: 'release', admission_id: first.admission_id })
      await waitFor(async () => expect((await counts()).requests).toHaveLength(2), wait)
      const waiting = await snapshot('Symphony request after owner admission')
      for (const ack of [first, second]) {
        const matching = waiting.requests.filter((row: { admission_id: string }) => row.admission_id === ack.admission_id)
        expect(matching).toMatchObject([{ request_id: 'task-' + ack.admission_id }])
      }
      expect(waiting).toMatchObject({ permits: 0, launches: 0 })
      expect(second.admission_id).not.toBe(first.admission_id)
      const oldSelector = await ordinary.request<{ admission_id?: string; ref?: Ack['ref'] }>('prompt.receipt', {
        session_id: runtime, admission_id: first.admission_id
      }).then(row => ({ available: true, matches: row?.admission_id === first.admission_id &&
        row.ref?.profile_id === first.ref.profile_id && row.ref.session_id === first.ref.session_id }),
        error => {
          if (error?.code !== -32601) throw error
          return { available: false, matches: false, code: error.code }
        })
      if (oldSelector.available) expect(oldSelector.matches).toBe(true)
      observe('existing admission selector on shared standalone', oldSelector)

      ui.unmount()
      ui = openChat(chat, request, sessionRoute(chat), false, runtime)
      const plan = await within(ui.container).findByRole('region', { name: 'План Symphony' }, wait)
      const digest = (await within(plan).findByText(/^SHA-256 /, {}, wait)).textContent!.slice(8)
      const shown = (await counts()).plan_inputs.find((item: { plan_digest: string }) => item.plan_digest === digest)
      expect(shown).toBeDefined()
      const source = [first, second].find(ack => ack.admission_id === shown.admission_id)
      expect(source).toBeDefined()
      expect(shown.request_id).toBe('task-' + source!.admission_id)
      await waitForReadyPlan(ui.container, shown.request_id, digest)
      observe('ready for fresh text', { request_id: shown.request_id, plan_digest: digest, admission_id: source!.admission_id })
      let rendered = false
      try {
        await ui.enter('отклоняю')
        await within(ui.container).findByText('План отклонён.', {}, wait)
        rendered = true
      } finally {
        observe('decision UI observation', { rendered, request_id: shown.request_id, plan_digest: digest,
          plan_verbs: peer.reads, authenticated_transports: peer.transports.length, open_plan_sockets: peer.openPlanSockets, frames })
        const owner = await counts().catch(() => undefined)
        observe('independent decision readback', { available: Boolean(owner), request_id: shown.request_id,
          decision: owner?.by_request[shown.request_id] ?? null, decision_count: owner?.decisions ?? null,
          permits: owner?.permits ?? null, launches: owner?.launches ?? null })
      }
      const denied = await snapshot('shown plan denied')
      expect(denied.by_request[shown.request_id]).toMatchObject({ decision: 'DENIED', plan_digest: digest })
      observe('decision for the displayed admission', { submission_id: source!.submission_id,
        admission_id: source!.admission_id, request_id: shown.request_id, plan_digest: digest,
        decision: denied.by_request[shown.request_id] })
      for (const verb of ['status', 'stop']) {
        for (const job of ['own', 'foreign']) {
          const count = ui.messages().length
          await ui.enter('/symphony ' + verb + ' ' + job)
          await waitFor(() => expect(JSON.stringify(ui!.messages().slice(count))).toContain('HOLD'), wait)
        }
      }
      const lostText = 'Inspect lost ACK ' + campaign
      dropAck = true
      await ui.enter(lostText)
      await waitFor(() => expect(ui!.messages().some(row => row.error?.includes('UNKNOWN'))).toBe(true), wait)
      const lost = frames.submits[2]
      const committed = await snapshot('after lost ACK')
      expect(committed.admissions).toContainEqual(expect.objectContaining({ request_id: lost, admission_id: acks[2].admission_id }))
      const priorFrames = structuredClone(frames)
      ui.unmount()
      sender!.emit('destroyed')
      ordinary.close()
      await closed
      await ordinary.connect(dialUrl)
      const rebound = await resume(chat)
      recordSessionEventScope({ session_id: rebound, profile: r.profile, connectionId: r.connectionId })
      installWindow(2)
      ui = openChat(chat, request, sessionRoute(chat), false, rebound)
      await waitFor(() => expect(ui!.editorText()).toBe(lostText), wait)
      await ui.enter()
      await waitFor(() => expect(ui!.editorText()).toBe(lostText), wait)
      expect(Object.values(storedJournal())).toContainEqual(expect.objectContaining({ id: lost, forwardSessionId: chat }))
      const final = await snapshot('reopened shared input held without another submission')
      expect(frames).toStrictEqual(priorFrames)
      expect(frames).toMatchObject({ probes: 4, legacy: 1, operatorWrites: 0, receiptReads: 1 })
      expect(frames.submits).toHaveLength(3)
      expect(final.admissions).toHaveLength(3)
      expect(final.ipc['forward-submit']).toMatchObject({ entered: 3, returned: 3, conflicts: 0 })
      expect(final.ipc['forward-probe'].entered).toBeGreaterThanOrEqual(frames.probes)
      expect(final).toMatchObject({ permits: 0, launches: 0, executor_cancels: 0 })
      receipt.state = 'FORWARD_OBSERVED_SHARED_RECOVERY_HOLD'
    } catch (error) {
      receipt.state = 'INCOMPLETE'
      observe('failure', { kind: error instanceof Error ? error.name : 'error' })
      throw error
    } finally {
      save()
      ui?.unmount()
      sender?.emit('destroyed')
      window.hermesDesktop = previous
      $connection.set(null)
      $connectionsRegistry.set(null)
      ordinary.close()
      await closed
      vi.unstubAllGlobals()
      await peer.shutdown()
      const body = JSON.stringify(receipt, null, 2) + '\n'
      writeFileSync(
        path.join(dir, 'joint-forward.sha256'),
        createHash('sha256').update(body).digest('hex') + '  joint-forward.json\n',
        { flag: 'wx' }
      )
    }
  },
  240000
)
