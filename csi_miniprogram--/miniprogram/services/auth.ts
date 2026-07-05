let resolved = false
let authSucceeded = false
let resolveReady: (success: boolean) => void = () => undefined
let readyPromise = new Promise<boolean>((resolve) => { resolveReady = resolve })

export function markAuthReady(success: boolean): void {
  authSucceeded = success
  resolved = true
  resolveReady(success)
}

export function waitForAuth(): Promise<boolean> {
  return resolved ? Promise.resolve(authSucceeded) : readyPromise
}

export function resetAuth(): boolean {
  if (!resolved) return false
  resolved = false
  authSucceeded = false
  readyPromise = new Promise<boolean>((resolve) => { resolveReady = resolve })
  return true
}
