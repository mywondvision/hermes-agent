import { BrowserWindow, ipcMain } from 'electron'
import { SymphonyRouteClient } from './symphony-route-client'
import { trustedPlanRoutes } from './symphony-route-registry'
import { PlanNotSent } from './symphony-route'
import type { PlanAddress, PlanEnvironment, PlanOperation, PlanRegistration } from './symphony-route'

// Only the host can provision a route. No renderer IPC for enrollment or key access.
// Live enrollment and sourcing independent K1 evidence require separate qualification.
export function registerSymphonyRoute(
  rendererUrl: () => string,
  matchesWindow: (id: number, address: PlanAddress) => boolean,
  environment: PlanEnvironment = trustedPlanRoutes
) {
  const windows = new Map<number, SymphonyRouteClient>()
  const selectedChats = new Map<number, Readonly<PlanRegistration>>()
  const senders = new Map<number, Electron.WebContents>()
  const navigating = new WeakSet<Electron.WebContents>()
  const watched = new WeakSet<Electron.WebContents>()
  const drop = (id: number) => {
    const reg = selectedChats.get(id)
    const sender = senders.get(id)
    windows.get(id)?.dispose()
    windows.delete(id)
    selectedChats.delete(id)
    if (reg && sender && !sender.isDestroyed() && displays(sender, reg)) {
      const { connectionId, profile, conversation } = reg
      sender.send('hermes:symphony-plan:signal', { connectionId, profile, conversation }, true)
    }
  }
  function displays(sender: Electron.WebContents, reg: Readonly<PlanRegistration>, url = sender.getURL()) {
    if (sender.isDestroyed() || !matchesWindow(sender.id, reg)) return false
    const expected = new URL(rendererUrl())
    if (!URL.canParse(url)) return false
    const actual = new URL(url)
    // The hash router addresses the stored chat. Its independently pinned runtime
    // conversation can have another ID; renderer command fields cannot select either.
    return (
      actual.protocol === expected.protocol &&
      actual.host === expected.host &&
      actual.pathname === expected.pathname &&
      actual.hash.slice(1).split(/[?#]/, 1)[0] === '/' + encodeURIComponent(reg.storedConversation)
    )
  }
  function registration(sender: Electron.WebContents, address: PlanAddress) {
    if (navigating.has(sender)) return undefined
    const reg = environment.registration(address)
    return reg && displays(sender, reg) ? reg : undefined
  }
  ipcMain.handle(
    'hermes:symphony-plan:request',
    async (event, address: PlanAddress, verb: PlanOperation, data?: object) => {
      const sender = event.sender
      const selected = address && registration(sender, address)
      if (!address || event.senderFrame !== sender.mainFrame || !BrowserWindow.fromWebContents(sender)) {
        throw new Error('Symphony plan route is not registered')
      }
      const unsent = { output: '', delivery: 'NOT_SENT' as const }
      if (!selected) {
        if (verb === 'plan-answer') return unsent
        throw new Error('Symphony plan route is not registered')
      }
      let client = windows.get(sender.id)
      if (!client) {
        const bound: PlanEnvironment = {
          registration: target => registration(sender, target),
          observe: environment.observe.bind(environment),
          attach: environment.attach.bind(environment)
        }
        client = new SymphonyRouteClient(bound, (target, disconnected) => {
          if (registration(sender, target) || (disconnected && displays(sender, selected)))
            sender.send('hermes:symphony-plan:signal', target, disconnected)
        })
        windows.set(sender.id, client)
        selectedChats.set(sender.id, selected)
      }
      if (!watched.has(sender)) {
        watched.add(sender)
        senders.set(sender.id, sender)
        const leaving = (url: string) => {
          const active = selectedChats.get(sender.id)
          return active && !displays(sender, active, url)
        }
        sender.on('did-start-navigation', details => {
          if (details.isMainFrame && (!details.isSameDocument || leaving(details.url))) {
            navigating.add(sender)
            drop(sender.id)
          }
        })
        sender.on('did-navigate-in-page', (_event, url, isMainFrame) => {
          if (isMainFrame) {
            if (leaving(url)) drop(sender.id)
            navigating.delete(sender)
          }
        })
        sender.on('did-navigate', () => navigating.delete(sender))
        sender.once('destroyed', () => {
          senders.delete(sender.id)
          drop(sender.id)
        })
      }
      let result
      try {
        result = await client.request(address, verb, data)
      } catch (error) {
        if (verb === 'plan-answer' && error instanceof PlanNotSent) return unsent
        throw error
      }
      if (windows.get(sender.id) !== client || !registration(sender, address))
        throw new Error('Symphony window route changed')
      return result
    }
  )
  return drop
}
