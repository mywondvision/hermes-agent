import { generateKeyPairSync } from 'node:crypto'
import { EventEmitter, once } from 'node:events'
import { PassThrough, Writable } from 'node:stream'

import { afterEach, expect, test, vi } from 'vitest'

import { openOperatorChannel } from '../../electron/symphony-operator-channel'
import { exchangeOperator, operatorPublicKey } from '../../electron/symphony-operator-wire'
import { RoutePeer } from './symphony-route-peer'
import { readTaskFailure, taskLaunchAccepted, taskWorkerSettlement, type TaskDiagnosticCounts } from './symphony-task-diagnostics'

const spawn = vi.hoisted(() => vi.fn())
vi.mock('node:child_process', () => ({ spawn }))
afterEach(() => spawn.mockReset())

const profile = 'synthetic-profile'
const ref = { profile_id: profile, session_id: 'task-chat' }

interface PlanDiagnosticStub {
  answer: Record<string, unknown>
  counts: TaskDiagnosticCounts
}

function stubPeer(mode = 'personal', automaticExit = true, authorityProfile = profile, plan?: PlanDiagnosticStub) {
  const stdout = new PassThrough()
  const actions = new Map<string, string>()
  const sealed = new Set<string>()
  const received: { op: string; socket?: string; channel?: string; operator_public_key?: string; method?: string; command?: string }[] = []
  const write = (value: object) => stdout.write(JSON.stringify(value) + '\n')
  const stdin = new Writable({ write(bytes, _encoding, done) {
    const message = JSON.parse(bytes.toString('utf8'))
    const { id, op, socket, frame } = message
    received.push({ op, socket, channel: message.channel, operator_public_key: message.operator_public_key,
      method: frame?.method, command: frame?.params?.payload?.command_id })
    queueMicrotask(() => {
      if (op === 'init') {
        write({ id, result: { profile, profile_id: authorityProfile, namespace: 'fixture', operator_id: 'fixture',
          conversation_id: ref.session_id, session_id: 'plan-session', install_id: 'fixture-install',
          boot_id: 'fixture-boot', generation: 'fixture-generation', connection_id: 'fixture-ssh', client_id: 'plan-client',
          forward_mode: mode } })
      } else if (op === 'open') {
        write({ id, result: { transport: 'standalone', credential: 'session-token', authority_bound: false } })
      } else if (op === 'counts' && plan) {
        write({ id, result: plan.counts })
      } else if (op === 'worker' && plan) {
        write({ id, result: { state: plan.counts.worker_diagnostics?.state } })
      } else if (op === 'frame') {
        write({ id, result: true })
        const { method, params } = frame
        let result
        switch (method) {
          case 'command.dispatch': result = { output: JSON.stringify(plan?.answer) }; break
          case 'session.resume': result = { session_id: 'runtime', session_key: ref.session_id }; break
          case 'prompt.forward_probe': result = { version: 1, route: 'forward', mode: 'personal', ref, generation: 'forward-gen' }; break
          case 'operator.challenge': {
            const challenge = socket + ':' + frame.id
            actions.set(challenge, params.action)
            const fields = ['hermes.desktop.operator', 1, params.action, 'fixture-owner', 'forward-gen', socket,
              ref.profile_id, ref.session_id, params.submission_id, params.installation_key, params.window_key,
              params.window_generation, challenge, params.payload_sha256, Date.now() + 10_000]
            result = { version: 1, challenge_id: challenge, ref, window_generation: params.window_generation,
              message: Buffer.from(JSON.stringify(fields)).toString('base64') }
            break
          }
          case 'operator.execute': {
            const action = actions.get(params.challenge_id)
            const command = params.payload.command_id
            if (action === 'submit') result = { ref, submission_id: 'submission', admission_id: 'admission', status: 'queued' }
            else {
              if (action === 'stop_reconcile') sealed.add(command)
              const unavailable = sealed.has(command)
              result = { ...params.payload, state: unavailable ? 'NOT_ACCEPTED' : 'CANCEL_REQUESTED',
                dispatched: !unavailable, local_stopped: false }
            }
            break
          }
        }
        write({ socket, frame: { jsonrpc: '2.0', id: frame.id, result } })
      } else {
        write({ id, result: true })
      }
    })
    done()
  } })
  const child = Object.assign(new EventEmitter(), { stdin, stdout, stderr: new PassThrough(), exitCode: null as number | null, killed: false })
  const close = (code: number) => {
    stdout.end()
    child.exitCode = code
    child.emit('close', code, null)
  }
  if (automaticExit) stdin.once('finish', () => close(0))
  spawn.mockReturnValue(child)
  return { received, stdin, close }
}

