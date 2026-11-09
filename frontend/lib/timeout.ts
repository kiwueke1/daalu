import { ApiError } from "@/lib/api";

/** A query gave up waiting: the service behind it didn't answer in time. */
export class TimeoutError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TimeoutError";
  }
}

/** Default client-side wait for a query that depends on live infra. */
export const LIVE_INFRA_TIMEOUT_MS = 15_000;

/**
 * Reject with a TimeoutError if `p` hasn't settled within `ms`, so a page
 * backed by live infra (Prometheus, a GPU node) never spins forever.
 */
export function withTimeout<T>(
  p: Promise<T>,
  what: string,
  ms: number = LIVE_INFRA_TIMEOUT_MS,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const t = setTimeout(
      () => reject(new TimeoutError(`${what} didn't answer within ${ms / 1000}s.`)),
      ms,
    );
    p.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      },
    );
  });
}

/** True when the error means "the store behind this didn't answer". */
export function isUnreachable(error: unknown): boolean {
  return (
    error instanceof TimeoutError ||
    (error instanceof ApiError && (error.status === 502 || error.status === 504))
  );
}
