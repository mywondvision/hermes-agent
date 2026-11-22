import { BrowserWindow, app, clipboard, dialog, ipcMain, safeStorage } from 'electron'

import { openOperatorChannel } from './symphony-operator-channel'
import { SymphonyOperatorClient } from './symphony-operator-client'
import type { OperatorCall, OperatorReply } from './symphony-operator-types'
import { sameOperatorAddress, SymphonyOperatorJournal, type OperatorAddress } from './symphony-operator-journal'
import { operatorPublicKey } from './symphony-operator-wire'
import type { PlanAddress, PlanEnvironment } from './symphony-route'

interface Options {
  rendererUrl(): string
  matchesWindow(window: BrowserWindow, address: PlanAddress): boolean
  environment: PlanEnvironment
  createWindow(address: OperatorAddress, source: BrowserWindow): BrowserWindow
  showMessageBox?: typeof dialog.showMessageBox
}

const hold: OperatorReply = { state: 'HOLD' }
const id = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 512

function parseCall(raw: unknown): OperatorCall | undefined {
  if (!raw || typeof raw !== 'object') return
  const data = raw as Record<string, unknown>
  switch (data.action) {
    case 'submit':
      if (id(data.submissionId) && typeof data.text === 'string' && data.text.trim() && data.text.length <= 1_048_576) {
        return { action: 'submit', submissionId: data.submissionId, text: data.text }
      }
      return
    case 'receipt':
      if (id(data.submissionId)) return { action: 'receipt', submissionId: data.submissionId }
      if (data.submissionId === undefined) return { action: 'receipt' }
      return
    case 'status':
    case 'stop':
      if (id(data.attemptId)) return { action: data.action, attemptId: data.attemptId }
      if (data.attemptId === undefined) return { action: data.action }
      return
    case 'stop-reconcile':
    case 'stop-replace':
      if (id(data.attemptId)) return { action: data.action, attemptId: data.attemptId }
      return
    case 'patch-decision':
      if (typeof data.text === 'string' && data.text.length <= 512) return { action: 'patch-decision', text: data.text }
  }
}

