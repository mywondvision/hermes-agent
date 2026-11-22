import { randomUUID } from 'node:crypto'

import {
  operatorPrivateKey, sameOperatorAddress,
  type OperatorAddress, type OperatorBinding, type OperatorStopTarget, type SymphonyOperatorJournal
} from './symphony-operator-journal'
import { parseOperatorPatchCommand, requestOperatorPatch } from './symphony-operator-patch'
import {
  exchangeOperator, OperatorHold, OperatorUnknown, sameOperatorRef,
  type OperatorChannel, type OperatorPayloads
} from './symphony-operator-wire'
import type { OperatorAdmission, OperatorCall, OperatorCheckedPatch, OperatorPatchCommand, OperatorReply } from './symphony-operator-types'

interface PatchAuthority<Owner> {
  focused(owner: Owner): boolean
  confirm(owner: Owner, patch: OperatorCheckedPatch, action: OperatorPatchCommand['action']): Promise<boolean>
}

interface Viewer {
  readonly address: OperatorAddress
  readonly owned: Set<string>
  readonly restoring: Set<string>
  channel?: Promise<OperatorChannel>
  tail: Promise<unknown>
  readonly sockets: Map<string, OperatorChannel>
}

const statuses = ['queued', 'started', 'terminal', 'unknown']
const stopReason = 'owner_requested'
const isId = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 512
const fields = (value: unknown) => value && typeof value === 'object' ? value as Record<string, unknown> : {}

function stopReply(value: unknown, attemptId: string, commandId: string, previousId?: string) {
  const data = fields(value)
  if (data.attempt_id !== attemptId || data.command_id !== commandId || !isId(data.state)) throw new OperatorUnknown()
  if (data.state === 'UNKNOWN') {
    return { state: 'STOP' as const, attemptId, commandId, observation: data.state,
      ...(previousId !== undefined && { previousCommandId: previousId }) }
  }
  if (data.previous_command_id !== previousId ||
      (data.state === 'NOT_ACCEPTED' && (data.dispatched !== false || data.local_stopped !== false))) {
    throw new OperatorUnknown()
  }
  return {
    state: 'STOP' as const, attemptId, commandId, observation: data.state,
    ...(previousId !== undefined && { previousCommandId: previousId }),
    ...(typeof data.dispatched === 'boolean' && { dispatched: data.dispatched }),
    ...(typeof data.local_stopped === 'boolean' && { localStopped: data.local_stopped })
  }
}

function statusReply(value: unknown, attemptId: string) {
  const data = fields(value)
  if (data.attempt_id !== attemptId || !isId(data.phase) || typeof data.observed_at !== 'number' ||
      !Number.isFinite(data.observed_at)) throw new OperatorUnknown()
  return { state: 'STATUS' as const, attemptId, phase: data.phase, observedAt: data.observed_at,
    ...(isId(data.evidence) && { evidence: data.evidence }) }
}

function admission(value: unknown, row: OperatorBinding, channel: OperatorChannel): OperatorAdmission {
  const data = fields(value)
  if (!sameOperatorRef(data.ref, channel.ref) || data.submission_id !== row.submissionId ||
      !isId(data.admission_id) || data.admission_id === row.submissionId ||
      (row.admissionId !== undefined && row.admissionId !== data.admission_id) ||
      typeof data.status !== 'string' || !statuses.includes(data.status)) throw new OperatorUnknown()
  return { admission_id: data.admission_id, submission_id: row.submissionId, status: data.status as OperatorAdmission['status'] }
}

async function oneAttempt(
  rows: readonly OperatorBinding[], requested: string | undefined,
  read: (row: OperatorBinding) => Promise<{ row: OperatorBinding }>
) {
  let matches: readonly OperatorBinding[]
  if (requested) {
    matches = rows.filter(row => row.attemptId === requested)
    // One bound admission is sufficient to ask the server to verify an exact
    // attempt. Status and STOP do not also require the receipt permission.
    if (matches.length === 0 && rows.length === 1) matches = rows
  } else {
    const resolved = await Promise.all(rows.map(async row => row.attemptId ? row : (await read(row)).row))
    matches = resolved.filter(row => row.attemptId)
  }
  const attemptId = requested ?? matches[0]?.attemptId
  if (matches.length !== 1 || !attemptId) throw new OperatorHold()
  return { row: matches[0], attemptId }
}

/** A BrowserWindow's ownership is ephemeral. Only the main process can move a
 * persisted binding to a replacement window; knowing its ID cannot claim it. */
export class SymphonyOperatorClient<Owner extends object> {
  private readonly viewers = new Map<Owner, Viewer>()

  constructor(
    private readonly journal: SymphonyOperatorJournal,
    private readonly open: (address: OperatorAddress) => Promise<OperatorChannel>,
    private readonly confirmReplacement?: (owner: Owner, stop: OperatorStopTarget) => Promise<boolean>,
    private readonly patchAuthority?: PatchAuthority<Owner>
  ) {}

