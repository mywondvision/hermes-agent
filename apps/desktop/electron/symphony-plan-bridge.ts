import type { IpcRenderer } from 'electron'
import type { PlanAddress, PlanOperation } from './symphony-route'

export function symphonyPlanBridge(ipc: Pick<IpcRenderer, 'invoke' | 'on' | 'removeListener'>) {
  return {
    request: (address: PlanAddress, verb: PlanOperation, data?: object): Promise<{ output: string }> =>
      ipc.invoke('hermes:symphony-plan:request', address, verb, data),
    onSignal(callback: (address: PlanAddress, disconnected: boolean) => void) {
      const listener = (_event: unknown, address: PlanAddress, disconnected: boolean) => callback(address, disconnected)
      ipc.on('hermes:symphony-plan:signal', listener)
      return () => {
        ipc.removeListener('hermes:symphony-plan:signal', listener)
      }
    }
  }
}