test('opens the personal main-process route and drops only selected committed ACKs', async () => {
  const { received } = stubPeer()
  const installationKey = generateKeyPairSync('ed25519').privateKey
  const windowKey = generateKeyPairSync('ed25519').privateKey
  const pub = operatorPublicKey(installationKey)
  const peer = new RoutePeer('fixture-python', 'fixture-linux', 'fixture-tmp', 'symphony_operator_peer')
  try {
    await peer.start('synthetic-dashboard-token', pub)
    const reg = peer.registration
    expect(reg.storedConversation).toBe(ref.session_id)
    expect(received).toContainEqual(expect.objectContaining({ op: 'init', operator_public_key: pub }))
    const channel = () => openOperatorChannel(peer.registry, reg)
    const context = { installationKey, windowKey, submissionId: 'submission', current: () => true }
    const first = await channel()
    peer.lostOperatorAcks.add('submit')
    await expect(exchangeOperator({ ...context, channel: first, windowGeneration: 0 }, 'submit', { text: 'synthetic' }))
      .rejects.toThrow('OPERATOR_UNKNOWN')
    expect(first.socket.open).toBe(false)
    const second = await channel()
    const stop = { attempt_id: 'attempt', command_id: 'command' }
    peer.lostOperatorAcks.add('stop')
    await expect(exchangeOperator({ ...context, channel: second, windowGeneration: 1 }, 'stop', { ...stop, reason: 'fixture' }))
      .rejects.toThrow('OPERATOR_UNKNOWN')
    const third = await channel()
    await expect(exchangeOperator({ ...context, channel: third, windowGeneration: 2 }, 'stop_result', stop))
      .resolves.toMatchObject({ value: { command_id: 'command', dispatched: true, local_stopped: false } })
    expect(peer.operatorTrace.map(row => [row.stage, row.action, row.command_id])).toEqual([
      ['sent', 'submit', undefined], ['dropped', 'submit', undefined],
      ['sent', 'stop', 'command'], ['dropped', 'stop', 'command'],
      ['sent', 'stop_result', 'command'], ['reply', 'stop_result', 'command']
    ])
    expect(received.filter(row => row.op === 'open').map(row => row.channel)).toEqual(['ordinary', 'ordinary', 'ordinary'])
    expect(JSON.stringify(peer.operatorTrace)).not.toMatch(/synthetic|signature|public_key|private_key/)
  } finally {
    await peer.shutdown()
  }
})

test('withholds a signed STOP outside stdio until explicit delivery and omits authentication material from the trace', async () => {
  const { received } = stubPeer()
  const installationKey = generateKeyPairSync('ed25519').privateKey
  const windowKey = generateKeyPairSync('ed25519').privateKey
  const peer = new RoutePeer('fixture-python', 'fixture-linux', 'fixture-tmp', 'symphony_operator_peer')
  try {
    await peer.start('synthetic-dashboard-token', operatorPublicKey(installationKey))
    const channel = await openOperatorChannel(peer.registry, peer.registration)
    const context = { installationKey, windowKey, submissionId: 'submission', current: () => true, channel, windowGeneration: 1 }
    const sent = () => received.filter(row => row.method === 'operator.execute').map(row => row.command)
    const lost = { attempt_id: 'attempt-b', command_id: 'lost-b', reason: 'fixture only' }
    peer.interceptNextStop('lose')
    await expect(exchangeOperator(context, 'stop', lost)).rejects.toThrow('OPERATOR_UNKNOWN')
    expect(sent()).toEqual([])
    expect(channel.socket.open).toBe(true)
    await expect(exchangeOperator(context, 'stop_reconcile', lost)).resolves.toMatchObject({ value: { state: 'NOT_ACCEPTED' } })
    await peer.releaseInterceptedStop()
    await vi.waitFor(() => expect(peer.operatorTrace).toContainEqual(expect.objectContaining({
      action: 'stop', stage: 'reply', command_id: 'lost-b', state: 'NOT_ACCEPTED',
    })))
    expect(sent()).toEqual(['lost-b', 'lost-b'])
    expect(() => peer.releaseInterceptedStop()).toThrow('No intercepted STOP')

    peer.interceptNextStop('hold')
    const delayed = exchangeOperator(context, 'stop', { ...lost, attempt_id: 'attempt-a', command_id: 'held-a' })
    await vi.waitFor(() => expect(peer.operatorTrace).toContainEqual(expect.objectContaining({
      action: 'stop', stage: 'held-before', command_id: 'held-a',
    })))
    expect(sent()).toHaveLength(2)
    await peer.releaseInterceptedStop()
    await expect(delayed).resolves.toMatchObject({ value: { command_id: 'held-a' } })
    expect(sent()).toEqual(['lost-b', 'lost-b', 'held-a'])
    expect(JSON.stringify(peer.operatorTrace)).not.toMatch(/fixture only|synthetic-dashboard-token|signature|public_key|private_key/)
  } finally {
    await peer.shutdown()
  }
})

