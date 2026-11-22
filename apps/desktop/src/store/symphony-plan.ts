import { map } from 'nanostores'

// Events wake the viewer. The full plan always comes from status on its own native connection.
export const $symphonyPlanSignals = map<Record<string, string>>({})

export function signalSymphonyPlan(session: string, digest: string) {
  $symphonyPlanSignals.setKey(session, digest)
}
