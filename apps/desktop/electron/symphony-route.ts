import { createHash, randomUUID, sign, type KeyObject } from 'node:crypto'
import { PlanRouteRefused } from './symphony-route-socket'

export interface PlanAddress {
  connectionId: string
  profile: string
  conversation: string
}

export const planOperations = ['status', 'plan-presented', 'plan-answer', 'plan-receipt', 'plan-result'] as const
export type PlanOperation = (typeof planOperations)[number]
export const routeProtocol = 'hermes.symphony-plan.v1'

export interface PlanRegistration extends PlanAddress {
  readonly kind: 'ssh'
  readonly storedConversation: string
  readonly installId: string
  readonly leaseId: string
  readonly bootId: string
  readonly hostKey: string
  readonly canonicalProfile: string
  readonly namespace: string
  readonly operator: string
  readonly session: string
  readonly generation: string
  readonly clientId: string
  readonly signingKey: KeyObject
}

export interface HostEvidence {
  attempt: string
  connectionId: string
  installId: string
  leaseId: string
  bootId: string
  hostKey: string
}

export interface PlanSocket {
  readonly protocol: string
  readonly open: boolean
  request(method: string, params: object): Promise<any>
  close(): void
}

export interface PlanEnvironment {
  // Immutable registration object; replacing/removing it invalidates its sockets.
  registration(address: PlanAddress): Readonly<PlanRegistration> | undefined
  observe(address: PlanAddress, attempt: string): Promise<HostEvidence>
  // Attach to the selected existing gateway only. Transport must enforce the full pinned key.
  attach(
    reg: Readonly<PlanRegistration>,
    evidence: HostEvidence
  ): Promise<{
    peerHostKey: string
    open(path: '/api/ws', protocol: string, receive: (frame: any) => void, closed: () => void): Promise<PlanSocket>
    close(): void
  }>
}

const unavailable = () => new Error('Symphony plan route unavailable')
export class PlanNotSent extends Error {}

function hostFingerprint(hostKey: string) {
  const match = /^([a-zA-Z0-9@._+-]+) ([A-Za-z0-9+/]+=*)$/.exec(hostKey)
  if (!match) throw unavailable()
  const [, algorithm, encoded] = match
  const bytes = Buffer.from(encoded, 'base64')
  if (
    bytes.length <= 4 + algorithm.length ||
    bytes.readUInt32BE() !== algorithm.length ||
    !bytes.subarray(4, 4 + algorithm.length).equals(Buffer.from(algorithm, 'ascii')) ||
    bytes.toString('base64') !== encoded
  )
    throw unavailable()
  return 'SHA256:' + createHash('sha256').update(bytes).digest('base64').replace(/=+$/, '')
}

export async function attachPinnedSymphonyHost(
  env: PlanEnvironment,
  lifetime: AbortSignal,
  address: PlanAddress
) {
  const reg = env.registration(address)
  if (
    !reg ||
    reg.kind !== 'ssh' ||
    ![
      reg.connectionId,
      reg.profile,
      reg.conversation,
      reg.storedConversation,
      reg.installId,
      reg.leaseId,
      reg.bootId,
      reg.canonicalProfile,
      reg.namespace,
      reg.operator,
      reg.session,
      reg.generation,
      reg.clientId
    ].every(value => typeof value === 'string' && value.trim().length > 0) ||
    reg.connectionId !== address.connectionId ||
    reg.profile !== address.profile ||
    reg.conversation !== address.conversation
  )
    throw unavailable()
  const fingerprint = hostFingerprint(reg.hostKey)
  const attempt = randomUUID()
  const evidence = await env.observe(address, attempt)
  if (
    evidence.attempt !== attempt ||
    evidence.connectionId !== reg.connectionId ||
    evidence.installId !== reg.installId ||
    evidence.leaseId !== reg.leaseId ||
    evidence.bootId !== reg.bootId ||
    evidence.hostKey !== reg.hostKey ||
    lifetime.aborted ||
    env.registration(address) !== reg
  )
    throw unavailable()

  const tunnel = await env.attach(reg, evidence)
  if (lifetime.aborted || tunnel.peerHostKey !== reg.hostKey || env.registration(address) !== reg) {
    tunnel.close()
    throw unavailable()
  }
  return { reg, fingerprint, tunnel }
}

