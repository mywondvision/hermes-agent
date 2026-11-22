import { createPrivateKey, generateKeyPairSync, randomUUID, type KeyObject } from 'node:crypto'
import { mkdirSync, readFileSync } from 'node:fs'
import path from 'node:path'

import { writeSecretFileAtomic } from './hardening'
import type { OperatorPatchCommand } from './symphony-operator-types'
import type { PlanAddress } from './symphony-route'

export interface OperatorAddress extends PlanAddress {
  readonly storedConversation: string
}

interface ProtectedStorage {
  isEncryptionAvailable(): boolean
  encryptString(value: string): Buffer
  decryptString(value: Buffer): string
}

export interface OperatorStopTarget {
  readonly id: string
  readonly attemptId: string
}

interface OperatorStop extends OperatorStopTarget {
  readonly previous?: OperatorStopTarget
}

export interface OperatorBinding {
  readonly address: OperatorAddress
  readonly submissionId: string
  readonly windowKey: string
  readonly generation: number
  readonly recovering?: true
  readonly admissionId?: string
  readonly attemptId?: string
  readonly job?: string
  readonly stop?: OperatorStop
  readonly patchCommands?: readonly OperatorPatchCommand[]
}

export interface OperatorInstallation {
  readonly id: string
  readonly key: KeyObject
}

interface Journal {
  readonly version: 1
  readonly id: string
  readonly privateKey: string
  readonly bindings: readonly OperatorBinding[]
}

function newKey() {
  return generateKeyPairSync('ed25519').privateKey.export({ type: 'pkcs8', format: 'pem' }).toString()
}

export function operatorPrivateKey(pem: string): KeyObject {
  const key = createPrivateKey(pem)
  if (key.asymmetricKeyType !== 'ed25519') throw new Error('Invalid Symphony operator key')
  return key
}

export function sameOperatorAddress(a: OperatorAddress, b: OperatorAddress) {
  return a.connectionId === b.connectionId && a.profile === b.profile &&
    a.conversation === b.conversation && a.storedConversation === b.storedConversation
}

/** OS-encrypted, main-only proof and delivery journal. A corrupt or unavailable
 * store cannot establish a fresh identity or authorize retransmission. */
export class SymphonyOperatorJournal {
  private readonly file: string

  constructor(userData: string, private readonly storage: ProtectedStorage) {
    this.file = path.join(userData, 'symphony-operator-v1.bin')
  }

  private secure() {
    if (!this.storage.isEncryptionAvailable()) throw new Error('Protected Symphony operator storage is unavailable')
  }

  private read(): Journal | undefined {
    this.secure()
    let raw: Buffer
    try {
      raw = readFileSync(this.file)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined
      throw error
    }
    const value: Journal = JSON.parse(this.storage.decryptString(raw))
    if (value?.version !== 1 || typeof value.id !== 'string' || !value.id ||
        typeof value.privateKey !== 'string' || !Array.isArray(value.bindings)) {
      throw new Error('Invalid Symphony operator journal')
    }
    return value
  }

  private write(value: Journal) {
    const encrypted = this.storage.encryptString(JSON.stringify(value))
    mkdirSync(path.dirname(this.file), { recursive: true })
    writeSecretFileAtomic(this.file, encrypted)
  }

  installation(): OperatorInstallation {
    const prior = this.read()
    if (prior) return { id: prior.id, key: operatorPrivateKey(prior.privateKey) }
    const journal: Journal = { version: 1, id: randomUUID(), privateKey: newKey(), bindings: [] }
    this.write(journal)
    return { id: journal.id, key: operatorPrivateKey(journal.privateKey) }
  }

  existingInstallation(): OperatorInstallation | undefined {
    const prior = this.read()
    if (prior) return { id: prior.id, key: operatorPrivateKey(prior.privateKey) }
  }

  find(address: OperatorAddress, submissionId: string): OperatorBinding | undefined {
    return this.read()?.bindings.find(row => row.submissionId === submissionId && sameOperatorAddress(row.address, address))
  }

  bindings(): readonly OperatorBinding[] {
    return this.read()?.bindings ?? []
  }

  begin(address: OperatorAddress, submissionId: string): OperatorBinding {
    const journal = this.read()
    if (!journal) throw new Error('Symphony operator installation is unavailable')
    if (journal.bindings.some(row => row.submissionId === submissionId && sameOperatorAddress(row.address, address))) {
      throw new Error('Symphony submission already has a binding')
    }
    const row: OperatorBinding = { address, submissionId, windowKey: newKey(), generation: 0 }
    this.write({ ...journal, bindings: [...journal.bindings, row] })
    return row
  }

  update(row: OperatorBinding, changes: Partial<Pick<OperatorBinding,
    'admissionId' | 'attemptId' | 'job' | 'stop' | 'patchCommands' | 'generation' | 'recovering'>>): OperatorBinding {
    const journal = this.read()
    const current = journal?.bindings.find(item => item.submissionId === row.submissionId && sameOperatorAddress(item.address, row.address))
    if (!journal || !current || current.windowKey !== row.windowKey || current.generation !== row.generation ||
        current.recovering !== row.recovering || current.stop?.id !== row.stop?.id ||
        current.stop?.attemptId !== row.stop?.attemptId || current.stop?.previous?.id !== row.stop?.previous?.id ||
        current.stop?.previous?.attemptId !== row.stop?.previous?.attemptId ||
        current.admissionId !== row.admissionId || current.attemptId !== row.attemptId || current.job !== row.job ||
        JSON.stringify(current.patchCommands) !== JSON.stringify(row.patchCommands)) {
      throw new Error('Symphony operator binding changed')
    }
    const next = { ...current, ...changes }
    this.write({ ...journal, bindings: journal.bindings.map(item => item === current ? next : item) })
    return next
  }
}
