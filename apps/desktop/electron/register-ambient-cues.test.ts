import { expect, it, vi } from 'vitest'

const ipc = vi.hoisted(() => ({
  created: vi.fn(),
  listen: vi.fn(),
  handle: vi.fn(),
  windows: vi.fn()
}))
vi.mock('electron', () => ({
  app: { on: ipc.created },
  ipcMain: { on: ipc.listen, handle: ipc.handle },
  BrowserWindow: { getAllWindows: ipc.windows }
}))

import { registerAmbientCues } from './register-ambient-cues'

it('requires the same sole window across a recording, even if its peer closes before readback', () => {
  registerAmbientCues()
  const created = ipc.created.mock.calls.find(row => row[0] === 'browser-window-created')![1]
  const observe = ipc.listen.mock.calls.find(row => row[0] === 'hermes:voice:single-window-generation')![1]
  const first = { webContents: {} },
    second = { webContents: {} }
  function snapshot(sender: object) {
    const event = { sender, returnValue: null }
    observe(event)
    return event.returnValue
  }
  created()
  ipc.windows.mockReturnValue([first])
  const start = snapshot(first.webContents)
  expect(typeof start).toBe('number')
  expect(snapshot(second.webContents)).toBe(null)
  created()
  ipc.windows.mockReturnValue([first, second])
  expect(snapshot(first.webContents)).toBe(null)
  ipc.windows.mockReturnValue([first])
  expect(snapshot(first.webContents)).not.toBe(start)
  expect(typeof snapshot(first.webContents)).toBe('number')
})
