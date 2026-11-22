import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { EventEmitter } from 'node:events'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'

import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $connection } from '@/store/session'
import { recordSessionEventScope } from '@/store/session-states'
import { SymphonyRouteClient } from '../../../../../electron/symphony-route-client'
import { PlanRouteRegistry } from '../../../../../electron/symphony-route-registry'
import type { PlanAddress } from '../../../../../electron/symphony-route'
import { symphonyPlanBridge } from '../../../../../electron/symphony-plan-bridge'
import { registerSymphonyRoute } from '../../../../../electron/register-symphony-route'
import { RoutePeer } from '../../../../../e2e/support/symphony-route-peer'
import { SymphonyPlanCard } from '../symphony-plan-card'
import { useSymphonyPlan } from './use-symphony-plan'

const settings = process.env
const ipc = vi.hoisted(() => ({ handle: vi.fn(), window: vi.fn() }))
vi.mock('electron', () => ({ ipcMain: { handle: ipc.handle }, BrowserWindow: { fromWebContents: ipc.window } }))
let nextWindow = 0
const wait = { timeout: 30000 }
const ordinaryRequest = async <T,>(): Promise<T> => {
  throw new Error('Unexpected ordinary gateway fallback')
}

function readCard(target: HTMLElement) {
  const pre = target.querySelector('pre')
  if (!pre) return undefined
  const card = within(target).getByRole('region', { name: 'План Symphony' })
  const request_id = within(card)
    .getByText(/^План Symphony · .+$/)
    .textContent!.slice('План Symphony · '.length)
  const plan_digest = within(card)
    .getByText(/^SHA-256 [0-9a-f]{64}$/)
    .textContent!.slice('SHA-256 '.length)
  return { request_id, plan_digest, plan: JSON.parse(pre.textContent!) as unknown }
}

it('compares full displayed JSON across serialization order and detects changed fields, arrays and bindings', () => {
  const plan = { checks: ['first', 'second'], limits: { count: 1, enabled: true } }
  const digest = 'a'.repeat(64)
  const card = (body: Record<string, unknown>, request_id = 'task', plan_digest = digest) => (
    <SymphonyPlanCard
      view={{ key: 'fixture', state: 'READY', binding: { request_id, plan_digest }, plan: body }}
      check={() => {}}
    />
  )
  const { container, rerender } = render(card(plan))
  const shown = readCard(container)
  expect(shown).toStrictEqual({ request_id: 'task', plan_digest: digest, plan })
  rerender(card({ limits: { enabled: true, count: 1 }, checks: ['first', 'second'] }))
  expect(readCard(container)).toStrictEqual(shown)
  for (const variant of [
    card({ ...plan, limits: { count: 2, enabled: true } }),
    card({ ...plan, limits: { count: 1 } }),
    card({ ...plan, checks: ['second', 'first'] }),
    card(plan, 'other'),
    card(plan, 'task', 'b'.repeat(64))
  ]) {
    rerender(variant)
    expect(readCard(container)).not.toStrictEqual(shown)
  }
})

