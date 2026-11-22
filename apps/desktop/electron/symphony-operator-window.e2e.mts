import assert from 'node:assert/strict'
import { generateKeyPairSync } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import { app, BrowserWindow, safeStorage } from 'electron'

import { registerSymphonyOperator } from './register-symphony-operator'
import type { OperatorReply } from './symphony-operator-types'
import type { PlanEnvironment, PlanRegistration } from './symphony-route'

const root = process.env.SYMPHONY_OPERATOR_E2E_DIR
assert(root, 'Set SYMPHONY_OPERATOR_E2E_DIR to a fresh fixture directory')
const page = path.join(root, 'chat.html')
const renderer = pathToFileURL(page).href
const profileDir = path.join(root, 'userdata')
const trace: string[] = []
const registration: PlanRegistration = {
  connectionId: 'fixture', profile: 'profile', conversation: 'chat', storedConversation: 'chat',
  kind: 'ssh', installId: 'fixture', leaseId: 'local', bootId: 'fixture',
  hostKey: 'ssh-ed25519 ' + Buffer.concat([Buffer.from([0, 0, 0, 11]), Buffer.from('ssh-ed25519'), Buffer.alloc(32, 3)]).toString('base64'),
  canonicalProfile: 'canonical', namespace: 'ns', operator: 'fixture', session: 'plan', generation: '0',
  clientId: 'plan-key', signingKey: generateKeyPairSync('ed25519').privateKey
}
const ref = { profile_id: registration.canonicalProfile, session_id: registration.conversation }
const address = { connectionId: 'fixture', profile: 'profile', conversation: 'chat' }
const submissionId = 'initial-input'
const receipt = { ref, submission_id: submissionId, admission_id: 'admitted-input', status: 'queued' }
const attemptId = 'fixture-attempt'
let originalStop: string | undefined
let closedStop = false
let generation = 0
let sequence = 0
let established = 0
const environment: PlanEnvironment = {
  registration(target) {
    if (Object.entries(address).every(([name, value]) => target[name] === value)) return registration
  },
  async observe(_target, attempt) { return { attempt, ...registration } },
  async attach() {
    const socketId = ++sequence
    let connected = true
    return {
      peerHostKey: registration.hostKey,
      close() { connected = false },
      async open(url, protocol) {
        assert.equal(url, '/api/ws')
        assert.equal(protocol, '')
        let expectedAction: string
        return {
          protocol, get open() { return connected }, close() { connected = false },
          async request(method, params: any) {
            assert(connected)
            if (method === 'session.resume') {
              assert.deepEqual(params, { session_id: 'chat', lazy: true })
              return { session_id: 'runtime', session_key: 'chat' }
            }
            assert.equal(params.session_id, 'runtime')
            if (method === 'prompt.forward_probe') return { ref, version: 1, mode: 'personal', route: 'forward', generation: 'gen' }
            if (method === 'operator.challenge') {
              expectedAction = params.action
              const frame = ['hermes.desktop.operator', 1, expectedAction, 'test-owner', 'gen', `socket-${socketId}`,
                ref.profile_id, ref.session_id, params.submission_id, params.installation_key, params.window_key,
                params.window_generation, 'nonce', params.payload_sha256, Date.now() + 10_000]
              return { ref, version: 1, challenge_id: 'nonce', window_generation: params.window_generation,
                message: Buffer.from(JSON.stringify(frame)).toString('base64') }
            }
            assert.equal(method, 'operator.execute')
            trace.push(expectedAction)
            if (expectedAction === 'submit' || expectedAction === 'recover') {
              generation++
              established = socketId
            }
            assert.equal(socketId, established)
            if (expectedAction === 'submit') throw new Error('synthetic lost ACK')
            if (expectedAction === 'recover') return { ...receipt, window_generation: generation }
            if (expectedAction === 'receipt') return { ...receipt, attempt: { attempt_id: attemptId } }
            const { attempt_id, command_id, reason } = params.payload
            assert.equal(attempt_id, attemptId)
            if (expectedAction === 'status') return { attempt_id, phase: 'RUNNING', observed_at: 3 }
            if (expectedAction === 'stop') {
              originalStop = command_id
              throw new Error('synthetic lost STOP before receipt')
            }
            assert.equal(command_id, originalStop)
            if (expectedAction === 'stop_reconcile') {
              assert.equal(reason, 'owner_requested')
              closedStop = true
            } else {
              assert.equal(expectedAction, 'stop_result')
              assert(closedStop)
            }
            return { attempt_id, command_id, state: 'NOT_ACCEPTED', dispatched: false, local_stopped: false }
          }
        }
      }
    }
  }
}

