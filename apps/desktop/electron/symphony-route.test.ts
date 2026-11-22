import { createHash, createPrivateKey, generateKeyPairSync, sign, verify } from 'node:crypto'
import { EventEmitter } from 'node:events'
import { expect, it, vi } from 'vitest'
import { registerSymphonyRoute } from './register-symphony-route'
import vector from './plan-route-v1.fixture.json'
import { planSigningBytes } from './symphony-route'
import type { PlanEnvironment, PlanRegistration } from './symphony-route'
import { SymphonyRouteClient } from './symphony-route-client'
import { PlanRouteRegistry } from './symphony-route-registry'
import { planSocket } from './symphony-route-socket'

it('matches the shared v1 signing bytes and Ed25519 signature', () => {
  const seed = Buffer.from(Array.from({ length: 32 }, (_, i) => i))
  const key = createPrivateKey({
    type: 'pkcs8',
    format: 'der',
    key: Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), seed])
  })
  const bytes = planSigningBytes(vector.fields)
  expect(createHash('sha256').update(bytes).digest('hex')).toBe(vector.sha256)
  expect(sign(null, bytes, key).toString('base64')).toBe(vector.signature)
})

const electron = vi.hoisted(() => ({ handle: vi.fn() }))
vi.mock('electron', () => ({ ipcMain: electron, BrowserWindow: { fromWebContents: () => ({}) } }))

function fixture(overrides: Partial<PlanRegistration> = {}, at: (stage: string) => void = () => {}) {
  const pair = generateKeyPairSync('ed25519')
  const hostBlob = Buffer.concat([Buffer.from('0000000b7373682d6564323535313900000020', 'hex'), Buffer.alloc(32, 17)])
  const fingerprint = 'SHA256:' + createHash('sha256').update(hostBlob).digest('base64').replace(/=+$/, '')
  const reg: PlanRegistration = {
    kind: 'ssh',
    connectionId: 'office',
    profile: 'work',
    conversation: 'chat',
    storedConversation: 'stored-chat',
    installId: 'install',
    leaseId: 'lease',
    bootId: 'boot',
    hostKey: 'ssh-ed25519 ' + hostBlob.toString('base64'),
    canonicalProfile: '/srv/work',
    namespace: 'team',
    operator: 'fixture',
    session: 'journal-session',
    generation: 'generation',
    clientId: 'desktop',
    signingKey: pair.privateKey,
    ...overrides
  }
  let defect = ''
  let frames = 0
  let dials = 0
  let attempt = ''
  const events: string[] = []
  const calls: string[] = []
  const transport: Pick<PlanEnvironment, 'observe' | 'attach'> = {
    async observe(_address, fresh) {
      at('observe')
      if (defect === 'missing') throw Error('K1 absent')
      if (defect !== 'cached') attempt = fresh
      return { ...reg, attempt, bootId: defect === 'reboot' ? 'new-boot' : reg.bootId }
    },
    async attach() {
      ++dials
      let disconnect: (() => void) | undefined
      return {
        peerHostKey: defect === 'peer' ? 'different' : reg.hostKey,
        async open(path, protocol, receive, closed) {
          expect(path).toBe('/api/ws')
          const fields = [
            'symphony-plan',
            '1',
            'nonce',
            'install',
            fingerprint,
            '/srv/work',
            'team',
            'fixture',
            'chat',
            reg.session,
            'socket-' + dials,
            reg.generation,
            reg.connectionId,
            reg.clientId
          ]
          return planSocket(
            {
              protocol,
              listen(frame, end) {
                disconnect = end
                deliver = frame
                return () => {
                  disconnect = undefined
                }
              },
              send(raw) {
                ++frames
                const rpc = JSON.parse(raw)
                at(rpc.method)
                let result: unknown
                if (rpc.method === 'route.challenge') {
                  result = { fields: fields.map((v, i) => (defect === 'conversation' && i === 8 ? 'neighbour' : v)) }
                } else if (rpc.method === 'route.authenticate') {
                  expect(
                    verify(
                      null,
                      Buffer.from(JSON.stringify(fields)),
                      pair.publicKey,
                      Buffer.from(rpc.params.signature, 'base64')
                    )
                  ).toBe(true)
                  result = {
                    state: 'READY',
                    transport_id: fields[10],
                    session_id: reg.session,
                    conversation_id: reg.conversation
                  }
                } else {
                  calls.push(rpc.params.arg)
                  if (defect === 'send') throw Error('wire send interrupted')
                  if (defect === 'silent') return
                  if (defect === 'lost') {
                    disconnect?.()
                    return
                  }
                  result = { output: '{}' }
                }
                deliver(JSON.stringify({ jsonrpc: '2.0', id: rpc.id, result }))
              },
              close() {
                disconnect?.()
              }
            },
            receive,
            closed
          )
        },
        close() {
          disconnect?.()
        }
      }
    }
  }
  let deliver: (raw: string) => void = () => {}
  const registry = new PlanRouteRegistry()
  registry.install(reg, transport)
  const client = new SymphonyRouteClient(registry, (_address, closed) => events.push(closed ? 'closed' : 'wake'))
  return {
    reg,
    client,
    registry,
    calls,
    events,
    defect: (value: string) => {
      defect = value
    },
    dials: () => dials,
    frames: () => frames,
    event: (session: string) =>
      deliver(
        JSON.stringify({
          jsonrpc: '2.0',
          method: 'event',
          params: { type: 'symphony.plan', session_id: session, payload: {} }
        })
      )
  }
}

