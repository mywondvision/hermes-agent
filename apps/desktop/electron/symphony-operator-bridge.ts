import type { IpcRenderer } from 'electron'
import type { OperatorCall } from './symphony-operator-types'
import type { PlanAddress } from './symphony-route'

export function symphonyOperatorBridge(ipc: Pick<IpcRenderer, 'invoke' | 'on' | 'removeListener'>) {
  return {
    request: (address: PlanAddress, call: OperatorCall) => ipc.invoke('hermes:symphony-operator:request', address, call),
    restoreWindow: () => ipc.invoke('hermes:symphony-operator:restore-window'),
    copyInstallationKey: () => ipc.invoke('hermes:symphony-operator:copy-installation-key'),
    onClear(callback: () => void) {
      const listener = () => callback()
      ipc.on('hermes:symphony-operator:clear', listener)
      return () => { ipc.removeListener('hermes:symphony-operator:clear', listener) }
    }
  }
}