test('preserves the bound first APPROVED answer without launch and reads only B diagnostics before cleanup', async () => {
  const plan = { admission_id: 'b', request_id: 'task-b', plan_digest: 'b'.repeat(64) }
  const input = { request_id: plan.request_id, plan_digest: plan.plan_digest, input_id: 'input-b' }
  const answer: Record<string, unknown> = { ...input, decision: 'APPROVED', debug: 'synthetic secret' }
  const { received } = stubPeer('personal', true, profile, { answer, counts: {
    permits: 1, reservations: 1, launches: 1, plan_inputs: [plan],
    by_request: { 'task-b': { decision: 'APPROVED', plan_digest: plan.plan_digest } },
    worker_diagnostics: {
      state: 'armed', finished_event: false,
      outcomes: [{ attempt_id: 'attempt-a', state: 'finished' }],
      launches: [{ request_id: 'task-a', attempt_id: 'attempt-a', state: 'STOPPED', hold_reason: 'WORKSPACE_HELD' }],
      launch_handoffs: [
        { request_id: 'task-a', grant_outcome: 'ISSUED', port_outcome: 'SUBMITTED', uncertainty: [] },
        { request_id: plan.request_id, grant_outcome: 'ISSUED', port_outcome: 'UNKNOWN',
          uncertainty: ['WORKSPACE_HELD', 'sensitive fixture detail'] }
      ],
      plan_answers: [
        { request_id: 'task-a', decision: 'APPROVED', launch: 'SUBMITTED', job: 'job-a' },
        { request_id: plan.request_id, decision: 'APPROVED', error_type: 'Refused', error_reason: 'synthetic secret' }
      ]
    }
  } })
  const peer = new RoutePeer('fixture-python', 'fixture-linux', 'fixture-tmp', 'symphony_operator_peer')
  try {
    await peer.start('synthetic-dashboard-token', operatorPublicKey(generateKeyPairSync('ed25519').privateKey))
    const reg = peer.registry.registration(peer.registration)!
    const evidence = await peer.registry.observe(reg, 'observe-plan')
    const tunnel = await peer.registry.attach(reg, evidence)
    const socket = await tunnel.open('/api/ws', 'hermes.symphony-plan.v1', () => {}, () => {})
    const answerPlan = () => socket.request('command.dispatch', { arg: 'plan-answer ' + JSON.stringify(input) })
    await answerPlan()
    const first = peer.planAnswerReplies[0]
    expect(first).toEqual({ ...input, state: 'APPROVED', launch: null, job_field: 'absent', binding_matches: true })
    expect(taskLaunchAccepted(first)).toBe(false)
    const failure = await readTaskFailure('b', plan.plan_digest, first, { permits: 1, reservations: 1, launches: 1 },
      () => peer.control('counts'), () => peer.control('worker', { action: 'state' }))
    expect(failure).toEqual({ ...plan, first_answer: first, worker: 'armed',
      counts_read: 'fulfilled', worker_read: 'fulfilled',
      launch_handoff: { grant_outcome: 'ISSUED', port_outcome: 'UNKNOWN', uncertainty: ['WORKSPACE_HELD', 'OTHER'] },
      persisted_decision: { decision: 'APPROVED', plan_digest: plan.plan_digest },
      deltas: { permits: 0, reservations: 0, launches: 0 },
      owner_worker: { state: 'armed', finished_event: false, launches: [], outcomes: [],
        plan_answers: [{ decision: 'APPROVED', launch: null, job: null, error_type: 'Refused', error_reason: null }] } })
    expect(received.map(row => row.op)).toEqual(['init', 'open', 'frame', 'counts', 'worker'])
    expect(JSON.stringify(failure)).not.toMatch(/synthetic secret|sensitive fixture detail|job-a|attempt-a|token|signature/)

    answer.launch = 'UNKNOWN'
    await answerPlan()
    const missingJob = peer.planAnswerReplies.at(-1)
    expect(missingJob).toMatchObject({ ...input, state: 'APPROVED', launch: 'UNKNOWN',
      job_field: 'absent', binding_matches: true })
    expect(taskLaunchAccepted(missingJob)).toBe(false)
    answer.job = null
    await answerPlan()
    const nullJob = peer.planAnswerReplies.at(-1)
    expect(nullJob).toMatchObject({ ...input, state: 'APPROVED', launch: 'UNKNOWN',
      job_field: 'null', binding_matches: true })
    expect(taskLaunchAccepted(nullJob)).toBe(false)
    answer.job = ''
    await answerPlan()
    const emptyJob = peer.planAnswerReplies.at(-1)
    expect(emptyJob).toMatchObject({ ...input, state: 'APPROVED', launch: 'UNKNOWN',
      job_field: 'empty', binding_matches: true })
    expect(taskLaunchAccepted(emptyJob)).toBe(false)

    answer.launch = 'SUBMITTED'
    answer.job = 'job-b'
    await answerPlan()
    const submitted = peer.planAnswerReplies.at(-1)
    expect(submitted).toMatchObject({ job_field: 'id' })
    expect(taskLaunchAccepted(submitted)).toBe(true)
    expect(JSON.stringify(submitted)).not.toContain('job-b')
    answer.plan_digest = 'a'.repeat(64)
    await answerPlan()
    const mismatched = peer.planAnswerReplies.at(-1)
    expect(mismatched).toMatchObject({ ...input, launch: 'SUBMITTED', binding_matches: false })
    expect(taskLaunchAccepted(mismatched)).toBe(false)
    expect(peer.planAnswerReplies[0]).toBe(first)
    tunnel.close()
  } finally {
    await peer.shutdown()
  }
})

