import type { OperatorCall, OperatorReply } from '../../electron/symphony-operator-types'
import type { SubmissionDestination } from '@/app/session/hooks/use-prompt-actions/submission-destination'
import { dismissNotification, notify } from '@/store/notifications'

export async function requestSymphonyOperator(
  destination: SubmissionDestination, conversation: string | null, call: OperatorCall
): Promise<OperatorReply> {
  const bridge = window.hermesDesktop?.symphonyOperator
  const owner = destination.owner
  const location = window.location.href
  if (!bridge || destination.taskTransport !== 'ssh' || !owner || typeof owner !== 'object' ||
      !conversation || destination.current?.() === false) return { state: 'HOLD' }
  try {
    const result = await bridge.request({ connectionId: owner.connectionId, profile: owner.profile, conversation }, call)
    if (location === window.location.href && destination.current?.() !== false) return result
  } catch {
    // A broken IPC reply cannot distinguish a refusal from a committed write.
  }
  return { state: 'UNKNOWN' }
}

export function showSymphonyOperatorNotice(message: string, title = 'Symphony') {
  let unsubscribe: (() => void) | undefined
  const clear = () => dismissNotification(id)
  const id = notify({ title, message, onDismiss: () => {
    unsubscribe?.()
    window.removeEventListener('hashchange', clear)
  } })
  unsubscribe = window.hermesDesktop?.symphonyOperator?.onClear?.(clear)
  window.addEventListener('hashchange', clear)
}

export async function reconcileSymphonyInput(destination: SubmissionDestination, conversation: string, submissionId: string) {
  const result = await requestSymphonyOperator(destination, conversation, { action: 'receipt', submissionId })
  if (result.state !== 'ADMITTED') throw new Error('UNKNOWN · Персональная квитанция не подтверждена.')
  showSymphonyOperatorNotice(`Квитанция ${result.receipt.admission_id} · ${result.receipt.status}`)
}

function stopFact(value: boolean | undefined, negative: string) {
  if (value === undefined) return 'неизвестно'
  return value ? 'подтверждено' : negative
}

export async function handleSymphonyPatchSlash(
  destination: SubmissionDestination, conversation: string | null, text: string, argument: string,
  fromComposer: boolean, render: (message: string) => void
) {
  const action = argument.split(/\s+/, 1)[0]?.toLowerCase()
  if (action !== 'apply' && action !== 'archive' && action !== 'apply-result') return false
  if (!fromComposer || destination.taskTransport !== 'ssh') {
    render('HOLD · Введите команду в зарегистрированном управляющем окне Symphony.')
    return true
  }

  const result = await requestSymphonyOperator(destination, conversation, { action: 'patch-decision', text })
  if (result.state === 'PATCH') {
    const { command, observation, commit } = result
    showSymphonyOperatorNotice([
      `${observation} · ${command.job} · ${command.id}`,
      `CAPTURE ${command.captureSeq} · CHECK ${command.checkSeq} · SHA-256 ${command.patchSha256}`,
      ...(commit ? [`Коммит ${commit}`] : []),
      ...(observation === 'UNKNOWN' ? [`Для сверки введите /symphony apply-result ${command.job} ${command.patchSha256}`] : []),
    ].join('\n'))
  } else if (result.state === 'UNKNOWN') {
    showSymphonyOperatorNotice('UNKNOWN · Для сверки введите /symphony apply-result с исходными job и SHA-256.')
  } else {
    render(`HOLD · Проверьте полномочия окна, job и полную SHA-256 для /symphony ${action}.`)
  }
  return true
}

export async function handleSymphonyOperatorSlash(
  destination: SubmissionDestination, conversation: string | null, argument: string, render: (message: string) => void
) {
  const [name, selector, ...rest] = argument.trim().split(/\s+/)
  const action = name.toLowerCase()
  if (!['status', 'stop', 'stop-reconcile', 'stop-replace', 'receipt', 'restore-window', 'installation-key'].includes(action)) return false
  if (rest.length || (!selector && (action === 'stop-reconcile' || action === 'stop-replace'))) {
    render('HOLD · Укажите одну точную цель Symphony. Команда не отправлена.')
    return true
  }
  const bridge = window.hermesDesktop?.symphonyOperator
  if (bridge && !selector && action === 'restore-window') {
    await bridge.restoreWindow()
    return true
  }
  if (bridge && !selector && action === 'installation-key') {
    await bridge.copyInstallationKey()
    return true
  }
  if (action === 'status' || action === 'stop' || action === 'receipt' || action === 'stop-reconcile' || action === 'stop-replace') {
    const call: OperatorCall = action === 'receipt' ? { action, submissionId: selector } : { action, attemptId: selector }
    const result = await requestSymphonyOperator(destination, conversation, call)
    switch (result.state) {
      case 'ADMITTED':
        showSymphonyOperatorNotice(`Квитанция ${result.receipt.admission_id} · ${result.receipt.status}`)
        return true
      case 'STATUS':
        showSymphonyOperatorNotice(`${result.attemptId} · ${result.phase} · наблюдение ${result.observedAt}${result.evidence ? ` · ${result.evidence}` : ''}`)
        return true
      case 'STOP':
        showSymphonyOperatorNotice([
          `${result.commandId} · ${result.observation}`,
          ...(result.previousCommandId ? [`Исходный STOP ${result.previousCommandId}`] : []),
          `Обращение к исполнителю — ${stopFact(result.dispatched, 'нет')}`,
          `Локальная остановка — ${stopFact(result.localStopped, 'без подтверждения')}`,
          ...(result.observation === 'UNKNOWN' && !result.previousCommandId
            ? [`Для сверки введите /symphony stop-reconcile ${result.attemptId}`] : []),
          ...(result.observation === 'NOT_ACCEPTED' && !result.previousCommandId
            ? [`Для нового STOP введите /symphony stop-replace ${result.attemptId}`] : []),
        ].join(' · '), 'Symphony STOP')
        return true
      case 'UNKNOWN':
        showSymphonyOperatorNotice('UNKNOWN · Исход уточняется по квитанции. Повторная отправка удержана.')
        return true
    }
  }
  render('HOLD · Действие Symphony удержано для указанной попытки.')
  return true
}
