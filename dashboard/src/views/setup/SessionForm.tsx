import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/errors';
import type { DraftUpdate } from '../../api/types';
import { useDebounced } from '../../lib/useDebounced';
import { useSnapshot, useTransport } from '../../state/store';

type DraftField = 'subject' | 'session_tag' | 'operator';

const FIELDS: Array<{ field: DraftField; label: string; placeholder: string }> = [
  { field: 'subject', label: 'Subject', placeholder: 'e.g. S012' },
  { field: 'session_tag', label: 'Tag', placeholder: 'e.g. baseline' },
  { field: 'operator', label: 'Operator', placeholder: 'e.g. francis' },
];

/**
 * Session form (SPEC §5.1). The draft lives on the SERVER: every edit is PUT
 * after a 400 ms debounce, so a reload or a second browser shows the same
 * values. Inputs reflect the server draft but never overwrite the field the
 * operator is currently typing in.
 */
export function SessionForm() {
  return (
    <section className="flex h-full flex-col gap-3 rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
      <h2 className="text-sm font-black uppercase tracking-wider text-zinc-400">Session</h2>
      {FIELDS.map((f) => (
        <DraftInput key={f.field} field={f.field} label={f.label} placeholder={f.placeholder} />
      ))}
      <p className="mt-auto text-xs text-zinc-500">
        Saved to the backend as you type — every browser sees the same draft.
      </p>
    </section>
  );
}

function DraftInput({
  field,
  label,
  placeholder,
}: {
  field: DraftField;
  label: string;
  placeholder: string;
}) {
  const transport = useTransport();
  const serverValue = useSnapshot((s) =>
    s.snapshot ? (s.snapshot.draft[field] as string) ?? '' : '',
  );
  const [local, setLocal] = useState(serverValue);
  const [error, setError] = useState<string | null>(null);
  const editingRef = useRef(false);

  // Reflect the server draft, but never clobber the field being typed in.
  useEffect(() => {
    if (!editingRef.current) setLocal(serverValue);
  }, [serverValue]);

  const save = useDebounced((value: string) => {
    transport
      .putDraft({ [field]: value } as DraftUpdate)
      .then(() => setError(null))
      .catch((e) => setError(errorMessage(e)));
  }, 400);

  return (
    <label className="block">
      <span className="mb-1 block text-xs font-bold uppercase tracking-wide text-zinc-400">
        {label}
      </span>
      <input
        type="text"
        value={local}
        placeholder={placeholder}
        autoComplete="off"
        aria-label={label}
        aria-invalid={error !== null}
        className={`w-full rounded border bg-zinc-950 px-2 py-2 text-lg font-semibold text-zinc-100 outline-none focus:border-cyan-400 ${
          error ? 'border-red-500' : 'border-zinc-700'
        }`}
        onFocus={() => {
          editingRef.current = true;
        }}
        onBlur={() => {
          editingRef.current = false;
          save.flush();
        }}
        onChange={(e) => {
          setLocal(e.target.value);
          save.schedule(e.target.value);
        }}
      />
      {error && <span className="mt-1 block text-xs text-red-400">{error}</span>}
    </label>
  );
}
