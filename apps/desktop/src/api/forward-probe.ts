import type { ForwardProbeResult, ForwardSubmitParams, ForwardSubmitResult } from '@hermes/shared'
import { JsonRpcGatewayError } from '@hermes/shared'

/** Renderer-only evidence. An RPC transport must redeem the same probe on the
 * same socket generation. Symbol keys are never serialized on JSON-RPC. */
export const probedRoute = Symbol('probed SSH route')

export class ForwardBeforeSend extends Error {
  constructor(message = 'HOLD · Подключение или выбранный разговор изменились до передачи текста.') {
    super(message)
  }
}

export class ForwardConflict extends Error {
  constructor() {
    super('Конфликт ввода · Владелец журнала уже связал этот ID с другим текстом. Изменённый текст не принят.')
  }
}

export interface ForwardRoute extends Omit<ForwardProbeResult, 'mode'> {
  route: Exclude<ForwardProbeResult['route'], 'hold'>
  mode: 'shared' | 'personal'
  ref: ForwardSubmitParams['ref']
  generation: string
}

export function isForwardRoute(value: Partial<ForwardRoute> | null, storedId: string): value is ForwardRoute {
  return value?.version === 1 &&
    ((value.mode === 'shared' && value.route === 'ordinary') ||
      ((value.mode === 'shared' || value.mode === 'personal') && value.route === 'forward')) &&
    typeof value.generation === 'string' && Boolean(value.generation.trim()) &&
    typeof value.ref?.profile_id === 'string' && Boolean(value.ref.profile_id.trim()) &&
    value.ref.session_id === storedId
}

export interface ProbedParams extends Record<string, unknown> {
  [probedRoute]?: { route: ForwardRoute; current: () => boolean }
}

export async function sendForwardInput(
  request: <T>(method: string, params: Record<string, unknown>, timeoutMs: number) => Promise<T>,
  binding: NonNullable<ProbedParams[typeof probedRoute]>,
  runtimeId: string,
  submissionId: string,
  text: string,
  timeoutMs: number
): Promise<ForwardSubmitResult> {
  if (!binding.current()) throw new ForwardBeforeSend()
  if (binding.route.mode !== 'shared') throw new ForwardBeforeSend()
  const { ref, generation } = binding.route
  const params: ForwardSubmitParams = { session_id: runtimeId, ref, generation, submission_id: submissionId, text }
  const result = await request<ForwardSubmitResult>('prompt.forward_submit', { ...params, [probedRoute]: binding }, timeoutMs).catch(error => {
    if (error instanceof ForwardBeforeSend) throw error
    if (binding.current() && error instanceof JsonRpcGatewayError &&
        error.code === 4090 && error.message === 'admission_conflict') {
      throw new ForwardConflict()
    }
    return null
  })
  if (!binding.current() || !result || result.submission_id !== submissionId ||
      result.ref?.session_id !== ref.session_id || result.ref.profile_id !== ref.profile_id ||
      typeof result.admission_id !== 'string' || !result.admission_id.trim() || result.admission_id === submissionId ||
      !['queued', 'started', 'terminal', 'unknown'].includes(result.status)) {
    throw new Error('UNKNOWN · Ответ владельца журнала не подтверждён. Повторная отправка удержана.')
  }
  return { ref, submission_id: submissionId, admission_id: result.admission_id, status: result.status }
}