function mountWindow(
  address: PlanAddress,
  storage: Storage,
  voiceId = 'fixture-final',
  observe?: (state: string, card: ReturnType<typeof readCard>) => void
) {
  const frame = document.createElement('iframe')
  document.body.append(frame)
  const host = frame.contentWindow!
  Object.defineProperty(host, 'localStorage', { value: storage })
  const target = frame.contentDocument!.body
  const events = new EventEmitter()
  const sender = Object.assign(new EventEmitter(), {
    id: ++nextWindow,
    mainFrame: {},
    getURL: () => 'file:///hermes/index.html#/stored-' + encodeURIComponent(address.conversation),
    isDestroyed: () => !frame.isConnected,
    send: (name: string, ...args: unknown[]) => events.emit(name, {}, ...structuredClone(args))
  })
  const client = symphonyPlanBridge({
    invoke: async (name, ...args) => {
      const handle = ipc.handle.mock.calls.findLast(row => row[0] === name)?.[1]
      if (!handle) throw Error('Unknown IPC request')
      return structuredClone(await handle({ sender, senderFrame: sender.mainFrame }, ...structuredClone(args)))
    },
    on: events.on.bind(events),
    removeListener: events.removeListener.bind(events)
  } as Parameters<typeof symphonyPlanBridge>[0])
  const { connectionId, profile, conversation } = address
  // Each simulated Electron window has its own preload/sender and IPC client.
  // The controls supply local text or an already-final synthetic utterance.
  Object.defineProperty(host, 'hermesDesktop', {
    value: {
      voiceWindowGeneration: () => 1,
      symphonyPlan: client
    }
  })
  function Composer() {
    const plan = useSymphonyPlan(conversation, ordinaryRequest, true, host)
    return (
      <>
        {plan.view && <SymphonyPlanCard view={plan.view} check={plan.check} />}
        <input
          aria-label="draft"
          onInput={plan.edit}
          onKeyDown={e => {
            if (e.key === 'Enter') plan.text(e.currentTarget.value)
          }}
        />
        <button onClick={() => plan.beginVoice(voiceId)}>start</button>
        <button onClick={() => plan.endVoice(voiceId)}>end</button>
        <button onClick={() => plan.final('отклоняю', voiceId)}>final</button>
      </>
    )
  }
  const root = render(<Composer />, { container: target })
  const ui = within(target)
  const snapshot = (state: string) => {
    const card = readCard(target)
    observe?.(state, card)
    return card
  }
  return {
    ready: async () => {
      await ui.findByText(/Напишите «подтверждаю»/, {}, wait)
      const shown = snapshot('READY')
      if (!shown) throw Error('Ready card is missing the shown plan')
      return shown
    },
    state: async (label: string) => {
      await ui.findByText(label, {}, wait)
      return snapshot(label)
    },
    text(value: string) {
      const draft = ui.getByLabelText('draft')
      fireEvent.input(draft, { target: { value } })
      fireEvent.keyDown(draft, { key: 'Enter' })
    },
    voice() {
      for (const label of ['start', 'end', 'final']) fireEvent.click(ui.getByText(label))
    },
    readback: (verb: 'plan-result' | 'plan-receipt', data: object) =>
      client.request({ connectionId, profile, conversation }, verb, data),
    close() {
      root.unmount()
      sender.emit('destroyed')
      frame.remove()
    },
    ui,
    target,
    host
  }
}

it('the SSH chat reaches the restricted preload operation and requires a host registration', async () => {
  const address = { connectionId: 'unregistered-ssh', profile: 'work', conversation: 'unregistered-chat' }
  const registry = new PlanRouteRegistry()
  const findRegistration = vi.spyOn(registry, 'registration')
  ipc.window.mockReturnValue({})
  registerSymphonyRoute(
    () => 'file:///hermes/index.html',
    () => true,
    registry
  )
  $connection.set({ mode: 'remote', connectionId: address.connectionId, profile: address.profile } as never)
  $connectionsRegistry.set({ connections: [{ id: address.connectionId, kind: 'ssh' }] } as never)
  recordSessionEventScope({
    session_id: address.conversation,
    profile: address.profile,
    connectionId: address.connectionId
  })
  const win = mountWindow(address, window.localStorage)
  try {
    await waitFor(() => expect(findRegistration).toHaveBeenCalledWith(address))
    expect(win.ui.queryByLabelText('План Symphony')).toBeNull()
  } finally {
    win.close()
    $connectionsRegistry.set(null)
    $connection.set(null)
  }
})

