import type { OperatorAddress } from './symphony-operator-journal'
import { attachPinnedSymphonyHost, type PlanEnvironment } from './symphony-route'
import { OperatorHold, sameOperatorRef, type OperatorChannel } from './symphony-operator-wire'

/** The host transport supplies the ordinary token upgrade through its pinned
 * tunnel. It never takes a renderer URL or reuses the plan signing key. */
export async function openOperatorChannel(environment: PlanEnvironment, address: OperatorAddress): Promise<OperatorChannel> {
  const lifetime = new AbortController()
  const { reg, tunnel } = await attachPinnedSymphonyHost(environment, lifetime.signal, address)
  const close = () => {
    if (lifetime.signal.aborted) return
    lifetime.abort()
    tunnel.close()
  }
  try {
    if (reg.storedConversation !== address.storedConversation) throw new OperatorHold()
    const socket = await tunnel.open('/api/ws', '', () => undefined, close)
    const open = () => socket.open && !lifetime.signal.aborted && environment.registration(address) === reg
    if (!open() || socket.protocol !== '') throw new OperatorHold()
    const session = await socket.request('session.resume', { session_id: address.conversation, lazy: true })
    if (!open() || typeof session?.session_id !== 'string' || !session.session_id || session.session_key !== address.conversation) {
      throw new OperatorHold()
    }
    const ref = { session_id: address.conversation, profile_id: reg.canonicalProfile }
    const probe = await socket.request('prompt.forward_probe', { session_id: session.session_id })
    if (!open() || probe?.version !== 1 || probe.mode !== 'personal' || probe.route !== 'forward' ||
        !sameOperatorRef(probe.ref, ref) || typeof probe.generation !== 'string' || !probe.generation) throw new OperatorHold()
    return {
      sessionId: session.session_id, ref, forwardGeneration: probe.generation,
      socket: {
        protocol: socket.protocol,
        get open() { return open() },
        request(method, params) {
          if (!open()) return Promise.reject(new OperatorHold())
          return socket.request(method, params)
        },
        close() {
          socket.close()
          close()
        }
      }
    }
  } catch (error) {
    close()
    throw error
  }
}
