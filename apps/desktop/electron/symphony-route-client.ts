import {
  openPlanSocket,
  PlanNotSent,
  planOperations,
  type PlanAddress,
  type PlanEnvironment,
  type PlanOperation
} from './symphony-route'

type Channel = Awaited<ReturnType<typeof openPlanSocket>>
type Notice = (address: PlanAddress, disconnected: boolean) => void

/** One instance belongs to one renderer window. A second window proves its own socket. */
export class SymphonyRouteClient {
  private channels = new Map<string, Promise<Channel>>()
  private readonly lifetime = new AbortController()

  constructor(
    private readonly env: PlanEnvironment,
    private readonly notify: Notice
  ) {}

  dispose() {
    this.lifetime.abort()
    this.channels.clear()
  }

  async request(address: PlanAddress, verb: PlanOperation, data?: object): Promise<{ output: string }> {
    if (
      this.lifetime.signal.aborted ||
      !address ||
      ![address.connectionId, address.profile, address.conversation].every(
        v => typeof v === 'string' && v.length > 0 && v.length <= 4096
      ) ||
      !planOperations.includes(verb)
    ) {
      throw new PlanNotSent('Invalid Symphony plan request')
    }
    const key = JSON.stringify([address.connectionId, address.profile, address.conversation])
    let channel = this.channels.get(key)
    if (channel) {
      const current = await channel.catch(() => {
        throw new PlanNotSent('Symphony plan connection unavailable')
      })
      if (!current.live()) {
        if (this.channels.get(key) === channel) this.channels.delete(key)
        current.close()
        channel = this.channels.get(key)
      }
    }
    if (!channel) {
      // Only a read opens another socket. A lost response can never re-send an answer.
      if (verb === 'plan-answer' || verb === 'plan-presented')
        throw new PlanNotSent('Symphony plan requires a fresh display')
      const connecting: Promise<Channel> = openPlanSocket(
        this.env,
        this.lifetime.signal,
        address,
        () => {
          if (this.channels.get(key) === connecting) this.notify(address, false)
        },
        () => {
          if (this.channels.get(key) === connecting) {
            this.channels.delete(key)
            this.notify(address, true)
          }
        }
      )
      this.channels.set(key, connecting)
      channel = connecting
      void connecting.catch(() => {
        if (this.channels.get(key) === connecting) this.channels.delete(key)
      })
    }
    const opened = await channel.catch(() => {
      throw new PlanNotSent('Symphony plan connection unavailable')
    })
    if (this.lifetime.signal.aborted || this.channels.get(key) !== channel || !opened.live())
      throw new PlanNotSent('Symphony plan disconnected')
    return opened.command(verb, data)
  }
}
