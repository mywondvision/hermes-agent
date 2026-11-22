import { randomUUID } from 'node:crypto'

import type { OperatorBinding } from './symphony-operator-journal'
import type { OperatorCheckedPatch, OperatorPatchCommand, OperatorReply } from './symphony-operator-types'
import { OperatorHold, OperatorUnknown, type OperatorPayloads } from './symphony-operator-wire'

export function parseOperatorPatchCommand(text: string) {
  const match = /^\/symphony[ \t]+(apply|archive|apply-result)[ \t]+([A-Za-z0-9_-]{1,128})[ \t]+([a-f0-9]{64})[ \t]*$/u.exec(text)
  if (!match || match[0] !== text) throw new OperatorHold()
  return { action: match[1] as 'apply' | 'archive' | 'apply-result', job: match[2], patchSha256: match[3] }
}

type PatchCall = ReturnType<typeof parseOperatorPatchCommand>
type PatchAction = 'apply_info' | 'apply' | 'archive' | 'apply_result'

interface PatchContext {
  exchange<K extends PatchAction>(action: K, payload: OperatorPayloads[K], beforeExecute?: () => void): Promise<{ value: unknown }>
  confirm(patch: OperatorCheckedPatch, action: OperatorPatchCommand['action']): Promise<boolean>
  persist(command: OperatorPatchCommand): void
  current(): boolean
}

const fields = (value: unknown) => value && typeof value === 'object' ? value as Record<string, unknown> : {}
const validSeq = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0

function patchReply(value: unknown, command: OperatorPatchCommand): OperatorReply {
  const data = fields(value)
  const observation = data.state
  if (data.attempt_id !== command.attemptId || data.command_id !== command.id) throw new OperatorUnknown()
  if (observation === 'UNKNOWN') return { state: 'PATCH', command, observation }
  if (data.job !== command.job || data.capture_seq !== command.captureSeq || data.check_seq !== command.checkSeq ||
      data.patch_sha256 !== command.patchSha256) throw new OperatorUnknown()
  if (command.action === 'archive' && observation === 'ARCHIVED') return { state: 'PATCH', command, observation }
  if (command.action === 'apply' && observation === 'CHECK_FAILED') return { state: 'PATCH', command, observation }
  if (command.action === 'apply' && observation === 'APPLIED' && typeof data.commit === 'string' &&
      [40, 64].includes(data.commit.length) && !/[^a-f0-9]/.test(data.commit)) {
    return { state: 'PATCH', command, observation, commit: data.commit }
  }
  throw new OperatorUnknown()
}

export async function requestOperatorPatch(row: OperatorBinding, call: PatchCall, context: PatchContext): Promise<OperatorReply> {
  const attemptId = row.attemptId
  if (!attemptId || attemptId.length > 128 || /[^A-Za-z0-9_-]/.test(attemptId) || !context.current()) throw new OperatorHold()
  const saved = row.patchCommands?.find(command => command.attemptId === attemptId &&
    command.job === call.job && command.patchSha256 === call.patchSha256)
  if (saved) {
    if (call.action !== 'apply-result' && call.action !== saved.action) throw new OperatorHold()
    try {
      const { value } = await context.exchange('apply_result', { attempt_id: attemptId, command_id: saved.id })
      return patchReply(value, saved)
    } catch (error) {
      if (!(error instanceof OperatorUnknown) || !context.current()) throw error
      return { state: 'PATCH', command: saved, observation: 'UNKNOWN' }
    }
  }
  if (call.action === 'apply-result') throw new OperatorHold()

  const info = async () => {
    const { value } = await context.exchange('apply_info', { attempt_id: attemptId })
    const data = fields(value)
    const { capture_seq: captureSeq, check_seq: checkSeq } = data
    if (data.attempt_id !== attemptId || data.job !== call.job || data.patch_sha256 !== call.patchSha256 ||
        !validSeq(captureSeq) || !validSeq(checkSeq) || captureSeq >= checkSeq) throw new OperatorHold()
    return { attemptId, job: call.job, patchSha256: call.patchSha256, captureSeq, checkSeq }
  }
  const patch = await info()
  if (!context.current() || !(await context.confirm(patch, call.action)) || !context.current()) throw new OperatorHold()
  const current = await info()
  if (current.captureSeq !== patch.captureSeq || current.checkSeq !== patch.checkSeq) throw new OperatorHold()

  const command: OperatorPatchCommand = { ...patch, id: randomUUID(), action: call.action }
  let prepared = false
  try {
    const { value } = await context.exchange(call.action, {
      attempt_id: attemptId, job: command.job, capture_seq: command.captureSeq, check_seq: command.checkSeq,
      patch_sha256: command.patchSha256, command_id: command.id,
    }, () => {
      context.persist(command)
      prepared = true
    })
    return patchReply(value, command)
  } catch (error) {
    if (!(error instanceof OperatorUnknown) || !prepared || !context.current()) throw error
    return { state: 'PATCH', command, observation: 'UNKNOWN' }
  }
}
