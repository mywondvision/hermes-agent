import { spawn } from 'node:child_process'
import { createHash, generateKeyPairSync, randomBytes } from 'node:crypto'
import { createInterface } from 'node:readline'
import { buildGatewayWsUrl } from '../../electron/connection-config'
import { PlanRouteRegistry } from '../../electron/symphony-route-registry'
import { planSocket } from '../../electron/symphony-route-socket'
import type { HostEvidence, PlanRegistration } from '../../electron/symphony-route'
import { planAnswerFrame, type PlanAnswerBinding, type PlanAnswerFrame } from './symphony-task-diagnostics'

type Callback = (message: any) => void

interface OperatorTrace {
  stage: 'sent' | 'reply' | 'dropped' | 'held-before' | 'lost-before' | 'refused'
  action: string
  submission_id: string
  admission_id?: string
  command_id?: string
  previous_command_id?: string
  attempt_id?: string
  phase?: string
  observed_at?: number
  evidence?: string
  state?: string
  dispatched?: boolean
  local_stopped?: boolean
  error?: string
}

interface PeerExit {
  code: number | null
  signal: NodeJS.Signals | null
}

export class RoutePeer {
  private readonly pending = new Map<number, Callback>()
  private readonly listeners = new Map<string, Callback>()
  private readonly sockets = new Map<string, () => void>()
  private sequence = 0
  private dials = 0
  private priorEvidence?: HostEvidence
  private stderr = ''
  readonly registry = new PlanRouteRegistry()
  readonly transports: string[] = []
  readonly reads: string[] = []
  readonly operatorTrace: OperatorTrace[] = []
  readonly lostOperatorAcks = new Set<string>()
  private stopBeforeDelivery?: 'hold' | 'lose'
  private delayedStop?: () => Promise<void>
  private operatorToken?: string
  readonly planAnswerReplies: PlanAnswerFrame[] = []
  onAnswerFrame?: (stage: string, facts: Record<string, unknown>) => void
  get openPlanSockets() { return this.sockets.size }
  observations = 0
  readonly clientKeys = generateKeyPairSync('ed25519')
  readonly hostBlob = Buffer.concat([Buffer.from('0000000b7373682d6564323535313900000020', 'hex'), randomBytes(32)])
  holding: '' | 'before' | 'after' = ''
  evidence: 'fresh' | 'cached' | 'reboot' | 'missing' = 'fresh'
  registration!: PlanRegistration
  private readonly child
  private readonly taskClosed?: Promise<PeerExit>
  private taskExit?: PeerExit
  get operatorExitStatus() { return this.taskExit }

  interceptNextStop(mode: 'hold' | 'lose') {
    if (this.module !== 'symphony_operator_peer' || this.stopBeforeDelivery || this.delayedStop) {
      throw new Error('A single personal STOP interception is allowed at a time')
    }
    this.stopBeforeDelivery = mode
  }

  releaseInterceptedStop() {
    const send = this.delayedStop
    if (!send) throw new Error('No intercepted STOP is available')
    this.delayedStop = undefined
    return send()
  }