it.each(['missing', 'cached', 'reboot', 'peer', 'conversation'])(
  'withholds plan data for %s evidence or address',
  async defect => {
    const stand = fixture()
    stand.defect(defect)
    await expect(stand.client.request(stand.reg, 'status')).rejects.toThrow()
    expect(stand.calls).toEqual([])
    expect(stand.frames()).toBe(defect === 'conversation' ? 1 : 0)
    expect(stand.dials()).toBe(['peer', 'conversation'].includes(defect) ? 1 : 0)
  }
)

it('routes a verified socket and opens another only for readback after a lost decision', async () => {
  const stand = fixture()
  await stand.client.request(stand.reg, 'status')
  stand.event('neighbour')
  stand.event('chat')
  expect(stand.events).toEqual(['wake'])
  stand.defect('lost')
  await expect(stand.client.request(stand.reg, 'plan-answer', { input_id: 'original' })).rejects.toThrow()
  expect(stand.events).toEqual(['wake', 'closed'])
  await expect(stand.client.request(stand.reg, 'plan-answer', { input_id: 'original' })).rejects.toThrow()
  stand.defect('')
  await stand.client.request(stand.reg, 'plan-receipt', { input_id: 'original' })
  expect(stand.calls).toEqual(['status', 'plan-answer {"input_id":"original"}', 'plan-receipt {"input_id":"original"}'])
  expect(stand.dials()).toBe(2)
  stand.registry.revoke(stand.reg)
  await expect(stand.client.request(stand.reg, 'status')).rejects.toThrow()
  expect(stand.events.at(-1)).toBe('closed')
})

it.each(['leaseId', 'bootId'] as const)('requires a nonempty registered %s before opening a tunnel', async field => {
  const s = fixture({ [field]: '' })
  await expect(s.client.request(s.reg, 'status')).rejects.toThrow()
  expect(s.dials()).toBe(0)
})

it.each(['silent', 'send'])('a %s transport failure invalidates the socket and allows fresh readback', async fault => {
  vi.useFakeTimers()
  try {
    const s = fixture()
    await s.client.request(s.reg, 'status')
    s.defect(fault)
    const failure = expect(s.client.request(s.reg, 'plan-answer', { input_id: 'first' })).rejects.toThrow()
    await vi.advanceTimersByTimeAsync(60001)
    await failure
    await expect(s.client.request(s.reg, 'plan-answer', { input_id: 'first' })).rejects.toThrow()
    s.defect('')
    await s.client.request(s.reg, 'plan-receipt', { input_id: 'first' })
    expect(s.dials()).toBe(2)
    expect(s.calls.filter(v => v.startsWith('plan-answer'))).toHaveLength(1)
  } finally {
    vi.useRealTimers()
  }
})

it.each(['observe', 'route.challenge'])(
  'a same-profile chat change during %s retires that window grant',
  async stage => {
    const page = 'file:///hermes/index.html'
    const chat = page + '#/stored-chat'
    let url = chat
    let changed = false
    const sender = Object.assign(new EventEmitter(), {
      id: 1,
      mainFrame: {},
      getURL: () => url,
      isDestroyed: () => false,
      send: vi.fn()
    })
    const s = fixture({}, point => {
      if (point !== stage || changed) return
      changed = true
      url = page + '#/another-chat'
      sender.emit('did-start-navigation', { isMainFrame: true, isSameDocument: true, url })
      // Returning to the same URL cannot revive an already-started request.
      url = chat
      sender.emit('did-navigate-in-page', {}, url, true)
    })
    registerSymphonyRoute(
      () => page,
      () => true,
      s.registry
    )
    const invoke = electron.handle.mock.calls.at(-1)![1]
    const status = () => invoke({ sender, senderFrame: sender.mainFrame }, s.reg, 'status')
    await expect(status()).rejects.toThrow()
    expect(s.calls).toEqual([])
    expect(s.frames()).toBe(stage === 'observe' ? 0 : 1)
    url = page + '#/another-chat'
    await expect(status()).rejects.toThrow()
    url = chat
    expect(await status()).toEqual({ output: '{}' })
    expect(s.calls).toEqual(['status'])
    sender.emit('destroyed')
  }
)

it('retains a display across a query or fragment and identifies refusal before sending', async () => {
  const page = 'file:///hermes/index.html'
  let url = page + '#/stored-chat'
  const sender = Object.assign(new EventEmitter(), {
    id: 2,
    mainFrame: {},
    getURL: () => url,
    isDestroyed: () => false,
    send: vi.fn()
  })
  const s = fixture()
  registerSymphonyRoute(
    () => page,
    () => true,
    s.registry
  )
  const invoke = electron.handle.mock.calls.at(-1)![1]
  const ask = (verb: string) => invoke({ sender, senderFrame: sender.mainFrame }, s.reg, verb)
  const move = (next: string) => {
    sender.emit('did-start-navigation', { isMainFrame: true, isSameDocument: true, url: next })
    url = next
    sender.emit('did-navigate-in-page', {}, url, true)
  }
  await ask('status')
  await ask('plan-presented')
  move(page + '#/stored-chat?tab=plan')
  move(page + '#/stored-chat#position')
  await expect(ask('plan-answer')).resolves.toEqual({ output: '{}' })
  expect(s.dials()).toBe(1)
  move(page + '#/other')
  move(page + '#/stored-chat')
  await expect(ask('plan-answer')).resolves.toMatchObject({ delivery: 'NOT_SENT' })
  expect(s.calls).toEqual(['status', 'plan-presented', 'plan-answer'])
  await ask('plan-receipt')
  s.defect('send')
  await expect(ask('plan-answer')).rejects.toThrow()
  expect(s.dials()).toBe(2)
  sender.emit('destroyed')
})