  drop(owner: Owner) {
    const view = this.viewers.get(owner)
    this.viewers.delete(owner)
    void view?.channel?.then(channel => channel.socket.close(), () => undefined)
  }

  private viewer(owner: Owner, address: OperatorAddress) {
    const prior = this.viewers.get(owner)
    if (prior) {
      if (!sameOperatorAddress(prior.address, address)) throw new OperatorHold()
      return prior
    }
    const view: Viewer = { address, owned: new Set(), restoring: new Set(), sockets: new Map(), tail: Promise.resolve() }
    this.viewers.set(owner, view)
    return view
  }

  recoverable() { return this.journal.bindings().filter(row => row.generation > 0 && !row.recovering) }

  /** Invoked with a protected journal record selected by native UI and a
   * replacement BrowserWindow actually created by main. */
  restore(owner: Owner, row: OperatorBinding) {
    const saved = this.journal.find(row.address, row.submissionId)
    if (!saved || saved.windowKey !== row.windowKey || saved.recovering || saved.generation === 0 ||
        saved.generation !== row.generation) throw new OperatorHold()
    if ([...this.viewers.values()].some(view => sameOperatorAddress(view.address, row.address) &&
        view.restoring.has(row.submissionId))) throw new OperatorHold()
    const displaced: Owner[] = []
    for (const [prior, viewer] of this.viewers) {
      if (sameOperatorAddress(viewer.address, row.address) && viewer.owned.delete(row.submissionId)) {
        displaced.push(prior)
        viewer.sockets.delete(row.submissionId)
        if (viewer.owned.size === 0) this.drop(prior)
      }
    }
    const replacement = this.viewer(owner, row.address)
    replacement.owned.add(row.submissionId)
    replacement.restoring.add(row.submissionId)
    return displaced
  }

  private async channel(view: Viewer) {
    const current = view.channel
    const existing = await current?.catch(() => undefined)
    if (existing?.socket.open) return existing
    if (view.channel !== current) return view.channel!
    view.sockets.clear()
    view.channel = this.open(view.address)
    return view.channel
  }

