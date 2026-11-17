// Anonymous visitor id for the public demo (no login): a random UUID made once per
// browser and sent as X-Visitor-Id, so the API lists only this browser's threads.
// localStorage can be unavailable (private mode, blocked storage): then the id lives
// for this page load only, which still keeps other visitors' threads out of view.
const KEY = "fira.visitor-id";
let memo: string | null = null;

// randomUUID needs a secure context (https, localhost): the demo always has one.
const newId = (): string => crypto.randomUUID();

export function visitorId(): string {
  if (memo) return memo;
  try {
    memo = localStorage.getItem(KEY);
    if (!memo) {
      memo = newId();
      localStorage.setItem(KEY, memo);
    }
  } catch {
    memo ??= newId();
  }
  return memo;
}

export const visitorHeaders = (): Record<string, string> => ({ "X-Visitor-Id": visitorId() });
