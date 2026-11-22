import { createHash, createPublicKey, verify } from 'node:crypto'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, test, vi } from 'vitest'

import { SymphonyOperatorClient } from './symphony-operator-client'
import type { OperatorCall, OperatorCheckedPatch, OperatorPatchCommand } from './symphony-operator-types'
import { SymphonyOperatorJournal } from './symphony-operator-journal'
import vector from './symphony-operator-v1.fixture.json'
import stopVectors from './symphony-operator-stop.fixture.json'
import { operatorPayloadHash, type OperatorChannel } from './symphony-operator-wire'
import { OperatorRouteRefused, planSocket } from './symphony-route-socket'

const address = { connectionId: 'ssh', profile: 'work', conversation: 'chat', storedConversation: 'chat' }
const ref = { profile_id: 'canonical-profile', session_id: 'chat' }
const submissionId = 'submission-a'
const admitted = { ref, submission_id: submissionId, admission_id: 'admission-a', status: 'started' }
const attemptId = 'attempt-a'
const job = 'job-a'
const patchSha256 = 'a'.repeat(64)
const patchCommit = 'c'.repeat(40)

function publicKey(encoded: string) {
  return createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: Buffer.from(encoded, 'base64').toString('base64url') }, format: 'jwk' })
}

const dirs: string[] = []
afterEach(() => {
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true })
})

