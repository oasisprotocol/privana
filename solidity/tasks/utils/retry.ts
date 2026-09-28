function isTransientFetchError(err: unknown): boolean {
  // Node's fetch throws a generic TypeError("fetch failed") for transient network
  // issues (DNS hiccup, connection reset, etc.), as opposed to the plain Error
  // fetchJson throws for a well-formed non-2xx HTTP response.
  return err instanceof TypeError && err.message.includes("fetch failed");
}

export async function withRetry<T>(
  fn: () => Promise<T>,
  { retries = 5, delayMs = 500 }: { retries?: number; delayMs?: number } = {},
): Promise<T> {
  for (let attempt = 1; ; attempt++) {
    try {
      return await fn();
    } catch (err) {
      if (!isTransientFetchError(err) || attempt > retries) {
        throw err;
      }
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
}
