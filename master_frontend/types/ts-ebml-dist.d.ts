// Type declarations for ts-ebml's browserified UMD bundle (dist/EBML.js).
// We only use the Decoder / Reader / tools surface for finalizing MediaRecorder WebM.
declare module "ts-ebml/dist/EBML" {
  export interface MakeSeekableCue {
    CueTrack: number;
    CueClusterPosition: number;
    CueTime: number;
  }
  export class Decoder {
    decode(buf: ArrayBuffer): Array<Record<string, unknown>>;
  }
  export class Reader {
    read(elm: Record<string, unknown>): void;
    stop(): void;
    metadatas: Array<Record<string, unknown>>;
    cues: MakeSeekableCue[];
    metadataSize: number;
    duration: number;
    timestampScale: number;
  }
  export class Encoder {}
  export const tools: {
    makeMetadataSeekable(
      metadatas: Array<Record<string, unknown>>,
      duration: number,
      cues: MakeSeekableCue[],
    ): ArrayBuffer;
  };
  export const version: string;
}