function displays(expectedUrl: string, actualUrl: string, session: string) {
  if (!URL.canParse(actualUrl)) return false
  const expected = new URL(expectedUrl)
  const actual = new URL(actualUrl)
  return actual.protocol === expected.protocol && actual.host === expected.host && actual.pathname === expected.pathname &&
    actual.hash.slice(1).split(/[?#]/, 1)[0] === '/' + encodeURIComponent(session)
}

export function registerSymphonyOperator(options: Options) {
  const messageBox = options.showMessageBox ?? dialog.showMessageBox.bind(dialog)
  const journal = new SymphonyOperatorJournal(app.getPath('userData'), safeStorage)
  const client = new SymphonyOperatorClient<BrowserWindow>(journal, address => openOperatorChannel(options.environment, address),
    async (window, stop) => {
      if (window.isDestroyed() || !window.isFocused()) return false
      const { response } = await messageBox(window, {
        type: 'question', title: 'Symphony STOP',
        message: 'Send a new STOP for this attempt?',
        detail: `Attempt: ${stop.attemptId}\nOriginal STOP: ${stop.id}`,
        buttons: ['Cancel', 'Send new STOP'], defaultId: 0, cancelId: 0,
      })
      return response === 1
    }, {
      focused: window => !window.isDestroyed() && window.isFocused(),
      async confirm(window, patch, action) {
        const { response } = await messageBox(window, {
          type: 'question', title: 'Symphony',
          message: action === 'apply' ? 'Apply this checked patch?' : 'Keep this checked patch in the archive?',
          detail: `/symphony ${action} ${patch.job} ${patch.patchSha256}\nAttempt ${patch.attemptId}\nCAPTURE ${patch.captureSeq} · CHECK ${patch.checkSeq}`,
          buttons: ['Cancel', action === 'apply' ? 'Apply patch' : 'Keep in archive'], defaultId: 0, cancelId: 0,
          noLink: true,
        })
        return response === 1
      },
    })
  const windows = new Map<number, BrowserWindow>()
  const watching = new WeakSet<BrowserWindow>()
  const navigating = new WeakSet<BrowserWindow>()
  const lifetimes = new WeakMap<BrowserWindow, { address: OperatorAddress }>()

  const clear = (window: BrowserWindow) => {
    if (!window.isDestroyed() && !window.webContents.isDestroyed()) window.webContents.send('hermes:symphony-operator:clear')
  }

  const drop = (window: BrowserWindow) => {
    if (lifetimes.delete(window)) clear(window)
    client.drop(window)
  }

  function watch(window: BrowserWindow) {
    if (watching.has(window)) return
    watching.add(window)
    const sender = window.webContents
    windows.set(sender.id, window)
    sender.on('did-start-navigation', detail => {
      const active = lifetimes.get(window)
      if (detail.isMainFrame && (!detail.isSameDocument ||
          (active && !displays(options.rendererUrl(), detail.url, active.address.storedConversation)))) {
        navigating.add(window)
        drop(window)
      }
    })
    sender.on('did-navigate', () => navigating.delete(window))
    sender.on('did-navigate-in-page', (_event, _url, main) => { if (main) navigating.delete(window) })
    sender.on('render-process-gone', () => drop(window))
    sender.once('destroyed', () => {
      windows.delete(sender.id)
      drop(window)
    })
  }

  ipcMain.handle('hermes:symphony-operator:request', async (event, address: PlanAddress, raw: unknown): Promise<OperatorReply> => {
    const { sender } = event
    const window = BrowserWindow.fromWebContents(sender)
    const call = parseCall(raw)
    if (!window || event.senderFrame !== sender.mainFrame || !call || !address ||
        ![address.connectionId, address.profile, address.conversation].every(id)) return hold
    const reg = options.environment.registration(address)
    if (!reg) return hold
    const scoped = () => !window.isDestroyed() && !navigating.has(window) && event.senderFrame === sender.mainFrame &&
      options.environment.registration(reg) === reg && options.matchesWindow(window, reg) &&
      displays(options.rendererUrl(), sender.getURL(), reg.storedConversation)
    if (!scoped()) return hold
    watch(window)
    const requested = {
      connectionId: reg.connectionId, profile: reg.profile, conversation: reg.conversation, storedConversation: reg.storedConversation
    }
    const current = lifetimes.get(window) ?? { address: requested }
    if (!sameOperatorAddress(current.address, requested)) return hold
    lifetimes.set(window, current)
    return client.request(window, current.address, () => lifetimes.get(window) === current && scoped(), call)
  })

  let selecting = false
  async function restore(source: BrowserWindow) {
    if (selecting) return
    selecting = true
    try {
      const choices = client.recoverable().filter(row => options.environment.registration(row.address) &&
        options.matchesWindow(source, row.address) && displays(options.rendererUrl(), source.webContents.getURL(), row.address.storedConversation))
      if (choices.length === 0) throw new Error('No protected binding for this conversation')
      const { response } = await messageBox(source, {
        type: 'question', title: 'Restore Symphony window',
        message: 'Restore control for one submission in a new window.',
        buttons: ['Cancel', ...choices.map(row => row.submissionId)], defaultId: 0, cancelId: 0
      })
      if (!response) return
      const row = choices[response - 1]
      if (!row || source.isDestroyed() || !options.matchesWindow(source, row.address) ||
          !displays(options.rendererUrl(), source.webContents.getURL(), row.address.storedConversation)) return
      const replacement = options.createWindow(row.address, source)
      try {
        replacement.once('closed', () => drop(replacement))
        for (const previous of client.restore(replacement, row)) clear(previous)
      } catch (error) {
        replacement.destroy()
        throw error
      }
    } catch {
      if (!source.isDestroyed()) await messageBox(source, {
        type: 'warning', message: 'Symphony could not restore a protected window for this conversation.'
      })
    } finally {
      selecting = false
    }
  }

  function controlWindow(event: Electron.IpcMainInvokeEvent) {
    const window = BrowserWindow.fromWebContents(event.sender)
    const url = event.sender.getURL()
    if (!window?.isFocused() || event.senderFrame !== event.sender.mainFrame || !URL.canParse(url)) return
    const route = new URL(url).hash.slice(2).split(/[?#]/, 1)[0]
    if (route && displays(options.rendererUrl(), url, decodeURIComponent(route))) return window
  }

  ipcMain.handle('hermes:symphony-operator:restore-window', async event => {
    const source = controlWindow(event)
    if (source) await restore(source)
  })
  ipcMain.handle('hermes:symphony-operator:copy-installation-key', async event => {
    const source = controlWindow(event)
    if (!source) return
    const { response } = await messageBox(source, {
      type: 'question', title: 'Symphony installation key',
      message: 'Copy only the public key for trusted enrollment?',
      buttons: ['Cancel', 'Copy public key'], defaultId: 0, cancelId: 0
    })
    if (response !== 1 || source.isDestroyed()) return
    try {
      clipboard.writeText(operatorPublicKey(journal.installation().key))
    } catch {
      await messageBox(source, {
        type: 'warning', message: 'Protected Symphony installation storage is unavailable.'
      })
    }
  })

  return {
    restore,
    drop(id: number) {
      const window = windows.get(id)
      if (window) drop(window)
    }
  }
}