function fixture(stopRevoked = false) {
  const actions: string[] = []
  const attempts = new Map([[submissionId, { id: attemptId, admission: admitted.admission_id, phase: 'RUNNING' }]])
  const bindings = new Map<string, { generation: number; socket: number }>()
  const cancellations: string[] = []
  const patchWrites = new Map<string, object>()
  let patchCheck = 19
  const background = new Set<object>()
  const requests: { action: string; submission: string; attempt?: string; command?: string; previous?: string }[] = []
  const commands = new Map<string, { attempt_id: string; command_id: string; state: string; reason: string;
    dispatched: boolean; local_stopped: boolean; previous_command_id?: string }>()
  const dir = mkdtempSync(path.join(os.tmpdir(), 'operator-'))
  dirs.push(dir)
  let storageAvailable = true
  const storage = {
    isEncryptionAvailable: () => storageAvailable,
    encryptString: (value: string) => Buffer.from(Buffer.from(value).reverse()),
    decryptString: (bytes: Buffer) => Buffer.from(bytes).reverse().toString()
  }
  const journal = new SymphonyOperatorJournal(dir, storage)
  let nextSocket = 0
  let stopObservable = true
  let mayRead = true
  const lose = new Set<string>()
  const dropBefore = new Set<string>()
  const unsupported = new Set<string>()
  let challengeChanged = false

  const open = async (): Promise<OperatorChannel> => {
    const transport = ++nextSocket
    let ready = true
    let pending: unknown[] | undefined
    return {
      sessionId: 'runtime', ref, forwardGeneration: 'forward-gen',
      socket: {
        protocol: '', get open() { return ready }, close() { ready = false },
        async request(method, params: any) {
          expect(params.session_id).toBe('runtime')
          if (method === 'operator.challenge') {
            if (unsupported.has(params.action)) throw new Error('unsupported action')
            pending = ['hermes.desktop.operator', 1, params.action, 'owner', challengeChanged ? 'another-generation' : 'forward-gen', `socket-${transport}`,
              ref.profile_id, ref.session_id, params.submission_id, params.installation_key, params.window_key,
              params.window_generation, 'nonce', params.payload_sha256, Date.now() + 10_000]
            return { version: 1, challenge_id: 'nonce', ref, window_generation: params.window_generation,
              message: Buffer.from(JSON.stringify(pending)).toString('base64') }
          }
          expect(method).toBe('operator.execute')
          expect(pending).toBeDefined()
          const message = Buffer.from(JSON.stringify(pending))
          const signatureValid = (field: number, signature: string) =>
            verify(null, message, publicKey(String(pending![field])), Buffer.from(signature, 'base64'))
          expect(signatureValid(9, params.installation_signature)).toBe(true)
          expect(signatureValid(10, params.window_signature)).toBe(true)
          const { text, attempt_id, command_id, previous_command_id, reason, capture_seq, check_seq, patch_sha256 } = params.payload
          const action = String(pending![2])
          const boundFields: Record<string, unknown[]> = {
            stop_reconcile: [attempt_id, command_id, reason],
            stop_replace: [attempt_id, previous_command_id, command_id, reason],
            apply_info: [attempt_id],
            apply: [attempt_id, params.payload.job, capture_seq, check_seq, patch_sha256, command_id],
            archive: [attempt_id, params.payload.job, capture_seq, check_seq, patch_sha256, command_id],
            apply_result: [attempt_id, command_id],
          }
          const values = boundFields[action] ? [action, ...boundFields[action]]
            : [text ?? null, attempt_id ?? null, command_id ?? null, reason ?? null]
          expect(pending![13]).toBe(createHash('sha256').update(JSON.stringify(values)).digest('hex'))
          const submission = String(pending![8])
          const binding = bindings.get(submission) ?? { generation: 0, socket: 0 }
          const attempt = attempts.get(submission)!
          expect(pending![11]).toBe(binding.generation)
          pending = undefined
          actions.push(action)
          requests.push({ action, submission, attempt: attempt_id, command: command_id, previous: previous_command_id })
          if (dropBefore.delete(action)) throw new Error('lost before fixture handler')
          if (action === 'submit' || action === 'recover') {
            binding.generation++
            binding.socket = transport
            bindings.set(submission, binding)
          }
          if (binding.socket !== transport) throw new Error('wrong socket')
          const matches = attempt_id === attempt.id
          if ((action === 'stop' || action === 'stop_replace') && (!matches || attempt.phase !== 'RUNNING')) {
            throw new OperatorRouteRefused('fixture holds inactive attempt')
          }
          if (action === 'stop_replace' && commands.get(previous_command_id)?.state !== 'NOT_ACCEPTED') {
            throw new OperatorRouteRefused('fixture requires closed STOP')
          }
          if (action === 'stop' || action === 'stop_replace') {
            commands.set(command_id, { attempt_id, command_id, reason, state: stopRevoked ? 'REVOKED_BEFORE_CANCEL' : 'CANCEL_REQUESTED',
              dispatched: !stopRevoked, local_stopped: false, ...(previous_command_id && { previous_command_id }) })
            if (!stopRevoked) cancellations.push(command_id)
          }
          if (action === 'stop_reconcile' && matches && !commands.has(command_id)) {
            commands.set(command_id, { attempt_id, command_id, reason, state: 'NOT_ACCEPTED', dispatched: false, local_stopped: false })
          }
          const checked = { attempt_id: attempt.id, job, capture_seq: 11, check_seq: patchCheck, patch_sha256: patchSha256 }
          if (action === 'apply' || action === 'archive') {
            expect(params.payload).toEqual({ ...checked, command_id })
            expect(patchWrites.has(command_id)).toBe(false)
            patchWrites.set(command_id, { ...checked, command_id, state: action === 'apply' ? 'APPLIED' : 'ARCHIVED',
              ...(action === 'apply' && { commit: patchCommit }) })
          }
          if (lose.delete(action)) {
            if (action === 'submit') ready = false
            throw new Error('lost reply')
          }
          const admission = { ...admitted, submission_id: submission, admission_id: attempt.admission }
          if (action === 'submit') return admission
          if (action === 'recover') return { ref, submission_id: submission, window_generation: binding.generation }
          if (action === 'receipt' && mayRead) return { ...admission, attempt: { attempt_id: attempt.id, job } }
          if (action === 'apply_info' && matches) return { ...checked, project: 'project-a' }
          if ((action === 'apply' || action === 'archive' || action === 'apply_result') && matches) {
            return patchWrites.get(command_id) ?? { attempt_id, command_id, state: 'UNKNOWN' }
          }
          if (action === 'status' && matches) return { attempt_id, phase: attempt.phase, observed_at: 3 }
          if (action.startsWith('stop') && matches) {
            if (!stopObservable) return { attempt_id, command_id, state: 'UNKNOWN', dispatched: null, local_stopped: null }
            return commands.get(command_id) ?? { attempt_id, command_id, state: 'UNKNOWN' }
          }
          throw new Error('not observable')
        }
      }
    }
  }
  const confirm = vi.fn<(owner: object, target: { id: string; attemptId: string }) => Promise<boolean>>().mockResolvedValue(true)
  const confirmPatch = vi.fn<(owner: object, patch: OperatorCheckedPatch, action: OperatorPatchCommand['action']) => Promise<boolean>>()
    .mockResolvedValue(true)
  const createClient = () => new SymphonyOperatorClient<object>(new SymphonyOperatorJournal(dir, storage), open, confirm, {
    focused: owner => !background.has(owner), confirm: confirmPatch,
  })
  const client = createClient()
  return { journal, storage, dir, client, createClient, actions, requests, cancellations, commands, attempts, confirm, lose, dropBefore, unsupported,
    confirmPatch, patchWrites, background, bumpPatchCheck: () => { patchCheck++ },
    disableStorage: () => { storageAvailable = false },
    denyReceipt: () => { mayRead = false },
    changeChallenge: () => { challengeChanged = true },
    setStopObservable: (value: boolean) => { stopObservable = value },
    request: (owner: object, call: OperatorCall) => client.request(owner, address, () => true, call) }
}