test('waits for the exact worker outcome and an explicit workspace hold clearance for A', () => {
  const launch: NonNullable<TaskDiagnosticCounts['worker_diagnostics']>['launches'][number] = {
    request_id: 'task-a', attempt_id: 'attempt-a', state: 'STOPPED', hold_reason: 'WORKSPACE_HELD'
  }
  const outcome = { attempt_id: 'attempt-a', state: 'finished' }
  const counts: TaskDiagnosticCounts = {
    permits: 1, reservations: 1, launches: 1, plan_inputs: [], by_request: {},
    worker_diagnostics: { state: 'idle', finished_event: true, outcomes: [outcome], launches: [launch], plan_answers: [] }
  }
  const settlement = () => taskWorkerSettlement(counts, 'attempt-a', 'task-a')
  expect(settlement()).toEqual({
    worker_outcome: { attempt_id: 'attempt-a', state: 'finished', error_type: null, error_reason: null },
    launch: { state: 'STOPPED', hold_reason: 'WORKSPACE_HELD' },
    settled: false
  })
  delete launch.hold_reason
  expect(settlement()).toMatchObject({ launch: { hold_reason: 'UNRECOGNIZED' }, settled: false })
  launch.hold_reason = null
  expect(settlement()).toMatchObject({ launch: { hold_reason: null }, settled: true })
  expect(taskWorkerSettlement(counts, 'attempt-a', 'task-b')).toMatchObject({ launch: null, settled: false })
  outcome.state = 'running'
  expect(settlement()).toMatchObject({ settled: false })
})

test.each([
  { label: 'shared mode', mode: 'shared', authorityProfile: profile },
  { label: 'a different profile', mode: 'personal', authorityProfile: 'another-profile' },
  { label: 'a non-canonical case variant', mode: 'personal', authorityProfile: 'Synthetic-profile' }
])('refuses $label before attaching an operator socket', async ({ mode, authorityProfile }) => {
  const { received } = stubPeer(mode, true, authorityProfile)
  const peer = new RoutePeer('fixture-python', 'fixture-linux', 'fixture-tmp', 'symphony_operator_peer')
  try {
    const pub = operatorPublicKey(generateKeyPairSync('ed25519').privateKey)
    await expect(peer.start('synthetic-dashboard-token', pub)).rejects.toThrow('OPERATOR enrollment or canonical profile not confirmed')
    expect(received.map(row => row.op)).toEqual(['init'])
  } finally {
    await peer.shutdown()
  }
})

test('waits beyond the shutdown ACK and reports a terminal cleanup error', async () => {
  const { stdin, close } = stubPeer('personal', false)
  const peer = new RoutePeer('fixture-python', 'fixture-linux', 'fixture-tmp', 'symphony_operator_peer')
  const pub = operatorPublicKey(generateKeyPairSync('ed25519').privateKey)
  await peer.start('synthetic-dashboard-token', pub)
  const flushed = once(stdin, 'finish')
  const ended = vi.fn()
  const shutdown = peer.shutdown()
  void shutdown.then(ended, ended)
  await flushed
  expect(ended).not.toHaveBeenCalled()
  expect(peer.operatorExitStatus).toBeUndefined()
  close(1)
  await expect(shutdown).rejects.toThrow('TASK peer ended during cleanup with code 1')
  expect(peer.operatorExitStatus).toEqual({ code: 1, signal: null })
})