it('shows the plan again after a local refusal and retains the input after ambiguous delivery', async () => {
  const address = { connectionId: 'cursor-ssh', profile: 'work', conversation: 'cursor-chat' }
  const pending = {
    request_id: 'cursor-task',
    digest: 'a'.repeat(64),
    plan: { goal: 'Read fixture' },
    state: 'WAITING_FOR_AUTHORITY'
  }
  const inputIds: string[] = []
  let displays = 0
  ipc.handle('hermes:symphony-plan:request', async (_event, _address, verb, data) => {
    if (verb === 'status') return { output: JSON.stringify({ pending_plan: pending }) }
    if (verb === 'plan-presented') {
      displays++
      return { output: JSON.stringify({ state: 'PRESENTED', plan_digest: pending.digest }) }
    }
    if (verb === 'plan-answer') {
      inputIds.push(data.input_id)
      if (inputIds.length === 1) return { output: '', delivery: 'NOT_SENT' }
      throw Error('lost after send')
    }
    return { output: 'DESKTOP_DECISION_UNKNOWN' }
  })
  $connection.set({ mode: 'remote', connectionId: address.connectionId, profile: address.profile } as never)
  $connectionsRegistry.set({ connections: [{ id: address.connectionId, kind: 'ssh' }] } as never)
  recordSessionEventScope({
    session_id: address.conversation,
    profile: address.profile,
    connectionId: address.connectionId
  })
  const win = mountWindow(address, window.localStorage)
  try {
    await win.ready()
    win.text('подтверждаю')
    await waitFor(() => expect(displays).toBe(2))
    await win.ready()
    expect(inputIds).toHaveLength(1)
    win.text('отклоняю')
    await win.state('Исход решения неизвестен. Требуется сверка журнала.')
    fireEvent.click(win.ui.getByRole('button', { name: 'Сверить' }))
    await win.state('Исход решения неизвестен. Требуется сверка журнала.')
    expect(new Set(inputIds).size).toBe(2)
    expect(inputIds).toHaveLength(2)
  } finally {
    win.close()
    $connectionsRegistry.set(null)
    $connection.set(null)
  }
})

