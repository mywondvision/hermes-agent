import { contextBridge, ipcRenderer } from 'electron'

import { symphonyOperatorBridge } from './symphony-operator-bridge'

contextBridge.exposeInMainWorld('operatorFixture', symphonyOperatorBridge(ipcRenderer))
