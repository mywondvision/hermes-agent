import { LOCAL_CONNECTION_ID } from '@hermes/shared'
import { hasRegistryTopology, registryConnectionKind } from './connection-registry-state'
import { $connection } from './session'
import { knownOwnerForSession } from './session-states'

export function sshTaskTransport(targetId: string | null | undefined, nativeLocal: boolean | undefined) {
  const current = $connection.get()
  const live = targetId === current?.connectionId ? current : null
  const registered = targetId ? registryConnectionKind(targetId) : undefined
  const liveKind = live?.remoteKind === 'url' ? 'remote' : live?.remoteKind
  if (registered && liveKind && registered !== liveKind) return 'unresolved'
  const kind = registered ?? liveKind
  if (kind === 'ssh') return 'ssh'
  if (!kind && (live?.mode === 'remote' || (targetId && targetId !== LOCAL_CONNECTION_ID)
      || (!nativeLocal && !targetId && !live?.mode && hasRegistryTopology()))) return 'unresolved'
  return undefined
}

export function needsSshProbe(sessionId: string | null | undefined) {
  const owner = knownOwnerForSession(sessionId)
  const connection = $connection.get()
  const target = owner && typeof owner === 'object' ? owner.connectionId : connection?.connectionId
  return Boolean(sshTaskTransport(target, target === LOCAL_CONNECTION_ID || connection?.authMode === 'native'))
}
