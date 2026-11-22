import { app, BrowserWindow, ipcMain } from 'electron'
import { createAmbientClaimArbiter } from './event-dedupe'

export function registerAmbientCues() {
  const ownsCue = createAmbientClaimArbiter()
  ipcMain.handle('hermes:ambient:claim', (_event, key: unknown) => ownsCue(String(key ?? '')))

  // Only one renderer can observe all local speech when it is the sole window.
  // Keep a generation so a window opened and closed within one recording is visible too.
  let generation = 0
  app.on('browser-window-created', () => {
    generation += 1
  })
  ipcMain.on('hermes:voice:single-window-generation', event => {
    const windows = BrowserWindow.getAllWindows()
    event.returnValue = windows.length === 1 && windows[0]?.webContents === event.sender ? generation : null
  })
}