it.runIf(settings.SYMPHONY_ROUTE_CAMPAIGN === '1')(
  'connects the actual Desktop card to the isolated Python/Linux owner',
  async () => {
    const windowsRoot = path.resolve('../..')
    const linuxRoot = settings.SYMPHONY_ROUTE_PEER!
    const heads = [windowsRoot, linuxRoot].map(root =>
      execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim()
    )
    expect(heads).toEqual([settings.SYMPHONY_ROUTE_WINDOWS_SHA, settings.SYMPHONY_ROUTE_LINUX_SHA])
    const dir = settings.SYMPHONY_ROUTE_RECEIPT!
    mkdirSync(dir, { recursive: true })
    const campaignId = randomUUID()
    const voiceId = 'final-' + campaignId
    const observations: object[] = []
    const receipt: Record<string, unknown> = {
      campaign_id: campaignId,
      observations,
      started: new Date().toISOString(),
      windows_sha: heads[0],
      linux_sha: heads[1],
      source: 'control.submit through the existing fixture ingress; Electron plan card through stdio ASGI gateway',
      state: 'IN_PROGRESS'
    }
    const save = (flag = 'w') =>
      writeFileSync(path.join(dir, 'joint-route.json'), JSON.stringify(receipt, null, 2) + '\n', {
        encoding: 'utf8',
        flag
      })
    save('wx')
    const peer = new RoutePeer(settings.HERMES_PYTHON!, linuxRoot, mkdtempSync(path.join(tmpdir(), 'srw-')))
    const windows: ReturnType<typeof mountWindow>[] = []
    const observe = (step: string, details: object) => {
      observations.push({ step, ...details })
      save()
    }
    const submit = async (stage: string) => {
      const args = { action: stage + '-' + campaignId, admission: 'source-' + stage + '-' + campaignId }
      observe('submit requested', args)
      const task = await peer.control('submit', args)
      observe('submit received', task)
      return task
    }
    let windowSequence = 0
    const open = (storage = window.localStorage) => {
      const windowId = ++windowSequence
      const w = mountWindow(peer.registration, storage, voiceId, (state, card) =>
        observe('card', { window: windowId, state, card })
      )
      windows.push(w)
      return w
    }
    const close = (w: ReturnType<typeof mountWindow>) => {
      w.close()
      windows.splice(windows.indexOf(w), 1)
    }
    let started = false
    try {
      await peer.start()
      started = true
      const baseline = await peer.control('counts')
      observe('fresh database', baseline)
      expect(baseline).toStrictEqual({ decisions: 0, permits: 0, reservations: 0, launches: 0, by_request: {} })
      const r = peer.registration
      ipc.window.mockImplementation(sender => (sender.id && !sender.isDestroyed() ? {} : null))
      registerSymphonyRoute(
        () => 'file:///hermes/index.html',
        (_id, a) => a.connectionId === r.connectionId && a.profile === r.profile,
        peer.registry
      )
      receipt.address = {
        connection_id: r.connectionId,
        profile: r.canonicalProfile,
        session: r.session,
        conversation: r.conversation
      }
      $connection.set({ mode: 'remote', connectionId: r.connectionId, profile: r.profile } as never)
      $connectionsRegistry.set({ connections: [{ id: r.connectionId, kind: 'ssh' }] } as never)
      recordSessionEventScope({ session_id: r.conversation, profile: r.profile, connectionId: r.connectionId })
      const first = open()
      await waitFor(() => expect(peer.reads).toContain('status'), wait)
      for (const fault of ['cached', 'reboot', 'missing'] as const) {
        peer.evidence = fault
        const observer = new SymphonyRouteClient(peer.registry, () => {})
        const dials = peer.transports.length
        await expect(observer.request(r, 'status')).rejects.toThrow()
        expect(peer.transports).toHaveLength(dials)
        observer.dispose()
      }
      peer.evidence = 'fresh'
      receipt.k1_closed = ['cached', 'reboot with same key', 'missing']
      const denied = await submit('deny')
      const deniedPlan = await first.ready()
      expect(deniedPlan.request_id).toBe(denied.request_id)
      observe('voice input', { request_id: denied.request_id, input_id: voiceId })
      first.voice()
      const deniedCard = await first.state('План отклонён.')
      const deniedCounts = await peer.control('counts')
      observe('counts after denial', deniedCounts)
      expect(deniedCard).toStrictEqual(deniedPlan)
      expect(deniedCounts).toMatchObject({ decisions: 1, permits: 0, reservations: 0, launches: 0 })
      expect(deniedCounts.by_request[denied.request_id]).toMatchObject({
        input_id: voiceId,
        plan_digest: deniedPlan.plan_digest,
        decision: 'DENIED'
      })
      receipt.denied = { ...denied, input_kind: 'voice', original_window: true, counts: deniedCounts }
      const readsBeforeLoss = [...peer.reads]
      const initialSocket = peer.transports.at(-1)
      for (const fault of ['cached', 'reboot', 'missing'] as const) {
        const observations = peer.observations
        peer.evidence = fault
        if (fault === 'cached') peer.disconnect()
        else fireEvent.click(first.ui.getByRole('button', { name: 'Сверить' }))
        await waitFor(() => expect(peer.observations).toBeGreaterThan(observations), wait)
        expect(await first.state('Для чтения плана требуется проверенное подключение.')).toBeUndefined()
        expect(first.ui.queryByText(/SHA-256/)).toBeNull()
        first.text('подтверждаю')
        expect(peer.reads).toEqual(readsBeforeLoss)
      }
      peer.evidence = 'fresh'
      fireEvent.click(first.ui.getByRole('button', { name: 'Сверить' }))
      expect(await first.state('План отклонён.')).toStrictEqual(deniedPlan)
      expect(peer.transports.at(-1)).not.toBe(initialSocket)
      expect(await peer.control('counts')).toEqual(deniedCounts)
      receipt.original_window_k1 = {
        before: initialSocket,
        after: peer.transports.at(-1),
        held: ['cached', 'reboot-same-key', 'missing'],
        unauthorized_gateway_operations: 0,
        restored_original_decision: true
      }
      save()

      const approved = await submit('approve')
      const originalPlan = await first.ready()
      expect(originalPlan.request_id).toBe(approved.request_id)
      // Give the competing client an isolated browser storage partition. Both
      // windows can deliver their answer even before learning the other's outcome.
      const second = open(window.sessionStorage)
      expect(await second.ready()).toStrictEqual(originalPlan)
      peer.holding = 'after'
      first.text('подтверждаю')
      expect(await first.state('План подтверждён.')).toStrictEqual(originalPlan)
      // A displayed plan in the second window may still be awaiting an answer.
      const beforeConflict = peer.reads.filter(v => v === 'plan-answer').length
      second.text('отклоняю')
      expect(await second.state('План подтверждён.')).toStrictEqual(originalPlan)
      expect(peer.reads.filter(v => v === 'plan-answer')).toHaveLength(beforeConflict + 1)
      const approvedCounts = await peer.control('counts')
      observe('counts after approval', approvedCounts)
      expect(approvedCounts).toMatchObject({ decisions: 2, permits: 1, reservations: 1, launches: 1 })
      const winning = approvedCounts.by_request[approved.request_id]
      expect(winning.decision).toBe('APPROVED')
      expect(winning.plan_digest).toBe(originalPlan.plan_digest)
      const lookup = { request_id: approved.request_id, plan_digest: winning.plan_digest }
      const fromOtherWindow = JSON.parse((await second.readback('plan-result', lookup)).output)
      expect(fromOtherWindow.input_id).toBe(winning.input_id)
      receipt.approved = {
        ...approved,
        input_kind: 'text',
        original_window: true,
        second_window: true,
        counts: approvedCounts
      }
      const transportBeforeRestart = [...peer.transports]
      save()
      close(second)
      close(first)
      await peer.restart()
      const restored = open()
      expect(await restored.state('План подтверждён.')).toStrictEqual(originalPlan)
      expect(await peer.control('counts')).toEqual(approvedCounts)
      await peer.control('legacy', { request_id: approved.request_id })
      close(restored)
      const legacy = open()
      expect(await legacy.state('План подтверждён.')).toBeUndefined()
      expect(await peer.control('counts')).toEqual(approvedCounts)
      receipt.restart = {
        transportBeforeRestart,
        transportAfterRestart: [...peer.transports],
        full_plan: true,
        legacy_body: false
      }
      save()
      const ambiguous = await submit('before')
      expect((await legacy.ready()).request_id).toBe(ambiguous.request_id)
      const sends = peer.reads.filter(v => v === 'plan-answer').length
      peer.holding = 'before'
      legacy.text('отклоняю')
      await legacy.state('Исход решения неизвестен. Требуется сверка журнала.')
      const resultsBeforeCheck = peer.reads.filter(v => v === 'plan-result').length
      fireEvent.click(legacy.ui.getByRole('button', { name: 'Сверить' }))
      await waitFor(
        () => expect(peer.reads.filter(v => v === 'plan-result').length).toBeGreaterThan(resultsBeforeCheck),
        wait
      )
      await legacy.state('Исход решения неизвестен. Требуется сверка журнала.')
      expect(peer.reads.filter(v => v === 'plan-answer')).toHaveLength(sends + 1)
      expect(await peer.control('counts')).toEqual(approvedCounts)
      receipt.before_commit = { ...ambiguous, state: 'UNKNOWN', readback_only: true, counts: approvedCounts }
      close(legacy)
      save()
      const privacy = await peer.control('privacy', { request_id: denied.request_id })
      receipt.privacy = privacy
      save()
      expect(privacy.request_id).toBe(denied.request_id)
      expect(privacy).toMatchObject({
        conversation_id: r.conversation,
        session_id: r.session,
        input_id: deniedCounts.by_request[denied.request_id].input_id,
        plan_digest: deniedCounts.by_request[denied.request_id].plan_digest
      })
      expect(privacy.before).toEqual(approvedCounts)
      expect(privacy.after).toEqual(privacy.before)
      const none = { live: 0, replay: 0, history: 0, status: 0, receipt: 0 }
      expect(privacy.matrix).toEqual({ ordinary: none, revoked: none, neighbor: none })
      expect(privacy.controls).toMatchObject({
        authorized_receipt: 1,
        authorized_result: 1,
        authorized_live: 1,
        authorized_status: 1,
        default: { live: 1, replay: 1 },
        beta: { live: 1, replay: 1 }
      })
      receipt.state = 'PASS'
    } finally {
      if (started && receipt.state !== 'PASS') {
        receipt.final_counts = await peer.control('counts').catch(() => 'UNKNOWN')
      }
      for (const w of windows) w.close()
      receipt.finished = new Date().toISOString()
      receipt.transport_ids = peer.transports
      receipt.operations = peer.reads
      if (receipt.state !== 'PASS') receipt.state = 'INCOMPLETE'
      save()
      await peer.shutdown()
      $connectionsRegistry.set(null)
      $connection.set(null)
    }
  },
  240000
)