export async function openPlanSocket(
  env: PlanEnvironment,
  lifetime: AbortSignal,
  address: PlanAddress,
  wake: () => void,
  closed: () => void
) {
  const { reg, fingerprint, tunnel } = await attachPinnedSymphonyHost(env, lifetime, address)
  let ready = false
  const stop = () => tunnel.close()
  lifetime.addEventListener('abort', stop, { once: true })
  const socket = await tunnel
    .open(
      '/api/ws',
      routeProtocol,
      frame => {
        if (
          ready &&
          env.registration(address) === reg &&
          frame?.method === 'event' &&
          frame.params?.type === 'symphony.plan' &&
          frame.params?.session_id === reg.conversation
        )
          wake()
      },
      () => {
        const wasReady = ready
        ready = false
        lifetime.removeEventListener('abort', stop)
        if (wasReady && !lifetime.aborted) closed()
        tunnel.close()
      }
    )
    .catch(error => {
      lifetime.removeEventListener('abort', stop)
      tunnel.close()
      throw error
    })
  const valid = () => !lifetime.aborted && socket.open && env.registration(address) === reg
  try {
    if (!valid() || socket.protocol !== routeProtocol) throw unavailable()
    const started = performance.now()
    const { fields } = await socket.request('route.challenge', {
      client_id: reg.clientId,
      connection_id: reg.connectionId
    })
    if (
      !Array.isArray(fields) ||
      fields.length !== 14 ||
      !fields.every(x => typeof x === 'string' && x.length > 0 && x.length <= 4096)
    )
      throw unavailable()
    const expected = [
      'symphony-plan',
      '1',
      fields[2],
      reg.installId,
      fingerprint,
      reg.canonicalProfile,
      reg.namespace,
      reg.operator,
      reg.conversation,
      reg.session,
      fields[10],
      reg.generation,
      reg.connectionId,
      reg.clientId
    ]
    if (
      !valid() ||
      fields.some((v, i) => v !== expected[i]) ||
      performance.now() - started >= 60_000 ||
      reg.signingKey.type !== 'private' ||
      reg.signingKey.asymmetricKeyType !== 'ed25519'
    )
      throw unavailable()
    const signature = sign(null, planSigningBytes(expected), reg.signingKey).toString('base64')
    const auth = await socket.request('route.authenticate', { signature })
    if (
      !valid() ||
      auth?.state !== 'READY' ||
      auth.transport_id !== fields[10] ||
      auth.session_id !== reg.session ||
      auth.conversation_id !== reg.conversation
    )
      throw unavailable()
    ready = true
  } catch (error) {
    socket.close()
    throw error
  }

  return {
    close: () => {
      ready = false
      socket.close()
    },
    live: () => ready && valid(),
    async command(verb: PlanOperation, data?: object) {
      if (!ready || !valid() || !planOperations.includes(verb)) throw new PlanNotSent('Symphony plan route unavailable')
      const response = await socket
        .request('command.dispatch', {
          session_id: reg.conversation,
          name: 'symphony',
          interaction: 'displayed-plan',
          arg: data ? verb + ' ' + JSON.stringify(data) : verb
        })
        .catch(error => {
          if (error instanceof PlanRouteRefused) {
            ready = false
            socket.close()
          }
          throw error
        })
      if (!ready || !valid() || typeof response?.output !== 'string') throw unavailable()
      return { output: response.output as string }
    }
  }
}

export function planSigningBytes(fields: readonly string[]) {
  return Buffer.from(JSON.stringify(fields), 'utf8')
}
