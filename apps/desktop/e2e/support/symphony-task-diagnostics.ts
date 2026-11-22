export interface PlanAnswerBinding {
  request_id: string
  plan_digest: string
  input_id: string
}

export interface PlanAnswerFrame extends PlanAnswerBinding {
  state: string
  launch: string | null
  job_field: 'absent' | 'null' | 'empty' | 'id' | 'invalid'
  binding_matches: boolean
}

const decisions = ['DENIED', 'APPROVED', 'RETIRED', 'UNKNOWN']
const launchOutcomes = ['SUBMITTED', 'DUPLICATE', 'CONFLICT', 'REJECTED', 'UNKNOWN']
const handoffUncertainties = ['WORKSPACE_HELD', 'SUPERVISOR_UNAVAILABLE', 'LAUNCH_OUTCOME_UNKNOWN', 'CONFLICT']
const workerStates = ['idle', 'armed', 'held', 'releasing']
const observedValue = (value: unknown, allowed: string[]) =>
  typeof value === 'string' && allowed.includes(value) ? value : null
const code = (value: unknown) => typeof value === 'string' && /^[A-Z][A-Z0-9_]{0,95}$/.test(value) ? value : null
const id = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,120}$/.test(value) ? value : null
const digest = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value) ? value : null

function jobField(reply: { job?: unknown } | null): PlanAnswerFrame['job_field'] {
  if (reply == null || !Object.prototype.hasOwnProperty.call(reply, 'job')) return 'absent'
  if (reply.job === null) return 'null'
  if (reply.job === '') return 'empty'
  return id(reply.job) ? 'id' : 'invalid'
}

export function planAnswerFrame(binding: PlanAnswerBinding, output: string | undefined): PlanAnswerFrame {
  let reply
  try {
    reply = JSON.parse(output ?? 'null')
  } catch {
    reply = null
  }
  return {
    ...binding,
    state: observedValue(reply?.decision ?? reply?.state, decisions) ?? 'UNRECOGNIZED',
    launch: observedValue(reply?.launch, launchOutcomes),
    job_field: jobField(reply),
    binding_matches: reply?.request_id === binding.request_id && reply?.plan_digest === binding.plan_digest &&
      reply?.input_id === binding.input_id
  }
}

export function taskLaunchAccepted(answer: PlanAnswerFrame | undefined) {
  return answer?.binding_matches === true && answer.state === 'APPROVED' && answer.launch === 'SUBMITTED'
}

interface TaskCounters {
  permits: number
  reservations: number
  launches: number
}

interface WorkerOutcome {
  attempt_id: string
  state: string
  error_type?: string
  error_reason?: string
}

export interface TaskDiagnosticCounts extends TaskCounters {
  plan_inputs: { admission_id: string; request_id: string; plan_digest: string }[]
  by_request: Record<string, { decision: string; plan_digest: string }>
  worker_diagnostics?: {
    state: string
    finished_event: boolean
    outcomes: WorkerOutcome[]
    launches: { request_id: string; attempt_id: string; state: string; hold_reason?: string | null }[]
    plan_answers: {
      request_id: string; decision: string; launch?: string; job?: string; error_type?: string; error_reason?: string
    }[]
    launch_handoffs?: { request_id: string; grant_outcome: string; port_outcome: string | null; uncertainty: string[] }[]
  }
}

const errorCodes = (row: { error_type?: string; error_reason?: string }) => ({
  error_type: id(row.error_type),
  error_reason: code(row.error_reason)
})

const workerOutcome = (row: WorkerOutcome) => ({
  attempt_id: id(row.attempt_id),
  state: observedValue(row.state, ['running', 'finished', 'failed']),
  ...errorCodes(row)
})

export function taskWorkerSettlement(counts: TaskDiagnosticCounts | undefined, attemptId: string, requestId: string) {
  const diagnostic = counts?.worker_diagnostics
  const outcome = diagnostic?.outcomes.find(row => row.attempt_id === attemptId)
  const launch = diagnostic?.launches.find(row => row.attempt_id === attemptId && row.request_id === requestId)
  return {
    worker_outcome: outcome ? workerOutcome(outcome) : null,
    launch: launch ? {
      state: code(launch.state),
      hold_reason: launch.hold_reason === null ? null : code(launch.hold_reason) ?? 'UNRECOGNIZED'
    } : null,
    settled: outcome?.state === 'finished' && launch?.hold_reason === null
  }
}

export async function readTaskFailure(
  admissionId: string,
  selectedPlan: string | undefined,
  answer: PlanAnswerFrame | undefined,
  before: TaskCounters,
  readCounts: () => Promise<TaskDiagnosticCounts>,
  readWorker: () => Promise<{ state: string }>
) {
  const requestId = 'task-' + admissionId
  const [countResult, workerResult] = await Promise.allSettled([readCounts(), readWorker()])
  const counts = countResult.status === 'fulfilled' ? countResult.value : undefined
  const diagnostic = counts?.worker_diagnostics
  const launchRows = diagnostic?.launches.filter(row => row.request_id === requestId) ?? []
  const handoff = diagnostic?.launch_handoffs?.find(row => row.request_id === requestId)
  const decision = counts?.by_request[requestId]
  const plan = counts?.plan_inputs.find(row => row.admission_id === admissionId && row.request_id === requestId)
  return {
    admission_id: admissionId,
    request_id: requestId,
    plan_digest: digest(selectedPlan ?? plan?.plan_digest),
    first_answer: answer ?? null,
    counts_read: countResult.status,
    worker_read: workerResult.status,
    worker: workerResult.status === 'fulfilled' ? observedValue(workerResult.value.state, workerStates) : null,
    launch_handoff: handoff ? {
      grant_outcome: observedValue(handoff.grant_outcome, ['ISSUED', 'MISSING']),
      port_outcome: observedValue(handoff.port_outcome, launchOutcomes),
      uncertainty: handoff.uncertainty.map(value => observedValue(value, handoffUncertainties) ?? 'OTHER')
    } : null,
    persisted_decision: decision ? { decision: observedValue(decision.decision, decisions), plan_digest: digest(decision.plan_digest) } : null,
    deltas: counts ? { permits: counts.permits - before.permits, reservations: counts.reservations - before.reservations,
      launches: counts.launches - before.launches } : null,
    owner_worker: diagnostic ? {
      state: observedValue(diagnostic.state, workerStates),
      finished_event: diagnostic.finished_event,
      launches: launchRows.map(row => ({ attempt_id: id(row.attempt_id), state: code(row.state), hold_reason: code(row.hold_reason) })),
      outcomes: diagnostic.outcomes.filter(row => launchRows.some(launch => launch.attempt_id === row.attempt_id))
        .map(workerOutcome),
      plan_answers: diagnostic.plan_answers.filter(row => row.request_id === requestId)
        .map(row => ({ decision: observedValue(row.decision, decisions), launch: observedValue(row.launch, launchOutcomes),
          job: id(row.job), ...errorCodes(row) }))
    } : null
  }
}
