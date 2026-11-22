import type { useSymphonyPlan } from './hooks/use-symphony-plan'

const labels: Record<string, string> = {
  RETIRED: 'Прежний план закрыт. Для продолжения нужен новый показ.',
  DISPLAY: 'Показ плана…',
  READY: 'Напишите «подтверждаю» или «отклоняю». При включённом голосовом управлении ответ можно произнести.',
  CHECKING: 'Сверяю решение с журналом…',
  UNKNOWN: 'Исход решения неизвестен. Требуется сверка журнала.',
  UNAVAILABLE: 'Подтверждение показа недоступно.',
  APPROVED: 'План подтверждён.',
  DENIED: 'План отклонён.'
}

export function SymphonyPlanCard({
  view,
  voiceHeld,
  check
}: {
  view: NonNullable<ReturnType<typeof useSymphonyPlan>['view']>
  voiceHeld?: boolean
  check: () => void
}) {
  if (view.state === 'RECONNECT') {
    return (
      <section aria-label="Подключение Symphony" className="rounded-lg border bg-background p-3 text-sm">
        <p role="status">Для чтения плана требуется проверенное подключение.</p>
        <button type="button" onClick={check}>
          Сверить
        </button>
      </section>
    )
  }
  return (
    <section aria-label="План Symphony" className="rounded-lg border bg-background p-3 text-sm">
      <strong>План Symphony · {view.binding.request_id}</strong>
      {view.plan && (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap">{JSON.stringify(view.plan, null, 2)}</pre>
      )}
      <div className="break-all font-mono text-xs">SHA-256 {view.binding.plan_digest}</div>
      <p role="status">{labels[view.state]}</p>
      {view.input && (
        <p>
          Ввод {view.input.input_kind === 'voice' ? 'голосом' : 'текстом'} · {view.input.input_id}
        </p>
      )}
      {voiceHeld && (
        <p>
          Для этой записи не удалось исключить голос Hermes. Введите ответ текстом или повторите его после окончания
          воспроизведения в единственном окне Hermes.
        </p>
      )}
      {['UNKNOWN', 'UNAVAILABLE'].includes(view.state) && (
        <button type="button" onClick={check}>
          Сверить
        </button>
      )}
    </section>
  )
}