  private async onChannel(owner: Owner, view: Viewer, current: () => boolean, call: OperatorCall): Promise<OperatorReply> {
    const patch = call.action === 'patch-decision' ? parseOperatorPatchCommand(call.text) : undefined
    const live = () => current() && this.viewers.get(owner) === view && (!patch || this.patchAuthority?.focused(owner) === true)
    if (!live() || (call.action !== 'submit' && view.owned.size === 0)) throw new OperatorHold()
    const channel = await this.channel(view)
    if (!live()) throw new OperatorHold()

    const exchange = <K extends keyof OperatorPayloads>(row: OperatorBinding, action: K, payload: OperatorPayloads[K], beforeExecute?: () => void) => {
      const installation = this.journal.existingInstallation()
      if (!installation) throw new OperatorHold()
      return exchangeOperator({
        channel, installationKey: installation.key, windowKey: operatorPrivateKey(row.windowKey),
        submissionId: row.submissionId, windowGeneration: row.generation,
        current: () => live() && view.owned.has(row.submissionId)
      }, action, payload, beforeExecute)
    }

    const bind = async (row: OperatorBinding) => {
      if (row.recovering || row.generation === 0) throw new OperatorUnknown()
      if (view.sockets.get(row.submissionId) !== channel) {
        const { value, windowGeneration } = await exchange(row, 'recover', {}, () => { row = this.journal.update(row, { recovering: true }) })
        const data = fields(value)
        if (!sameOperatorRef(data.ref, channel.ref) || data.submission_id !== row.submissionId ||
            data.window_generation !== windowGeneration + 1) throw new OperatorUnknown()
        row = this.journal.update(row, { recovering: undefined, generation: windowGeneration + 1 })
        view.sockets.set(row.submissionId, channel)
        view.restoring.delete(row.submissionId)
      }
      return row
    }

    const read = async (saved: OperatorBinding) => {
      let row = await bind(saved)
      const { value } = await exchange(row, 'receipt', {})
      const receipt = admission(value, row, channel)
      const attempt = fields(fields(value).attempt)
      const attemptId = attempt.attempt_id
      const verified = isId(attemptId) ? attemptId : undefined
      const job = verified && typeof attempt.job === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(attempt.job) ? attempt.job : undefined
      if (!row.admissionId || row.attemptId !== verified || row.job !== job) {
        row = this.journal.update(row, { admissionId: receipt.admission_id, attemptId: verified, job })
      }
      return { row, receipt }
    }

    if (call.action === 'submit') {
      let row = this.journal.find(view.address, call.submissionId)
      if (row && !view.owned.has(row.submissionId)) throw new OperatorHold()
      if (row && row.generation > 0) throw new OperatorUnknown()
      if (!row) {
        this.journal.installation()
        row = this.journal.begin(view.address, call.submissionId)
        view.owned.add(row.submissionId)
      }
      const prepared = row
      const { value } = await exchange(prepared, 'submit', { text: call.text }, () => {
        row = this.journal.update(prepared, { generation: 1 })
        view.sockets.set(prepared.submissionId, channel)
      })
      const receipt = admission(value, prepared, channel)
      try {
        this.journal.update(row, { admissionId: receipt.admission_id })
      } catch {
        throw new OperatorUnknown()
      }
      return { state: 'ADMITTED', receipt }
    }

    if (call.action === 'receipt') {
      const submissionId = call.submissionId ?? (view.owned.size === 1 ? [...view.owned][0] : undefined)
      if (!submissionId || !view.owned.has(submissionId)) throw new OperatorHold()
      const row = this.journal.find(view.address, submissionId)
      if (!row) throw new OperatorUnknown()
      return { state: 'ADMITTED', receipt: (await read(row)).receipt }
    }

    const rows = this.journal.bindings().filter(row => row.generation > 0 && view.owned.has(row.submissionId) &&
      sameOperatorAddress(row.address, view.address))
    if (call.action === 'patch-decision') {
      const authority = this.patchAuthority
      if (!authority || !patch) throw new OperatorHold()
      const resolved = await Promise.all(rows.map(async row => row.job ? row : (await read(row)).row))
      const matches = resolved.filter(row => row.job === patch.job)
      if (matches.length !== 1) throw new OperatorHold()
      const row = await bind(matches[0])
      return requestOperatorPatch(row, patch, {
        current: () => live() && view.owned.has(row.submissionId),
        exchange: (action, payload, beforeExecute) => exchange(row, action, payload, beforeExecute),
        confirm: (checked, action) => authority.confirm(owner, checked, action),
        persist: command => { this.journal.update(row, { patchCommands: [...(row.patchCommands ?? []), command] }) },
      })
    }
    const targetAttempt = await oneAttempt(rows, call.attemptId, read)
    const attemptId = targetAttempt.attemptId
    if (call.action !== 'status' && targetAttempt.row.attemptId && targetAttempt.row.attemptId !== attemptId) {
      throw new OperatorHold()
    }
    const row = await bind(targetAttempt.row)
    const observeStatus = async () => {
      const { value } = await exchange(row, 'status', { attempt_id: attemptId })
      return statusReply(value, attemptId)
    }
    if (call.action === 'status') return observeStatus()

    const { stop } = row
    if (stop && stop.attemptId !== attemptId) throw new OperatorUnknown()
    if (!stop) {
      if (call.action !== 'stop') throw new OperatorHold()
      const commandId = randomUUID()
      const { value } = await exchange(row, 'stop', { attempt_id: attemptId, command_id: commandId, reason: stopReason },
        () => this.journal.update(row, { stop: { id: commandId, attemptId } }))
      return stopReply(value, attemptId, commandId)
    }

    const target = { attempt_id: attemptId, command_id: stop.id }
    if (call.action === 'stop-reconcile' && !stop.previous) {
      const { value } = await exchange(row, 'stop_reconcile', { ...target, reason: stopReason })
      return stopReply(value, attemptId, stop.id)
    }

    const { value } = await exchange(row, 'stop_result', target)
    const observed = stopReply(value, attemptId, stop.id, stop.previous?.id)
    if (call.action !== 'stop-replace' || stop.previous || observed.observation !== 'NOT_ACCEPTED') return observed
    if (!this.confirmReplacement || (await observeStatus()).phase !== 'RUNNING' || !live()) throw new OperatorHold()
    if (!(await this.confirmReplacement(owner, stop)) || !live() || (await observeStatus()).phase !== 'RUNNING') {
      throw new OperatorHold()
    }
    const commandId = randomUUID()
    const { value: replacement } = await exchange(row, 'stop_replace', {
      ...target, previous_command_id: stop.id, command_id: commandId, reason: stopReason
    }, () => this.journal.update(row, { stop: { id: commandId, attemptId, previous: stop } }))
    return stopReply(replacement, attemptId, commandId, stop.id)
  }

  async request(owner: Owner, address: OperatorAddress, current: () => boolean, call: OperatorCall): Promise<OperatorReply> {
    try {
      if (!current()) throw new OperatorHold()
      const view = this.viewer(owner, address)
      const queued = view.tail.then(() => this.onChannel(owner, view, current, call))
      view.tail = queued.catch(() => undefined)
      return await queued
    } catch (error) {
      return { state: error instanceof OperatorUnknown ? 'UNKNOWN' : 'HOLD' }
    }
  }
}
