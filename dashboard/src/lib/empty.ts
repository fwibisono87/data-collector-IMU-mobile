import type { Camera, Notice, Phone } from '../api/types';

// Stable empty arrays so selectors keep returning the same reference when the
// snapshot is null (useSyncExternalStore would loop on fresh objects).
export const EMPTY_PHONES: Phone[] = [];
export const EMPTY_CAMERAS: Camera[] = [];
export const EMPTY_ARR: Notice[] = [];
