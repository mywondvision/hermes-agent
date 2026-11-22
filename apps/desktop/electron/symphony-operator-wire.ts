import { createHash, createPublicKey, sign, type KeyObject } from 'node:crypto'

import { OperatorRouteRefused } from './symphony-route-socket'
import type { PlanSocket } from './symphony-route'

export interface OperatorRef {
  readonly profile_id: string
  readonly session_id: string
}

export interface OperatorChannel {
  readonly socket: PlanSocket
  readonly sessionId: string
  readonly ref: OperatorRef
  readonly forwardGeneration: string
}

export interface OperatorPayloads {
  submit: { text: string }
  receipt: Record<string, never>
  status: { attempt_id: string }
  stop: { attempt_id: string; command_id: string; reason: string }
  stop_result: { attempt_id: string; command_id: string }
  stop_reconcile: { attempt_id: string; command_id: string; reason: string }
  stop_replace: { attempt_id: string; previous_command_id: string; command_id: string; reason: string }
  apply_info: { attempt_id: string }
  apply: OperatorPatchPayload
  archive: OperatorPatchPayload
  apply_result: { attempt_id: string; command_id: string }
  recover: Record<string, never>
}

interface OperatorPatchPayload {
  attempt_id: string
  job: string
  capture_seq: number
  check_seq: number
  patch_sha256: string
  command_id: string
}

export class OperatorHold extends Error {
  constructor() { super('OPERATOR_HOLD') }
}

export class OperatorUnknown extends Error {
  constructor() { super('OPERATOR_UNKNOWN') }
}

export function operatorPublicKey(key: KeyObject) {
  return Buffer.from(createPublicKey(key).export({ format: 'jwk' }).x!, 'base64url').toString('base64')
}

export function sameOperatorRef(a: unknown, b: OperatorRef) {
  if (!a || typeof a !== 'object' || !('profile_id' in a) || !('session_id' in a)) return false
  return a.profile_id === b.profile_id && a.session_id === b.session_id
}

export function operatorPayloadHash<K extends keyof OperatorPayloads>(action: K, payload: OperatorPayloads[K]) {
  const fields = payload as Partial<OperatorPayloads['submit'] & OperatorPayloads['stop_replace'] & OperatorPatchPayload>
  let values: readonly unknown[]
  switch (action) {
    case 'stop_reconcile':
      values = [action, fields.attempt_id, fields.command_id, fields.reason]
      break
    case 'stop_replace':
      values = [action, fields.attempt_id, fields.previous_command_id, fields.command_id, fields.reason]
      break
    case 'apply_info':
      values = [action, fields.attempt_id]
      break
    case 'apply':
    case 'archive':
      values = [action, fields.attempt_id, fields.job, fields.capture_seq, fields.check_seq, fields.patch_sha256, fields.command_id]
      break
    case 'apply_result':
      values = [action, fields.attempt_id, fields.command_id]
      break
    default:
      values = [fields.text ?? null, fields.attempt_id ?? null, fields.command_id ?? null, fields.reason ?? null]
  }
  return createHash('sha256')
    .update(JSON.stringify(values))
    .digest('hex')
}

interface OperatorExchange {
  readonly channel: OperatorChannel
  readonly installationKey: KeyObject
  readonly windowKey: KeyObject
  readonly submissionId: string
  readonly windowGeneration: number
  readonly current: () => boolean
}

function validateChallenge(challenge: any, fields: readonly unknown[], channel: OperatorChannel) {
  if (challenge?.version !== 1 || typeof challenge.message !== 'string' || challenge.message.length > 16_384 ||
      typeof challenge.challenge_id !== 'string' || !challenge.challenge_id ||
      !Number.isSafeInteger(challenge.window_generation) || challenge.window_generation < 0 ||
      !sameOperatorRef(challenge.ref, channel.ref)) throw new OperatorHold()
  const message = Buffer.from(challenge.message, 'base64')
  if (message.toString('base64') !== challenge.message) throw new OperatorHold()
  const signed: unknown = JSON.parse(message.toString('utf8'))
  if (!Array.isArray(signed) || signed.length !== 15 ||
      [signed[3], signed[5]].some(value => typeof value !== 'string' || !value) ||
      !Number.isSafeInteger(signed[14]) || signed[14] <= Date.now() || signed[14] > Date.now() + 60_000) {
    throw new OperatorHold()
  }
  const [action, submissionId, installation, window, hash] = fields
  const expected = ['hermes.desktop.operator', 1, action, signed[3], channel.forwardGeneration, signed[5],
    channel.ref.profile_id, channel.ref.session_id, submissionId, installation, window,
    challenge.window_generation, challenge.challenge_id, hash, signed[14]]
  if (expected.some((value, i) => value !== signed[i])) throw new OperatorHold()
  return message
}

/** Only the holder of the pinned main-process socket can redeem this exchange.
 * Check the window on both sides of every await before exposing a response. */
export async function exchangeOperator<K extends keyof OperatorPayloads>(
  exchange: OperatorExchange, action: K, payload: OperatorPayloads[K], beforeExecute?: () => void
): Promise<{ value: unknown; windowGeneration: number }> {
  const { channel, submissionId, installationKey, windowKey, windowGeneration } = exchange
  const { socket, sessionId } = channel
  const current = () => socket.open && exchange.current()
  const installation = operatorPublicKey(installationKey)
  const window = operatorPublicKey(windowKey)
  const hash = operatorPayloadHash(action, payload)
  let request: object
  try {
    if (!current()) throw new OperatorHold()
    const challenge = await socket.request('operator.challenge', {
      session_id: sessionId, action, submission_id: submissionId,
      installation_key: installation, window_key: window, payload_sha256: hash, window_generation: windowGeneration
    })
    if (!current()) throw new OperatorHold()
    const bytes = validateChallenge(challenge, [action, submissionId, installation, window, hash], channel)
    if (challenge.window_generation !== windowGeneration || (action === 'submit') !== (windowGeneration === 0)) throw new OperatorHold()
    request = {
      session_id: sessionId, challenge_id: challenge.challenge_id, payload,
      installation_signature: sign(null, bytes, installationKey).toString('base64'),
      window_signature: sign(null, bytes, windowKey).toString('base64')
    }
    if (!current()) throw new OperatorHold()
    beforeExecute?.()
  } catch {
    throw new OperatorHold()
  }
  try {
    const result = await socket.request('operator.execute', request)
    if (!current()) throw new OperatorUnknown()
    return { value: result, windowGeneration }
  } catch (error) {
    if (current() && error instanceof OperatorRouteRefused &&
        (action === 'stop' || action === 'stop_reconcile' || action === 'stop_replace' ||
          action === 'apply_info' || action === 'apply' || action === 'archive' || action === 'apply_result')) throw new OperatorHold()
    throw new OperatorUnknown()
  }
}
