import { act, cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'
import { $voicePlayback, setLiveVoiceOutput } from '@/store/voice-playback'
import { $symphonyPlanSignals, signalSymphonyPlan } from '@/store/symphony-plan'
import type { GatewayRequest } from '@/app/session/hooks/use-prompt-actions/utils'
import { SymphonyPlanCard } from '../symphony-plan-card'
import { useSymphonyPlan } from './use-symphony-plan'
import { waitForReadyPlan } from '../../../../../e2e/support/forward-plan-display'

const binding = { request_id: 'task-one', plan_digest: 'a'.repeat(64) }
const plan = { goal: 'Inspect the fixture', attempt_budget: 1 }
beforeEach(() => {
  vi.stubGlobal('hermesDesktop', { voiceWindowGeneration: () => 1 })
  localStorage.clear()
  $connection.set({ authMode: 'native', mode: 'local', connectionId: 'local' } as never)
  signalSymphonyPlan('runtime', binding.plan_digest)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  $symphonyPlanSignals.set({})
  $connection.set(null)
  localStorage.clear()
})

function setup(outcome: string, lost = false, duringDisplay?: 'changed' | 'decided', presentation?: Promise<void>) {
  let pending: typeof binding | undefined = binding
  let recorded: Record<string, unknown> | undefined
  let readable = !lost
  const verbs: string[] = []
  const request = vi.fn(async (_method: string, params?: Record<string, unknown>) => {
    expect(params?.session_id).toBe('runtime')
    const [verb, ...rest] = String(params?.arg).split(' ')
    verbs.push(verb)
    if (verb === 'status') {
      return {
        output: JSON.stringify({
          pending_plan: pending && {
            digest: pending.plan_digest,
            request_id: pending.request_id,
            plan,
            state: 'WAITING_FOR_AUTHORITY'
          }
        })
      }
    }
    const input = JSON.parse(rest.join(' '))
    expect(params?.interaction).toBe('displayed-plan')
    if (verb === 'plan-presented') {
      if (presentation) await presentation
      expect(screen.getByText(/Inspect the fixture/)).toBeTruthy()
      if (duringDisplay && pending?.request_id === binding.request_id) {
        if (duringDisplay === 'decided') {
          recorded = {
            ...input,
            input_id: 'winner-before-display',
            input_kind: 'text',
            decision: 'APPROVED',
            shown_plan: plan
          }
        }
        pending = undefined
        return { output: duringDisplay === 'changed' ? 'TYPED_PLAN_CHANGED' : 'NO_MATCHING_DESKTOP_PLAN' }
      }
      return { output: JSON.stringify({ state: 'PRESENTED', plan_digest: input.plan_digest }) }
    }
    if (verb === 'plan-answer') {
      if (outcome === 'TYPED_PLAN_CHANGED') {
        pending = undefined
        return { output: outcome }
      }
      if (outcome === 'OTHER_VIEWER') {
        recorded = {
          ...input,
          input_id: 'other-window-final',
          input_kind: 'voice',
          decision: 'DENIED',
          shown_plan: plan
        }
        if (lost) {
          throw Error('reply lost')
        }
        return { output: 'NO_MATCHING_DESKTOP_PLAN' }
      }
      expect(recorded).toBeUndefined()
      recorded = { ...input, decision: outcome, shown_plan: plan }
      if (lost) {
        throw Error('reply lost')
      }
    }
    const found = readable && recorded && (verb !== 'plan-receipt' || input.input_id === recorded.input_id)
    return { output: found ? JSON.stringify(recorded) : 'DESKTOP_DECISION_UNKNOWN' }
  }) as GatewayRequest

  function Composer() {
    const state = useSymphonyPlan('runtime', request, true)
    return (
      <>
        {state.view && <SymphonyPlanCard view={state.view} check={state.check} />}
        <input
          aria-label="draft"
          onInput={state.edit}
          onKeyDown={e => {
            if (e.key === 'Enter') {
              state.text(e.currentTarget.value)
            }
          }}
        />
        <button onClick={() => state.beginVoice('fixture-utterance')}>open</button>
        <button onClick={() => state.endVoice('fixture-utterance')}>end</button>
        <button onClick={() => state.final('отклоняю', 'fixture-utterance')}>final</button>
      </>
    )
  }
  return {
    mount: () => render(<Composer />),
    verbs,
    replace() {
      pending = { request_id: 'new-request', plan_digest: 'b'.repeat(64) }
      signalSymphonyPlan('runtime', pending.plan_digest)
    },
    allowRead: () => {
      readable = true
    },
    receipt: () => recorded
  }
}

it('waits for the same card to become ready before typing a fresh decision', async () => {
  let acknowledge!: () => void
  const barrier = new Promise<void>(resolve => { acknowledge = resolve })
  const h = setup('DENIED', false, undefined, barrier)
  const ui = h.mount()
  await screen.findByText('SHA-256 ' + binding.plan_digest)
  expect(screen.getByText('Показ плана…')).toBeTruthy()
  const field = screen.getByLabelText('draft')
  const edit = () => fireEvent.input(field, { target: { value: 'отклоняю' } })
  const enter = () => fireEvent.keyDown(field, { key: 'Enter' })
  edit()
  enter()
  expect(h.verbs).not.toContain('plan-answer')
  const ready = waitForReadyPlan(ui.container, binding.request_id, binding.plan_digest)
  await act(async () => acknowledge())
  await ready
  enter()
  expect(h.verbs).not.toContain('plan-answer')
  edit()
  enter()
  await screen.findByText('План отклонён.')
  expect(h.verbs.filter(v => v === 'plan-answer')).toHaveLength(1)
  expect(h.receipt()).toMatchObject({ ...binding, decision: 'DENIED' })
})

it('records fresh editor input only after the complete plan is shown; reads an uncertain receipt on remount', async () => {
  const h = setup('APPROVED', true)
  const first = h.mount()
  await screen.findByText(/Напишите/)
  const field = screen.getByLabelText('draft')
  fireEvent.input(field, { target: { value: 'подтверждаю' } })
  fireEvent.keyDown(field, { key: 'Enter' })
  await screen.findByText(/Исход решения неизвестен/)
  expect(h.receipt()).toMatchObject({ ...binding, input_kind: 'text', text: 'подтверждаю' })
  first.unmount()
  h.allowRead()
  h.mount()
  await screen.findByText('План подтверждён.')
  expect(screen.getByText(/Inspect the fixture/)).toBeTruthy()
  expect(h.verbs.filter(v => v === 'plan-answer')).toHaveLength(1)
  expect(h.verbs.filter(v => v === 'plan-receipt')).toHaveLength(2)
})

it('requires a final from a capture opened for this displayed plan and deduplicates the repeated final', async () => {
  // The renderer reopened before the decision and missed the plan notification.
  $symphonyPlanSignals.set({})
  const h = setup('DENIED')
  const first = h.mount()
  await screen.findByText(/Напишите/)
  fireEvent.click(screen.getByText('final'))
  expect(h.verbs).not.toContain('plan-answer')
  fireEvent.click(screen.getByText('open'))
  expect(h.verbs).not.toContain('plan-answer')
  fireEvent.click(screen.getByText('end'))
  const field = screen.getByLabelText('draft')
  fireEvent.input(field, { target: { value: 'не подтверждаю' } })
  fireEvent.keyDown(field, { key: 'Enter' })
  expect(h.verbs).not.toContain('plan-answer')
  act(() => {
    fireEvent.click(screen.getByText('final'))
    fireEvent.click(screen.getByText('final'))
  })
  await waitFor(() => expect(screen.getByText('План отклонён.')).toBeTruthy())
  expect(h.receipt()).toMatchObject({
    ...binding,
    input_kind: 'voice',
    input_id: 'fixture-utterance',
    text: 'отклоняю'
  })
  expect(h.verbs.filter(v => v === 'plan-answer')).toHaveLength(1)
  first.unmount()
  h.mount()
  await screen.findByText('План отклонён.')
  expect(screen.getByText(/Inspect the fixture/)).toBeTruthy()
})

it('keeps a pre-commit retirement distinct from a lost reply and shows the replacement request', async () => {
  const h = setup('TYPED_PLAN_CHANGED')
  h.mount()
  await screen.findByText(/Напишите/)
  const field = screen.getByLabelText('draft')
  fireEvent.input(field, { target: { value: 'подтверждаю' } })
  fireEvent.keyDown(field, { key: 'Enter' })
  await screen.findByText(/Прежний план закрыт/)
  act(() => h.replace())
  await screen.findByText(/new-request/)
  await screen.findByText(/Напишите/)
  expect(h.verbs.filter(verb => verb === 'plan-answer')).toHaveLength(1)
})

it.each([false, true])('reads the winning input after another viewer settles the same plan, lost=%s', async lost => {
  const h = setup('OTHER_VIEWER', lost)
  h.mount()
  await screen.findByText(/Напишите/)
  const field = screen.getByLabelText('draft')
  fireEvent.input(field, { target: { value: 'подтверждаю' } })
  fireEvent.keyDown(field, { key: 'Enter' })
  if (lost) {
    await screen.findByText(/Исход решения неизвестен/)
    h.allowRead()
    fireEvent.click(screen.getByText('Сверить'))
  }
  await screen.findByText(/other-window-final/)
  expect(screen.getByText('План отклонён.')).toBeTruthy()
  expect(h.verbs).toContain('plan-result')
  act(() => h.replace())
  await screen.findByText(/new-request/)
  expect(h.verbs.filter(v => v === 'plan-answer')).toHaveLength(1)
})

it.each(['speaking', 'live-track', 'second-window', 'unobserved-window'])(
  'holds overlapping synthetic speech (%s) and accepts a clean next capture',
  async mode => {
    let generation: number | null = 1
    vi.stubGlobal('hermesDesktop', { voiceWindowGeneration: () => generation })
    const h = setup('DENIED')
    h.mount()
    await screen.findByText(/Напишите/)
    const idle = $voicePlayback.get()
    if (mode === 'unobserved-window') {
      generation = null
    }
    fireEvent.click(screen.getByText('open'))
    if (mode === 'speaking') {
      for (const status of ['preparing', 'speaking', 'idle'] as const) {
        $voicePlayback.set({ ...idle, status })
      }
    } else if (mode === 'live-track') {
      const audio = document.createElement('audio')
      setLiveVoiceOutput(audio, true)
      setLiveVoiceOutput(audio, false)
    } else {
      generation = 2
    }
    fireEvent.click(screen.getByText('end'))
    fireEvent.click(screen.getByText('final'))
    expect(h.verbs).not.toContain('plan-answer')
    fireEvent.click(screen.getByText('open'))
    fireEvent.click(screen.getByText('end'))
    fireEvent.click(screen.getByText('final'))
    await screen.findByText('План отклонён.')
    expect(h.verbs.filter(v => v === 'plan-answer')).toHaveLength(1)
  }
)

it.each(['changed', 'decided'] as const)(
  'reconciles a %s plan between status and display acknowledgement',
  async how => {
    const h = setup('APPROVED', false, how)
    h.mount()
    await screen.findByText(how === 'decided' ? /winner-before-display/ : /Прежний план закрыт/)
    act(() => h.replace())
    await screen.findByText(/new-request/)
    await screen.findByText(/Напишите/)
    expect(h.verbs.filter(v => v === 'plan-presented')).toHaveLength(2)
    expect(h.verbs).not.toContain('plan-answer')
  }
)

it('retains text decisions while an unobservable live audio track is attached', async () => {
  const audio = document.createElement('audio')
  setLiveVoiceOutput(audio, true)
  try {
    const h = setup('APPROVED')
    h.mount()
    await screen.findByText(/Напишите/)
    fireEvent.click(screen.getByText('open'))
    fireEvent.click(screen.getByText('end'))
    fireEvent.click(screen.getByText('final'))
    expect(h.verbs).not.toContain('plan-answer')
    const field = screen.getByLabelText('draft')
    fireEvent.input(field, { target: { value: 'подтверждаю' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    await screen.findByText('План подтверждён.')
    expect(h.receipt()).toMatchObject({ input_kind: 'text' })
  } finally {
    setLiveVoiceOutput(audio, false)
  }
})
