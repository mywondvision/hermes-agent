export type OperatorCall =
  | { action: 'submit'; submissionId: string; text: string }
  | { action: 'receipt'; submissionId?: string }
  | { action: 'status' | 'stop'; attemptId?: string }
  | { action: 'stop-reconcile' | 'stop-replace'; attemptId: string }
  | { action: 'patch-decision'; text: string }

export interface OperatorCheckedPatch {
  attemptId: string
  job: string
  captureSeq: number
  checkSeq: number
  patchSha256: string
}

export interface OperatorPatchCommand extends OperatorCheckedPatch {
  id: string
  action: 'apply' | 'archive'
}

export interface OperatorAdmission {
  admission_id: string
  submission_id: string
  status: 'queued' | 'started' | 'terminal' | 'unknown'
}

export type OperatorReply =
  | { state: 'HOLD' | 'UNKNOWN' }
  | { state: 'ADMITTED'; receipt: OperatorAdmission }
  | { state: 'STATUS'; attemptId: string; phase: string; observedAt: number; evidence?: string }
  | { state: 'STOP'; attemptId: string; commandId: string; observation: string; previousCommandId?: string; dispatched?: boolean; localStopped?: boolean }
  | { state: 'PATCH'; command: OperatorPatchCommand; observation: 'APPLIED' | 'ARCHIVED' | 'CHECK_FAILED' | 'UNKNOWN'; commit?: string }
