/** In-memory WebSocket transport. HermesGateway owns JSON-RPC ids, encoding and response matching. */
interface TaskWire {
  send(raw: string): Promise<void>
  close(): Promise<void>
}

interface TaskDial {
  (url: string, protocols: string[], receive: (raw: string) => void): Promise<TaskWire>
}

export function taskWebSocket(dial: TaskDial) {
  return class extends EventTarget {
    static readonly OPEN = 1
    static readonly CLOSED = 3
    readyState = 0
    private wire?: TaskWire

    constructor(
      readonly url: string,
      protocols?: string | string[]
    ) {
      super()
      const early: string[] = []
      const deliver = (raw: string) => this.dispatchEvent(new MessageEvent('message', { data: raw }))
      void dial(url, typeof protocols === 'string' ? [protocols] : (protocols ?? []), raw => {
        if (this.readyState === 0) early.push(raw)
        else if (this.readyState === 1) deliver(raw)
      })
        .then(async wire => {
          if (this.readyState === 3) {
            await wire.close()
            return
          }
          this.wire = wire
          this.readyState = 1
          this.dispatchEvent(new Event('open'))
          early.forEach(deliver)
        })
        .catch(() => {
          this.dispatchEvent(new Event('error'))
          this.close()
        })
    }

    send(raw: string) {
      if (!this.wire || this.readyState !== 1) throw new Error('Socket is closed')
      void this.wire.send(raw).catch(() => this.close())
    }

    close() {
      if (this.readyState === 3) return
      this.readyState = 3
      this.dispatchEvent(new CloseEvent('close', { code: 1000 }))
      void this.wire?.close().catch(() => undefined)
    }
  }
}