async function exercise() {
  await app.whenReady()
  assert(safeStorage.isEncryptionAvailable(), 'Windows protected storage is unavailable')
  const managed = new Set<BrowserWindow>()
  const createWindow = () => {
    const window = new BrowserWindow({ show: false, webPreferences: {
      preload: path.join(root, 'preload.cjs'), sandbox: true, contextIsolation: true, nodeIntegration: false
    } })
    managed.add(window)
    const loaded = window.loadURL(renderer + '#/chat')
    return { window, loaded }
  }
  let restored: ReturnType<typeof createWindow> | undefined
  let prompts = 0
  const controller = registerSymphonyOperator({
    rendererUrl: () => renderer, matchesWindow: window => managed.has(window), environment,
    createWindow: () => { restored = createWindow(); return restored.window },
    showMessageBox: async () => { prompts++; return { response: 1, checkboxChecked: false } }
  })
  const a = createWindow()
  const b = createWindow()
  await Promise.all([a.loaded, b.loaded])
  const call = (target: BrowserWindow, input: object): Promise<OperatorReply> =>
    target.webContents.executeJavaScript(`operatorFixture.request(${JSON.stringify(address)}, ${JSON.stringify(input)})`)
  const read = { action: 'receipt', submissionId }
  const text = { action: 'submit', submissionId, text: 'Synthetic protected input' }
  assert.deepEqual(await call(a.window, text), { state: 'UNKNOWN' })
  assert.deepEqual(await call(b.window, read), { state: 'HOLD' })
  assert.deepEqual(await call(b.window, { action: 'stop', attemptId: 'known-attempt' }), { state: 'HOLD' })
  await a.window.webContents.executeJavaScript('window.cleared = 0; operatorFixture.onClear(() => window.cleared++); void 0')
  await controller.restore(b.window)
  assert(restored)
  await restored.loaded
  assert.equal(await a.window.webContents.executeJavaScript('window.cleared'), 1)
  assert.deepEqual(await call(a.window, read), { state: 'HOLD' })
  assert.deepEqual(await call(b.window, read), { state: 'HOLD' })
  assert.deepEqual(await call(restored.window, read), {
    state: 'ADMITTED', receipt: { admission_id: receipt.admission_id, submission_id: submissionId, status: 'queued' }
  })
  assert.deepEqual(await call(restored.window, { action: 'stop', attemptId }), { state: 'UNKNOWN' })
  assert(originalStop)
  assert.deepEqual(await call(b.window, { action: 'stop-reconcile', attemptId }), { state: 'HOLD' })
  assert.deepEqual(await call(restored.window, { action: 'stop-reconcile', attemptId }), {
    state: 'STOP', attemptId, commandId: originalStop, observation: 'NOT_ACCEPTED', dispatched: false, localStopped: false
  })
  assert(!restored.window.isFocused())
  assert.deepEqual(await call(restored.window, { action: 'stop-replace', attemptId }), { state: 'HOLD' })
  assert.equal(prompts, 1)
  assert.deepEqual(trace, ['submit', 'recover', 'receipt', 'stop', 'stop_reconcile', 'stop_result', 'status'])
  const encrypted = fs.readFileSync(path.join(profileDir, 'symphony-operator-v1.bin'))
  assert(!encrypted.includes(Buffer.from('PRIVATE KEY')))
  assert(safeStorage.decryptString(encrypted).includes('PRIVATE KEY'))
  return { state: 'PASS', real_windows: managed.size, generation, wire_actions: trace,
    background_replace_held: true, windows_protected_storage: true }
}

fs.mkdirSync(profileDir, { recursive: true })
fs.writeFileSync(page, '<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src \'none\'"><title>Operator fixture</title>')
app.setPath('userData', profileDir)
app.disableHardwareAcceleration()
app.commandLine.appendSwitch('disable-background-networking')
app.commandLine.appendSwitch('disable-component-update')
void exercise().then(result => {
  fs.writeFileSync(path.join(root, 'result.json'), JSON.stringify(result, null, 2) + '\n')
  app.exit(0)
}, (error: Error) => {
  fs.writeFileSync(path.join(root, 'result.json'), JSON.stringify({ state: 'FAIL', error: error.message, wire_actions: trace }, null, 2) + '\n')
  app.exit(1)
})