  constructor(python: string, checkout: string, temp: string, private readonly module: 'symphony_plan_peer' | 'symphony_task_peer' | 'symphony_forward_peer' | 'symphony_operator_peer' = 'symphony_plan_peer') {
    this.child = spawn(python, ['-B', '-u', '-m', 'tests.plugins.' + module], {
      cwd: checkout,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONUTF8: '1',
        PYTHONDONTWRITEBYTECODE: '1',
        TMP: temp,
        TEMP: temp,
        TMPDIR: temp,
        HERMES_HOME: temp,
        HERMES_RUNTIME_DIR: temp,
        UV_OFFLINE: '1',
        PYTEST_DISABLE_PLUGIN_AUTOLOAD: '1'
      }
    })
    this.child.stderr.on('data', chunk => {
      this.stderr = (this.stderr + chunk.toString()).slice(-3500)
    })
    this.child.on('error', error => this.failed(error.message))
    if (module === 'symphony_operator_peer') {
      this.child.stdin.on('error', error => this.failed(error.message))
      // The stdio ACK precedes Python cleanup. `close` follows both exit and the
      // last data on stdout, so it cannot race the delivery of that ACK.
      this.taskClosed = new Promise(resolve => this.child.once('close', (code, signal) => {
        const outcome = { code, signal }
        this.taskExit = outcome
        resolve(outcome)
        this.failed('peer exited ' + code + ': ' + this.stderr)
      }))
    } else {
      this.child.on('exit', code => this.failed('peer exited ' + code + ': ' + this.stderr))
    }
    createInterface({ input: this.child.stdout }).on('line', line => {
      const value = JSON.parse(line)
      if (value.frame) this.listeners.get(value.socket)?.(value.frame)
      else this.pending.get(value.id)?.(value)
    })
  }

  private failed(message: string) {
    for (const cb of this.pending.values()) cb({ error: message })
    for (const close of [...this.sockets.values()]) close()
  }

  control(op: string, args: object = {}): Promise<any> {
    if (!this.child.stdin.writable || this.child.exitCode !== null)
      return Promise.reject(new Error('Peer unavailable: ' + this.stderr))
    const id = ++this.sequence
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error('Peer timeout: ' + op))
      }, 45000)
      this.pending.set(id, msg => {
        clearTimeout(timer)
        this.pending.delete(id)
        if (msg.error) reject(new Error(msg.error))
        else resolve(msg.result)
      })
      this.child.stdin.write(JSON.stringify({ id, op, ...args }) + '\n')
    })
  }

  async start(ordinaryToken?: string, operatorKey?: string) {
    const publicKey = this.clientKeys.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32).toString('base64')
    const hostFingerprint = 'SHA256:' + createHash('sha256').update(this.hostBlob).digest('base64').replace(/=+$/, '')
    if (this.module === 'symphony_operator_peer' && (!ordinaryToken || !operatorKey)) throw new Error('Missing OPERATOR enrollment inputs')
    const manifest = await this.control('init', { public_key: publicKey, host_fingerprint: hostFingerprint,
      ...(ordinaryToken && { ssh_token: ordinaryToken }), ...(operatorKey && { operator_public_key: operatorKey }) })
    if (this.module === 'symphony_operator_peer') {
      if (manifest.forward_mode !== 'personal' || manifest.profile_id !== manifest.profile)
        throw new Error('OPERATOR enrollment or canonical profile not confirmed')
      this.operatorToken = ordinaryToken
    }
    this.install(manifest)
    return manifest
  }

  private install(manifest: any) {
    const reg: PlanRegistration = {
      kind: 'ssh',
      connectionId: manifest.connection_id,
      profile: 'work',
      conversation: manifest.conversation_id,
      storedConversation: ['symphony_forward_peer', 'symphony_operator_peer'].includes(this.module)
        ? manifest.conversation_id : 'stored-' + manifest.conversation_id,
      installId: manifest.install_id,
      leaseId: 'fixture-lease',
      bootId: manifest.boot_id,
      hostKey: 'ssh-ed25519 ' + this.hostBlob.toString('base64'),
      canonicalProfile: manifest.profile,
      namespace: manifest.namespace,
      operator: manifest.operator_id,
      session: manifest.session_id,
      generation: manifest.generation,
      clientId: manifest.client_id,
      signingKey: this.clientKeys.privateKey
    }
    this.registration = reg
    this.registry.install(reg, {
      observe: async (_address, attempt) => {
        this.observations += 1
        if (this.evidence === 'missing') throw new Error('Missing K1 fixture')
        if (this.evidence === 'cached' && this.priorEvidence) return this.priorEvidence
        const read = {
          attempt,
          connectionId: reg.connectionId,
          installId: reg.installId,
          leaseId: reg.leaseId,
          bootId: this.evidence === 'reboot' ? 'other-boot' : reg.bootId,
          hostKey: reg.hostKey
        }
        this.priorEvidence = read
        return read
      },
      attach: async () => {
        const name = 'window-' + ++this.dials
        let closeOperator: (() => void) | undefined
        let didOpen = false
        let finish: (() => void) | undefined
        const close = () => {
          if (closeOperator) return closeOperator()
          if (!didOpen) return
          didOpen = false
          this.sockets.delete(name)
          this.listeners.delete(name)
          finish?.()
          if (!this.child.killed && this.child.exitCode === null)
            void this.control('close', { socket: name }).catch(() => {})
        }
        return {
          peerHostKey: reg.hostKey,
          close,
          open: async (path, protocol, event, closed) => {
            if (path === '/api/ws' && protocol === '' && this.operatorToken) {
              const socket = await this.openOperator(name, this.operatorToken, event, closed)
              closeOperator = socket.close
              return socket
            }
            if (path !== '/api/ws' || protocol !== 'hermes.symphony-plan.v1') throw Error('Wrong plan route')
            await this.control('open', { socket: name })
            didOpen = true
            this.sockets.set(name, close)
            const answers = new Map<number, PlanAnswerBinding>()
            return planSocket(
              {
                protocol,
                close,
                listen: (read, end) => {
                  finish = end
                  this.listeners.set(name, frame => {
                    if (frame.result?.state === 'READY') this.transports.push(frame.result.transport_id)
                    const binding = answers.get(frame.id)
                    if (binding) {
                      answers.delete(frame.id)
                      const answer = planAnswerFrame(binding, frame.result?.output)
                      this.planAnswerReplies.push(answer)
                      this.onAnswerFrame?.('plan-answer response at transport', { ...answer, socket: name,
                        rpc_id: frame.id, error_code: frame.error?.code ?? null })
                    }
                    if (binding && this.holding === 'after') {
                      this.holding = ''
                      close()
                      return
                    }
                    read(JSON.stringify(frame))
                    if (binding) {
                      this.onAnswerFrame?.('plan-answer response delivered to Desktop channel', {
                        ...binding, socket: name, rpc_id: frame.id
                      })
                    }
                  })
                  return () => {
                    this.listeners.delete(name)
                  }
                },
                send: raw => {
                  const frame = JSON.parse(raw)
                  if (frame.method === 'command.dispatch') {
                    const verb = frame.params.arg.split(' ')[0]
                    this.reads.push(verb)
                    if (verb === 'plan-answer') {
                      if (this.holding === 'before') {
                        this.holding = ''
                        close()
                        return
                      }
                      const { request_id, plan_digest, input_id } = JSON.parse(frame.params.arg.slice(verb.length + 1))
                      const binding = { request_id, plan_digest, input_id }
                      answers.set(frame.id, binding)
                      this.onAnswerFrame?.('plan-answer passed to transport', { ...binding, socket: name, rpc_id: frame.id })
                    }
                  }
                  void this.control('frame', { socket: name, frame }).catch(close)
                }
              },
              event,
              closed
            )
          }
        }
      }
    })
  }

  private async openOperator(name: string, token: string, event: (frame: any) => void, closed: () => void) {
    type Action = Pick<OperatorTrace, 'action' | 'submission_id'>
    const outstanding = new Map<number, Action>()
    const challenges = new Map<string, Action>()
    const executing = new Map<number, OperatorTrace>()
    let notifyClose: (() => void) | undefined
    let read: ((value: string) => void) | undefined
    let open = true
    const close = () => {
      if (!open) return
      open = false
      this.sockets.delete(name)
      notifyClose?.()
      void transport.close().catch(() => undefined)
    }
    const transport = await this.openOrdinary(name, buildGatewayWsUrl('http://127.0.0.1', token), [], raw => {
      if (!open) return
      const { id, result, error } = JSON.parse(raw)
      const challenge = outstanding.get(id)
      outstanding.delete(id)
      if (challenge && typeof result?.challenge_id === 'string') challenges.set(result.challenge_id, challenge)
      const execution = executing.get(id)
      executing.delete(id)
      if (execution && error) {
        const message = ['OPERATOR_UNKNOWN', 'OPERATOR_HOLD', 'admission_conflict'].includes(error.message) ? error.message : 'OTHER'
        this.operatorTrace.push({ ...execution, stage: 'refused', error: message })
      }
      if (execution && result) {
        const drop = this.lostOperatorAcks.delete(execution.action)
        const { admission_id, attempt_id, command_id, previous_command_id, phase, observed_at, evidence, state, dispatched, local_stopped } = result
        this.operatorTrace.push({ ...execution, stage: drop ? 'dropped' : 'reply',
          admission_id, attempt_id, command_id, previous_command_id, phase, observed_at, evidence, state, dispatched, local_stopped })
        if (drop) return close()
      }
      read?.(raw)
    })
    this.sockets.set(name, close)
    return planSocket({
      protocol: '', close,
      listen(callback, end) {
        read = callback
        notifyClose = end
        return () => { read = undefined }
      },
      send: raw => {
        const { method, params, id } = JSON.parse(raw)
        if (method === 'operator.challenge') outstanding.set(id, { action: params.action, submission_id: params.submission_id })
        if (method === 'operator.execute') {
          const metadata = challenges.get(params.challenge_id)
          challenges.delete(params.challenge_id)
          if (!metadata) return close()
          const { command_id, previous_command_id, attempt_id } = params.payload
          const entry: OperatorTrace = { ...metadata, stage: 'sent', command_id, previous_command_id, attempt_id }
          const send = () => {
            if (!open) throw new Error('Intercepted STOP socket is closed')
            this.operatorTrace.push(entry)
            executing.set(id, entry)
            return transport.send(raw)
          }
          if (metadata.action === 'stop' && this.stopBeforeDelivery) {
            const mode = this.stopBeforeDelivery
            this.stopBeforeDelivery = undefined
            this.delayedStop = send
            this.operatorTrace.push({ ...entry, stage: mode === 'hold' ? 'held-before' : 'lost-before' })
            if (mode === 'lose') {
              read?.(JSON.stringify({ jsonrpc: '2.0', id, error: { code: 4090, message: 'OPERATOR_UNKNOWN' } }))
            }
            return
          }
          void send().catch(close)
          return
        }
        void transport.send(raw).catch(close)
      }
    }, event, closed)
  }

  async openOrdinary(socket: string, url: string, protocols: string[], receive: (raw: string) => void) {
    const address = new URL(url)
    this.listeners.set(socket, frame => receive(JSON.stringify(frame)))
    const upgrade = await this.control('open', { socket, channel: 'ordinary', path: address.pathname, query: address.search.slice(1), subprotocols: protocols })
    if (upgrade?.transport !== 'standalone' || upgrade.credential !== 'session-token' || upgrade.authority_bound !== false) {
      throw new Error('Ordinary SSH standalone upgrade is unverified')
    }
    return {
      upgrade: { transport: upgrade.transport, credential: upgrade.credential, authority_bound: upgrade.authority_bound },
      send: async (raw: string) => {
        await this.control('frame', { socket, frame: JSON.parse(raw) })
      },
      close: async () => {
        this.listeners.delete(socket)
        await this.control('close', { socket })
      }
    }
  }

  disconnect() {
    for (const close of [...this.sockets.values()]) close()
  }

  async restart() {
    this.disconnect()
    this.install(await this.control('restart'))
  }

  async shutdown() {
    this.stopBeforeDelivery = undefined
    this.delayedStop = undefined
    for (const close of [...this.sockets.values()]) close()
    try {
      await this.control('shutdown')
    } finally {
      this.child.stdin.end()
      if (this.taskClosed) {
        let timer: ReturnType<typeof setTimeout> | undefined
        try {
          const exit = await Promise.race([this.taskClosed, new Promise<never>((_resolve, reject) => {
            timer = setTimeout(() => reject(new Error('TASK peer shutdown outcome is unknown')), 45_000)
          })])
          if (exit.code !== 0 || exit.signal !== null) throw new Error('TASK peer ended during cleanup with code ' + exit.code)
        } finally {
          clearTimeout(timer)
        }
      }
    }
  }
}
