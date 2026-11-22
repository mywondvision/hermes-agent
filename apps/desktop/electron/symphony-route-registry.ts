import type { HostEvidence, PlanAddress, PlanEnvironment, PlanRegistration } from './symphony-route'

type HostTransport = Pick<PlanEnvironment, 'observe' | 'attach'>
const addressKey = (a: PlanAddress) => JSON.stringify([a.connectionId, a.profile, a.conversation])

/** Host-only registration. The live enrollment and K1 providers are separate from
 * the desktop renderer. With no provisioned entry, opening a window has no effects. */
export class PlanRouteRegistry implements PlanEnvironment {
  private entries = new Map<
    string,
    { reg: Readonly<PlanRegistration>; transport: HostTransport; sockets: Set<() => void> }
  >()

  install(reg: PlanRegistration, transport: HostTransport) {
    const key = addressKey(reg)
    this.revoke(reg)
    this.entries.set(key, { reg: Object.freeze({ ...reg }), transport, sockets: new Set() })
  }

  revoke(address: PlanAddress) {
    const key = addressKey(address)
    const entry = this.entries.get(key)
    this.entries.delete(key)
    if (entry) for (const close of [...entry.sockets]) close()
  }

  registration(address: PlanAddress) {
    return this.entries.get(addressKey(address))?.reg
  }

  observe(address: PlanAddress, attempt: string) {
    const entry = this.entries.get(addressKey(address))
    if (!entry) throw new Error('No registered Symphony route')
    return entry.transport.observe(address, attempt)
  }

  async attach(reg: Readonly<PlanRegistration>, evidence: HostEvidence) {
    const entry = this.entries.get(addressKey(reg))
    if (entry?.reg !== reg) throw new Error('Symphony registration changed')
    const tunnel = await entry.transport.attach(reg, evidence)
    if (this.registration(reg) !== reg) {
      tunnel.close()
      throw new Error('Symphony registration changed')
    }
    const close = () => {
      if (entry.sockets.delete(close)) tunnel.close()
    }
    entry.sockets.add(close)
    return { peerHostKey: tunnel.peerHostKey, open: tunnel.open.bind(tunnel), close }
  }
}

export const trustedPlanRoutes = new PlanRouteRegistry()
