import { randomUuid } from '@hermes/shared'
import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { captureSubmissionDestination } from '@/app/session/hooks/use-prompt-actions/submission-destination'
import type { GatewayRequest } from '@/app/session/hooks/use-prompt-actions/utils'
import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import type { PlanOperation } from '../../../../../electron/symphony-route'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection } from '@/store/session'
import { $symphonyPlanSignals } from '@/store/symphony-plan'
import { $liveVoiceOutput, $voicePlayback } from '@/store/voice-playback'

interface Binding {
  request_id: string
  plan_digest: string
}
interface Plan extends Binding {
  plan: Record<string, unknown>
}
interface Input extends Binding {
  input_id: string
  input_kind: 'text' | 'voice'
  retired?: boolean
}
interface View {
  key: string
  binding: Binding
  plan?: Plan['plan']
  state: string
  input?: Input
}

const matches = (a: Binding, b: Binding) => a.request_id === b.request_id && a.plan_digest === b.plan_digest
const isDecision = (text: string) => ['подтверждаю', 'отклоняю'].includes(text.trim().toLowerCase())

function singleWindowGeneration(host: Window) {
  try {
    return host.hermesDesktop?.voiceWindowGeneration?.() ?? null
  } catch {
    return null
  }
}

export function useSymphonyPlan(
  session: string | null | undefined,
  request: GatewayRequest,
  main: boolean,
  host: Window = window
) {
  const { document, localStorage } = host
  const visible = usePaneVisible() && main
  const profile = useStore($activeGatewayProfile)
  const connection = useStore($connection)
  const registry = useStore($connectionsRegistry)
  const signal = useStore($symphonyPlanSignals)[session ?? '']
  const destination = useMemo(
    () => captureSubmissionDestination(session, request),
    [session, request, profile, connection]
  )
  const remote = useMemo(() => {
    const owner = destination.owner
    if (
      destination.nativeLocal ||
      !session ||
      !owner ||
      typeof owner !== 'object' ||
      !registry?.connections.some(entry => entry.kind === 'ssh' && entry.id === owner.connectionId)
    )
      return
    return { connectionId: owner.connectionId, profile: owner.profile, conversation: session }
  }, [destination, session, registry])
  const key = remote
    ? JSON.stringify(['symphony-route-v1', remote.connectionId, remote.profile, remote.conversation])
    : JSON.stringify(['symphony-decision-v1', destination.scopeKey, session])
  const displayKey = key + ':display'
  const [view, setView] = useState<View>()
  const [revision, refresh] = useState(0)
  const [onScreen, setOnScreen] = useState(document.visibilityState !== 'hidden')
  const presented = useRef<View | undefined>(undefined)
  const typed = useRef<View | undefined>(undefined)
  const spoken = useRef<
    { id: string; generation?: number | null; listening: boolean; blocked: boolean; plan?: View } | undefined
  >(undefined)
  const [voiceHold, setVoiceHold] = useState<View>()
  const inFlight = useRef<string | undefined>(undefined)
  const current = (remote || destination.nativeLocal) && visible && onScreen && view?.key === key ? view : undefined

  useEffect(() => {
    if (!remote) return
    return host.hermesDesktop?.symphonyPlan?.onSignal((target, disconnected) => {
      if (
        target.connectionId !== remote.connectionId ||
        target.profile !== remote.profile ||
        target.conversation !== remote.conversation
      )
        return
      if (disconnected) {
        presented.current = undefined
        typed.current = undefined
        setView(old => (old?.key === key ? { key, binding: old.binding, state: 'RECONNECT' } : undefined))
      }
      refresh(n => n + 1)
    })
  }, [remote, host])

  useEffect(() => {
    function observe(active: boolean) {
      if (spoken.current?.listening && active) {
        spoken.current.blocked = true
      }
    }
    const stopTts = $voicePlayback.listen(playback => observe(playback.status === 'speaking'))
    const stopLive = $liveVoiceOutput.listen(observe)
    return () => {
      stopTts()
      stopLive()
    }
  }, [])

  useEffect(() => {
    const update = () => setOnScreen(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [document])

  const command = async (verb: PlanOperation, data?: object) => {
    const answer = remote
      ? await host.hermesDesktop!.symphonyPlan!.request(remote, verb, data)
      : await destination.requestGateway<{ output?: string }>('command.dispatch', {
          session_id: session,
          name: 'symphony',
          arg: data ? verb + ' ' + JSON.stringify(data) : verb,
          interaction: 'displayed-plan'
        })
    if (remote && 'delivery' in answer && answer.delivery === 'NOT_SENT') return { state: 'NOT_SENT' }
    // This exact plugin response follows validation before the journal commits.
    // Transport failures and unrecognized replies must keep the original input for readback.
    if (answer.output === 'TYPED_PLAN_CHANGED' || answer.output === 'NO_MATCHING_DESKTOP_PLAN') {
      return { state: 'RETIRED' }
    }
    if (answer.output === 'DESKTOP_DECISION_UNKNOWN') {
      return null
    }
    return JSON.parse(answer.output ?? '')
  }

  useEffect(() => {
    let active = true
    presented.current = undefined
    typed.current = undefined
    if (!session || !visible || !onScreen || !(destination.nativeLocal || remote)) {
      return
    }
    const load = async () => {
      const saved = localStorage.getItem(key)
      const input: Input | undefined = saved ? JSON.parse(saved) : undefined
      const displayed: (Binding & { closed?: boolean }) | null = JSON.parse(localStorage.getItem(displayKey) ?? 'null')
      if (!input && displayed) {
        const winner = await command('plan-result', displayed)
        if (!active) {
          return
        }
        if (
          winner &&
          matches(displayed, winner) &&
          typeof winner.input_id === 'string' &&
          ['text', 'voice'].includes(winner.input_kind) &&
          ['APPROVED', 'DENIED'].includes(winner.decision)
        ) {
          if (!localStorage.getItem(key)) {
            localStorage.setItem(
              key,
              JSON.stringify({ ...displayed, input_id: winner.input_id, input_kind: winner.input_kind })
            )
          }
          refresh(n => n + 1)
          return
        }
      }
      if (input) {
        let state = 'UNKNOWN'
        let shown: Plan['plan'] | undefined
        let winning: Input | undefined
        try {
          const original = await command('plan-receipt', input)
          const receipt =
            original ?? (await command('plan-result', { request_id: input.request_id, plan_digest: input.plan_digest }))
          if (
            receipt &&
            matches(input, receipt) &&
            typeof receipt.input_id === 'string' &&
            ['text', 'voice'].includes(receipt.input_kind) &&
            (!original || (receipt.input_id === input.input_id && receipt.input_kind === input.input_kind)) &&
            ['APPROVED', 'DENIED'].includes(receipt.decision)
          ) {
            state = receipt.decision
            shown = receipt.shown_plan
            winning = {
              request_id: input.request_id,
              plan_digest: input.plan_digest,
              input_id: receipt.input_id,
              input_kind: receipt.input_kind
            }
          } else if (!receipt && input.retired) {
            state = 'RETIRED'
            if (active && localStorage.getItem(key) === saved) {
              localStorage.removeItem(key)
            }
          }
        } finally {
          if (active) {
            setView(old => ({
              key,
              binding: input,
              input: winning,
              plan: shown ?? (!remote && old?.key === key && matches(old.binding, input) ? old.plan : undefined),
              state
            }))
          }
        }
        if (!active || state === 'UNKNOWN') {
          return
        }
      }
      const status = await command('status')
      const pending = status?.pending_plan
      if (!active) {
        return
      }
      if (
        pending?.state === 'WAITING_FOR_AUTHORITY' &&
        typeof pending.digest === 'string' &&
        pending.digest !== input?.plan_digest &&
        !(
          displayed?.closed &&
          displayed.plan_digest === pending.digest &&
          displayed.request_id === pending.request_id
        ) &&
        typeof pending.request_id === 'string' &&
        pending.plan
      ) {
        if (input && localStorage.getItem(key) === saved) {
          localStorage.removeItem(key)
        }
        setView({
          key,
          binding: { request_id: pending.request_id, plan_digest: pending.digest },
          plan: pending.plan,
          state: 'DISPLAY'
        })
      } else if (!input) {
        setView(old => (old?.key === key && old.state === 'RETIRED' ? old : undefined))
      }
    }
    void load().catch(() => {
      if (active) {
        setView(old =>
          remote
            ? old?.key === key
              ? { key, binding: old.binding, state: 'RECONNECT' }
              : undefined
            : old?.key === key && ['READY', 'DISPLAY', 'CHECKING'].includes(old.state)
              ? { ...old, state: 'UNAVAILABLE' }
              : old
        )
      }
    })
    return () => {
      active = false
    }
  }, [destination, remote, key, session, signal, visible, onScreen, revision])

  // Acknowledge only after React committed the complete card in this visible pane.
  useEffect(() => {
    if (!current || current.state !== 'DISPLAY') {
      return
    }
    let active = true
    void command('plan-presented', current.binding)
      .then(result => {
        if (!active) {
          return
        }
        if (result?.state === 'PRESENTED' && result.plan_digest === current.binding.plan_digest) {
          localStorage.setItem(displayKey, JSON.stringify(current.binding))
          const ready = { ...current, state: 'READY' }
          presented.current = ready
          setView(ready)
        } else if (result?.state === 'RETIRED') {
          localStorage.setItem(displayKey, JSON.stringify({ ...current.binding, closed: true }))
          setView({ ...current, state: 'RETIRED' })
          refresh(n => n + 1)
        } else {
          setView(remote ? { key, binding: current.binding, state: 'RECONNECT' } : { ...current, state: 'UNAVAILABLE' })
        }
      })
      .catch(() => {
        if (active) {
          setView(remote ? { key, binding: current.binding, state: 'RECONNECT' } : { ...current, state: 'UNAVAILABLE' })
        }
      })
    return () => {
      active = false
    }
  }, [current, destination])

  function answer(text: string, kind: Input['input_kind'], id?: string): boolean {
    if (!current || document.visibilityState === 'hidden' || !isDecision(text)) {
      return false
    }
    if (kind === 'voice' && !id) {
      return true
    }
    const capture = spoken.current
    const bound =
      kind === 'text'
        ? typed.current
        : capture && capture.id === id && !capture.listening && !capture.blocked
          ? capture.plan
          : undefined
    if (
      !bound ||
      bound !== current ||
      presented.current !== current ||
      current.state !== 'READY' ||
      inFlight.current === key
    ) {
      if (kind === 'voice' && current.state === 'READY') {
        setVoiceHold(current)
      }
      return true
    }
    const input = { ...current.binding, input_id: id ?? randomUuid(), input_kind: kind }
    try {
      if (localStorage.getItem(key)) {
        refresh(n => n + 1)
        return true
      }
      localStorage.setItem(key, JSON.stringify(input))
    } catch {
      setView({ ...current, state: 'UNAVAILABLE' })
      return true
    }
    inFlight.current = key
    presented.current = undefined
    setView({ ...current, state: 'CHECKING' })
    void command('plan-answer', { ...input, text })
      .then(result => {
        if (result?.state === 'NOT_SENT' && localStorage.getItem(key) === JSON.stringify(input)) {
          localStorage.removeItem(key)
        } else if (result?.state === 'RETIRED' && localStorage.getItem(key) === JSON.stringify(input)) {
          localStorage.setItem(key, JSON.stringify({ ...input, retired: true }))
        }
      })
      .catch(() => undefined)
      .finally(() => {
        inFlight.current = undefined
        refresh(n => n + 1)
      })
    return true
  }
  return {
    view: current,
    voiceHeld: Boolean(current && voiceHold === current),
    check: () => refresh(n => n + 1),
    edit: () => {
      typed.current = presented.current
    },
    beginVoice: (id: string) => {
      const ready = presented.current
      const generation = ready && singleWindowGeneration(host)
      spoken.current = {
        id,
        generation,
        listening: true,
        plan: ready,
        blocked: !Number.isInteger(generation) || $voicePlayback.get().status === 'speaking' || $liveVoiceOutput.get()
      }
    },
    endVoice: (id: string) => {
      const capture = spoken.current
      if (capture?.id === id && capture.listening) {
        capture.listening = false
        if (!capture.blocked && capture.generation !== singleWindowGeneration(host)) {
          capture.blocked = true
        }
      }
    },
    text: (text: string) => answer(text, 'text'),
    final: (text: string, id: string) => answer(text, 'voice', id)
  }
}
