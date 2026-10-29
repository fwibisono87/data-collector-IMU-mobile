/**
 * localStorage holds ONLY view preferences — never session data (SPEC goal 2:
 * nothing important lives in a browser). Every access is wrapped in try/catch
 * because private modes and locked-down profiles throw.
 */
const PREFIX = 'imu.view.';

export function loadPref(key: string, fallback: string): string {
  try {
    return localStorage.getItem(PREFIX + key) ?? fallback;
  } catch {
    return fallback;
  }
}

export function savePref(key: string, value: string): void {
  try {
    localStorage.setItem(PREFIX + key, value);
  } catch {
    // Preference lost — the fallback stays in effect.
  }
}

export function loadNumberPref(key: string, fallback: number): number {
  const raw = loadPref(key, '');
  const n = Number(raw);
  return raw !== '' && Number.isFinite(n) ? n : fallback;
}

export function loadBoolPref(key: string, fallback: boolean): boolean {
  const raw = loadPref(key, '');
  return raw === '' ? fallback : raw === '1';
}
