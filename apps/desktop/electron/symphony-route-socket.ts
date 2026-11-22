import type { PlanSocket } from './symphony-route'

export class PlanRouteRefused extends Error {}
export class OperatorRouteRefused extends Error {}

/** One already pinned SSH tunnel and one negotiated WebSocket. Test transport
 * carries the same JSON frames over pipes. Reconnect always creates a new instance. */
export interface PlanWire {
  protocol: string
  send(frame: string): void
  listen(frame: (value: string) => void, closed: () => void): () => void
  close(): void
}

export function planSocket(wire: PlanWire, event: (value: unknown) => void, closed: () => void): PlanSocket {
  let open = true
  let next = 0
  const waiting = new Map<number, { resolve: (value: any) => void; reject: (error: Error) => void }>()
  const finish = () => {
    if (!open) return
    open = false
    for (const call of waiting.values()) call.reject(new Error('Symphony plan transport disconnected'))
    waiting.clear()
    closed()
  }
  const unlisten = wire.listen(raw => {
    let frame
    try {
      frame = JSON.parse(raw)
    } catch {
      return
    }
    if (frame?.jsonrpc !== '2.0') return
    if (frame.method === 'event' && frame.id === undefined) {
      event(frame)
      return
    }
    const call = waiting.get(frame.id)
    if (!call) return
    waiting.delete(frame.id)
    if (frame.error?.code === -32003) call.reject(new PlanRouteRefused('Symphony plan authorization declined'))
    else if (frame.error?.code === 4090 && frame.error.message === 'OPERATOR_HOLD') {
      call.reject(new OperatorRouteRefused('Symphony operator action held'))
    }
    else if (frame.error || !Object.hasOwn(frame, 'result')) call.reject(new Error('Symphony plan request declined'))
    else call.resolve(frame.result)
  }, finish)
  const disconnect = () => {
    if (!open) return
    finish()
    unlisten()
    wire.close()
  }
  return {
    protocol: wire.protocol,
    get open() {
      return open
    },
    request(method, params) {
      if (!open) return Promise.reject(new Error('Symphony plan transport disconnected'))
      const id = ++next
      return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
          disconnect()
        }, 60_000)
        waiting.set(id, {
          resolve(value) {
            clearTimeout(timeout)
            resolve(value)
          },
          reject(error) {
            clearTimeout(timeout)
            reject(error)
          }
        })
        try {
          wire.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }))
        } catch {
          disconnect()
        }
      })
    },
    close: disconnect
  }
}
