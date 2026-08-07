// File System Access API typings.
//
// The lib.dom shipped with this TypeScript version does not declare showSaveFilePicker, and
// two separate modules need it (lib/zip_stream.ts streams the session archive,
// components/VideoRecoveryModal.tsx streams individual recovered cameras). Declaring it in
// each of them produced conflicting `declare global` blocks that only collided once both
// were merged, so it lives here as the single source of truth.
//
// `write` accepts Blob as well as Uint8Array: the recovery screen feeds IndexedDB chunks
// (Blobs) straight through without materialising them as typed arrays.

interface FileSystemWritableFileStreamLike {
  write(data: Uint8Array | Blob): Promise<void>;
  close(): Promise<void>;
  abort(): Promise<void>;
}

interface FileSystemFileHandleLike {
  createWritable(options?: { keepExistingData?: boolean }): Promise<FileSystemWritableFileStreamLike>;
}

interface SaveFilePickerOptions {
  suggestedName?: string;
  types?: Array<{ description?: string; accept: Record<string, string[]> }>;
}

interface Window {
  // Optional: absent on Firefox/Safari. Both call sites feature-detect before invoking.
  showSaveFilePicker?: (options?: SaveFilePickerOptions) => Promise<FileSystemFileHandleLike>;
}
