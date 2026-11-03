// Per-role accent colors, shared by phone cards and the live chart so the same
// node is the same color everywhere (information the old dashboard's operators
// relied on; hues kept, nothing else copied).
const ROLE_HEX: Record<string, string> = {
  chest: '#3b82f6',
  waist: '#a855f7',
  thigh_left: '#22c55e',
  thigh_right: '#14b8a6',
  ankle_left: '#eab308',
  ankle_right: '#f97316',
  wrist_left: '#ec4899',
  wrist_right: '#f43f5e',
};

const FALLBACK_HEX = '#a1a1aa';

export function roleHex(role: string): string {
  return ROLE_HEX[role] ?? FALLBACK_HEX;
}
