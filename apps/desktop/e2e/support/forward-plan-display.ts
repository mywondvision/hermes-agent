import { isInaccessible, waitFor, within } from '@testing-library/react'
import { expect } from 'vitest'

/** DISPLAY already contains the full digest. An edit belongs to the plan only after READY. */
export async function waitForReadyPlan(container: HTMLElement, requestId: string, digest: string) {
  await waitFor(() => {
    const card = within(container).getByRole('region', { name: 'План Symphony' })
    const content = within(card)
    expect(content.getByText('План Symphony · ' + requestId)).toBeTruthy()
    expect(content.getByText('SHA-256 ' + digest)).toBeTruthy()
    const status = content.getByRole('status')
    expect(isInaccessible(status)).toBe(false)
    expect(status.textContent).toMatch(/^Напишите «подтверждаю» или «отклоняю»/)
  }, { timeout: 25000 })
}