describe('Symphony operator', () => {
  test('persists one confirmed patch command and reads its exact result after a lost reply and restart', async () => {
    const f = fixture()
    const owner = {}
    await f.request(owner, { action: 'submit', submissionId, text: 'checked patch fixture' })
    f.lose.add('apply')
    const call = { action: 'patch-decision', text: `/symphony apply ${job} ${patchSha256}` } as const
    const lost = await f.request(owner, call)
    expect(lost).toMatchObject({ state: 'PATCH', observation: 'UNKNOWN', command: { attemptId, job, patchSha256 } })
    if (lost.state !== 'PATCH') throw new Error('missing protected patch command')
    expect(f.confirmPatch).toHaveBeenCalledWith(owner, {
      attemptId, job, patchSha256, captureSeq: 11, checkSeq: 19,
    }, 'apply')
    expect(f.patchWrites.size).toBe(1)
    f.client.drop(owner)

    const restarted = f.createClient()
    const replacement = {}
    restarted.restore(replacement, restarted.recoverable()[0])
    const readback = { action: 'patch-decision', text: `/symphony apply-result ${job} ${patchSha256}` } as const
    const expected = { state: 'PATCH', observation: 'APPLIED', command: lost.command, commit: patchCommit }
    expect(await restarted.request(replacement, address, () => true, readback)).toEqual(expected)
    expect(await restarted.request(replacement, address, () => true, call)).toEqual(expected)
    expect(f.requests.filter(row => row.action === 'apply' || row.action === 'apply_result').map(row => [row.action, row.command]))
      .toEqual([['apply', lost.command.id], ['apply_result', lost.command.id], ['apply_result', lost.command.id]])
    expect(f.confirmPatch).toHaveBeenCalledTimes(1)
    expect(f.patchWrites.size).toBe(1)
  })

  test('holds background windows, mismatched patches and a changed CHECK before any apply', async () => {
    const f = fixture()
    const owner = {}
    const neighbor = {}
    const call = { action: 'patch-decision', text: `/symphony apply ${job} ${patchSha256}` } as const
    await f.request(owner, { action: 'submit', submissionId, text: 'checked patch fixture' })
    f.background.add(owner)
    expect(await f.request(owner, call)).toEqual({ state: 'HOLD' })
    expect(await f.request(neighbor, call)).toEqual({ state: 'HOLD' })
    expect(f.actions).toEqual(['submit'])
    f.background.delete(owner)

    expect(await f.request(owner, { ...call, text: `/symphony apply ${job} ${'b'.repeat(64)}` })).toEqual({ state: 'HOLD' })
    expect(f.confirmPatch).not.toHaveBeenCalled()
    f.confirmPatch.mockImplementationOnce(async () => {
      f.bumpPatchCheck()
      return true
    })
    expect(await f.request(owner, call)).toEqual({ state: 'HOLD' })
    f.confirmPatch.mockImplementationOnce(async () => {
      f.client.restore(neighbor, f.client.recoverable()[0])
      return true
    })
    expect(await f.request(owner, call)).toEqual({ state: 'HOLD' })
    expect(f.actions).not.toContain('apply')
    expect(f.journal.find(address, submissionId)?.patchCommands).toBeUndefined()
    expect(f.patchWrites.size).toBe(0)
  })

  test('distinguishes a framed owner HOLD from ambiguous transport errors', async () => {
    let receive!: (raw: string) => void
    let message = 'OPERATOR_HOLD'
    const socket = planSocket({
      protocol: '', close() {},
      listen(frame) { receive = frame; return () => undefined },
      send(raw) {
        const { id } = JSON.parse(raw)
        receive(JSON.stringify({ jsonrpc: '2.0', id, error: { code: 4090, message } }))
      },
    }, () => undefined, () => undefined)
    await expect(socket.request('operator.execute', {})).rejects.toBeInstanceOf(OperatorRouteRefused)
    message = 'admission_conflict'
    await expect(socket.request('operator.execute', {})).rejects.not.toBeInstanceOf(OperatorRouteRefused)
    socket.close()
  })

  test('verifies the agreed Unicode wire bytes and both raw Ed25519 signatures', () => {
    const bytes = Buffer.from(vector.message, 'base64')
    expect(bytes.toString()).toBe(JSON.stringify(vector.fields))
    expect(vector.fields[13]).toBe(createHash('sha256').update(JSON.stringify([vector.payload.text, null, null, null])).digest('hex'))
    expect(verify(null, bytes, publicKey(String(vector.fields[9])), Buffer.from(vector.installation_signature, 'base64'))).toBe(true)
    expect(verify(null, bytes, publicKey(String(vector.fields[10])), Buffer.from(vector.window_signature, 'base64'))).toBe(true)
  })

  test('binds every STOP reconciliation and replacement field to the agreed signatures', () => {
    for (const row of stopVectors.cases) {
      const action = row.action === 'stop_reconcile' ? 'stop_reconcile' : 'stop_replace'
      const payload = { ...row.payload, previous_command_id: row.payload.previous_command_id ?? '' }
      const bytes = Buffer.from(row.message, 'base64')
      expect(bytes.toString()).toBe(JSON.stringify(row.fields))
      expect(row.fields[13]).toBe(row.payload_sha256)
      expect(operatorPayloadHash(action, payload)).toBe(row.payload_sha256)
      for (const [field, signature] of [[9, row.installation_signature], [10, row.window_signature]] as const) {
        const key = publicKey(String(row.fields[field]))
        const raw = Buffer.from(signature, 'base64')
        expect(verify(null, bytes, key, raw)).toBe(true)
        for (const name of Object.keys(row.payload) as (keyof typeof payload)[]) {
          const altered = [...row.fields]
          altered[13] = operatorPayloadHash(action, { ...payload, [name]: 'changed' })
          expect(altered[13]).not.toBe(row.payload_sha256)
          expect(verify(null, Buffer.from(JSON.stringify(altered)), key, raw)).toBe(false)
        }
      }
    }
  })

  test('recovers a lost submission in only one replacement window without replaying it', async () => {
    const f = fixture()
    const original = {}
    const neighbor = {}
    const restored = {}
    f.lose.add('submit')
    const send = { action: 'submit', submissionId, text: 'one input' } as const
    expect(await f.request(original, send)).toEqual({ state: 'UNKNOWN' })
    expect(await f.request(original, send)).toEqual({ state: 'UNKNOWN' })
    expect(await f.request(neighbor, send)).toEqual({ state: 'HOLD' })
    const receipt = { action: 'receipt', submissionId } as const
    expect(await f.request(neighbor, receipt)).toEqual({ state: 'HOLD' })
    const [saved] = f.client.recoverable()
    const reopened = new SymphonyOperatorJournal(f.dir, f.storage)
    expect(reopened.find(address, submissionId)?.generation).toBe(1)
    expect(readFileSync(path.join(f.dir, 'symphony-operator-v1.bin'), 'utf8')).not.toContain('PRIVATE KEY')
    f.client.restore(restored, saved)
    expect(() => f.client.restore(neighbor, saved)).toThrow('OPERATOR_HOLD')
    expect(await f.request(original, receipt)).toEqual({ state: 'HOLD' })
    expect(await f.request(restored, receipt)).toMatchObject({ state: 'ADMITTED', receipt: { submission_id: submissionId } })
    expect(await f.request(neighbor, { action: 'stop', attemptId })).toEqual({ state: 'HOLD' })
    expect(f.actions).toEqual(['submit', 'recover', 'receipt'])
    expect(reopened.find(address, submissionId)?.generation).toBe(2)
  })

  test('reconciles an ambiguous STOP by its protected command ID and never issues a second stop', async () => {
    const f = fixture()
    const origin = {}
    expect(await f.request(origin, { action: 'submit', submissionId, text: 'launch' })).toMatchObject({ state: 'ADMITTED' })
    f.denyReceipt()
    expect(await f.request(origin, { action: 'status', attemptId })).toMatchObject({ state: 'STATUS', attemptId, phase: 'RUNNING' })
    const stop = { action: 'stop', attemptId } as const
    f.lose.add('stop')
    expect(await f.request(origin, stop)).toEqual({ state: 'UNKNOWN' })
    const commandId = f.journal.find(address, submissionId)?.stop?.id
    expect(commandId).toBeTruthy()
    f.setStopObservable(false)
    expect(await f.request(origin, stop)).toEqual({ state: 'STOP', attemptId, commandId, observation: 'UNKNOWN' })
    f.setStopObservable(true)
    expect(await f.request(origin, stop)).toEqual({
      state: 'STOP', attemptId, commandId, observation: 'CANCEL_REQUESTED', dispatched: true, localStopped: false
    })
    expect(f.actions).toEqual(['submit', 'status', 'stop', 'stop_result', 'stop_result'])
  })

  test('holds before preparing a write when protected storage is unavailable', async () => {
    const f = fixture()
    f.disableStorage()
    expect(await f.request({}, { action: 'submit', submissionId, text: 'untrusted storage' })).toEqual({ state: 'HOLD' })
    expect(f.actions).toEqual([])
  })

  test('preserves explicit negative STOP observations in the receipt after revocation', async () => {
    const f = fixture(true)
    const origin = {}
    expect(await f.request(origin, { action: 'submit', submissionId, text: 'launch' })).toMatchObject({ state: 'ADMITTED' })
    const stop = { action: 'stop', attemptId } as const
    const first = await f.request(origin, stop)
    expect(first).toMatchObject({
      state: 'STOP', attemptId, observation: 'REVOKED_BEFORE_CANCEL', dispatched: false, localStopped: false
    })
    expect(await f.request(origin, stop)).toEqual(first)
    expect(f.actions).toEqual(['submit', 'stop', 'stop_result'])
  })

  test('replaces only a closed original STOP after native consent and reads only the new ID after restart', async () => {
    const f = fixture()
    const initial = {}
    const stop = { action: 'stop', attemptId } as const
    const reconcile = { action: 'stop-reconcile', attemptId } as const
    const replace = { action: 'stop-replace', attemptId } as const
    await f.request(initial, { action: 'submit', submissionId, text: 'start attempt' })
    f.dropBefore.add('stop')
    expect(await f.request(initial, stop)).toEqual({ state: 'UNKNOWN' })
    const originalId = f.journal.find(address, submissionId)!.stop!.id
    expect(f.commands.size).toBe(0)
    expect(f.cancellations).toEqual([])
    const recovered = {}
    f.client.restore(recovered, f.client.recoverable()[0])
    expect(await f.request(initial, reconcile)).toEqual({ state: 'HOLD' })
    expect(await f.request(recovered, replace)).toMatchObject({ state: 'STOP', commandId: originalId, observation: 'UNKNOWN' })
    expect(f.confirm).not.toHaveBeenCalled()
    f.unsupported.add('stop_reconcile')
    expect(await f.request(recovered, reconcile)).toEqual({ state: 'HOLD' })
    f.unsupported.clear()
    f.lose.add('stop_reconcile')
    expect(await f.request(recovered, reconcile)).toEqual({ state: 'UNKNOWN' })
    const closed = { state: 'STOP', commandId: originalId, observation: 'NOT_ACCEPTED', dispatched: false, localStopped: false }
    expect(await f.request(recovered, stop)).toMatchObject(closed)
    expect(f.cancellations).toEqual([])
    f.confirm.mockResolvedValueOnce(false)
    expect(await f.request(recovered, replace)).toEqual({ state: 'HOLD' })
    expect(f.journal.find(address, submissionId)!.stop).toEqual({ id: originalId, attemptId })

    f.lose.add('stop_replace')
    const [lost, concurrent] = await Promise.all([f.request(recovered, replace), f.request(recovered, replace)])
    expect(lost).toEqual({ state: 'UNKNOWN' })
    const saved = f.journal.find(address, submissionId)!.stop!
    expect(saved).toEqual({ id: expect.any(String), attemptId, previous: { id: originalId, attemptId } })
    expect(saved.id).not.toBe(originalId)
    expect(f.confirm).toHaveBeenCalledTimes(2)
    expect(f.confirm).toHaveBeenLastCalledWith(recovered, { id: originalId, attemptId })
    const replaced = { state: 'STOP', commandId: saved.id, previousCommandId: originalId, observation: 'CANCEL_REQUESTED' }
    expect(concurrent).toMatchObject(replaced)
    expect(f.cancellations).toEqual([saved.id])
    expect(f.requests.filter(row => row.action === 'stop_replace')).toEqual([
      { action: 'stop_replace', submission: submissionId, attempt: attemptId, command: saved.id, previous: originalId },
    ])
    f.client.drop(recovered)
    const restarted = f.createClient()
    const reopened = {}
    restarted.restore(reopened, restarted.recoverable()[0])
    expect(await restarted.request(reopened, address, () => true, stop)).toMatchObject(replaced)
    expect(f.requests.at(-1)).toMatchObject({ action: 'stop_result', command: saved.id })
    expect(f.cancellations).toEqual([saved.id])
  })

  test('holds replacement if the exact attempt changes phase while the native prompt is open', async () => {
    const f = fixture()
    const owner = {}
    await f.request(owner, { action: 'submit', submissionId, text: 'start attempt' })
    f.dropBefore.add('stop')
    await f.request(owner, { action: 'stop', attemptId })
    await f.request(owner, { action: 'stop-reconcile', attemptId })
    f.confirm.mockImplementationOnce(async () => {
      f.attempts.get(submissionId)!.phase = 'STOPPED'
      return true
    })
    expect(await f.request(owner, { action: 'stop-replace', attemptId })).toEqual({ state: 'HOLD' })
    expect(f.actions.slice(-3)).toEqual(['stop_result', 'status', 'status'])
    expect(f.actions).not.toContain('stop_replace')
    expect(f.cancellations).toEqual([])
  })

  test('addresses the real old attempt while leaving its successor and its STOP binding intact', async () => {
    const f = fixture()
    const originalWindow = {}
    const activeWindow = {}
    const restoredWindow = {}
    const nextSubmission = 'submission-b'
    const nextAttempt = 'attempt-b'
    await f.request(originalWindow, { action: 'submit', submissionId, text: 'first attempt' })
    f.attempts.get(submissionId)!.phase = 'STOPPED'
    f.attempts.set(nextSubmission, { id: nextAttempt, admission: 'admission-b', phase: 'RUNNING' })
    await f.request(activeWindow, { action: 'submit', submissionId: nextSubmission, text: 'second attempt' })
    await f.request(activeWindow, { action: 'receipt', submissionId: nextSubmission })
    const old = f.client.recoverable().find(item => item.submissionId === submissionId)!
    f.client.restore(restoredWindow, old)
    expect(await f.request(restoredWindow, { action: 'receipt', submissionId })).toMatchObject({ state: 'ADMITTED' })
    expect(await f.request(restoredWindow, { action: 'stop', attemptId })).toEqual({ state: 'HOLD' })
    expect(f.requests.at(-1)).toMatchObject({ action: 'stop', submission: submissionId, attempt: attemptId })
    const previousRequests = f.requests.length
    expect(await f.request(activeWindow, { action: 'stop', attemptId })).toEqual({ state: 'HOLD' })
    expect(f.requests).toHaveLength(previousRequests)
    expect(f.journal.find(address, nextSubmission)!.stop).toBeUndefined()
    expect(await f.request(activeWindow, { action: 'status', attemptId: nextAttempt }))
      .toMatchObject({ state: 'STATUS', attemptId: nextAttempt, phase: 'RUNNING' })
    expect(f.commands.size).toBe(0)
    expect(f.cancellations).toEqual([])
  })

  test('refuses to sign a challenge from another forwarding generation', async () => {
    const f = fixture()
    f.changeChallenge()
    expect(await f.request({}, { action: 'submit', submissionId, text: 'held text' })).toEqual({ state: 'HOLD' })
    expect(f.actions).toEqual([])
    expect(f.journal.find(address, submissionId)?.generation).toBe(0)
  })
})
