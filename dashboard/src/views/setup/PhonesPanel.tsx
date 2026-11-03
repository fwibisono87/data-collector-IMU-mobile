import { useState } from 'react';
import { errorMessage } from '../../api/errors';
import type { Phone } from '../../api/types';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { EMPTY_PHONES } from '../../lib/empty';
import { fmtAge, fmtDuration, fmtNumber } from '../../lib/format';
import { roleHex } from '../../lib/roleColors';
import { useNow } from '../../lib/useNow';
import { useSnapshot, useTransport } from '../../state/store';

const LOW_HZ = 90; // SPEC §5.1: below this the true rate gets a red badge

/**
 * One card per phone (SPEC §5.1): role in large text, online/offline, true Hz
 * with a red badge below 90, packets, app version, telemetry age. The ⋯ menu
 * holds "Reset phone connections" behind a confirm dialog; it is disabled
 * while recording.
 */
export function PhonesPanel() {
  const phones = useSnapshot((s) => s.snapshot?.phones ?? EMPTY_PHONES);
  const state = useSnapshot((s) => s.snapshot?.state ?? null);
  const transport = useTransport();
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: 'error' | 'info'; text: string } | null>(null);
  const recording = state === 'RECORDING' || state === 'FINALIZING' || state === 'VALIDATING';

  // Version mismatch among online phones is shown on the cards (SPEC §5.1).
  const versions = [...new Set(phones.filter((p) => p.online).map((p) => p.app_version))];
  const mismatch = versions.length > 1;

  const reset = () => {
    setBusy(true);
    transport
      .resetPhones()
      .then(() => setMsg({ kind: 'info', text: 'Phone connections reset — phones reconnect automatically.' }))
      .catch((e) => setMsg({ kind: 'error', text: errorMessage(e) }))
      .finally(() => {
        setBusy(false);
        setConfirmOpen(false);
      });
  };

  return (
    <section className="flex h-full min-h-0 flex-col rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-black uppercase tracking-wider text-zinc-400">
          Phones <span className="text-zinc-500">({phones.filter((p) => p.online).length}/{phones.length})</span>
        </h2>
        <div className="relative">
          <button
            type="button"
            aria-label="Phone actions"
            aria-expanded={menuOpen}
            className="rounded px-2 py-0.5 text-lg font-bold text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
            onClick={() => setMenuOpen((v) => !v)}
          >
            ⋯
          </button>
          {menuOpen && (
            <div className="absolute right-0 z-20 mt-1 w-60 rounded border border-zinc-700 bg-zinc-900 p-1 shadow-xl">
              <button
                type="button"
                disabled={recording}
                className="w-full rounded px-2 py-1.5 text-left text-sm font-semibold hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
                onClick={() => {
                  setMenuOpen(false);
                  setConfirmOpen(true);
                }}
              >
                Reset phone connections
                {recording && <span className="block text-xs font-normal text-zinc-500">disabled while recording</span>}
              </button>
            </div>
          )}
        </div>
      </div>

      {msg && (
        <p className={`mb-2 text-xs ${msg.kind === 'error' ? 'text-red-400' : 'text-green-400'}`} role="status">
          {msg.text}
        </p>
      )}

      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto pr-1">
        {phones.length === 0 && (
          <p className="text-sm text-zinc-500">
            No phones connected. Open the app on each phone; they discover the laptop via mDNS.
          </p>
        )}
        {phones.map((p) => (
          <PhoneCard key={p.device_id} phone={p} versionMismatch={mismatch} />
        ))}
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title="Reset phone connections?"
        body="Closes all phone links and clears connectivity history. Phones reconnect automatically within a few seconds. Not available while recording."
        confirmLabel="Reset connections"
        danger
        busy={busy}
        onConfirm={reset}
        onCancel={() => setConfirmOpen(false)}
      />
    </section>
  );
}

function PhoneCard({ phone, versionMismatch }: { phone: Phone; versionMismatch: boolean }) {
  const now = useNow(1000);
  const lowHz = phone.online && phone.true_hz_avg > 0 && phone.true_hz_avg < LOW_HZ;
  const offlineFor =
    !phone.online && phone.offline_since_ms ? fmtDuration(now - phone.offline_since_ms) : null;

  return (
    <div
      className={`rounded-lg border p-2 ${
        phone.online ? 'border-zinc-700 bg-zinc-950/60' : 'border-red-500/50 bg-red-950/30'
      }`}
      data-testid={`phone-card-${phone.role}`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span
          className="min-w-0 truncate text-2xl font-black tracking-tight"
          style={{ color: roleHex(phone.role) }}
          title={phone.role}
        >
          {phone.role}
        </span>
        {/* The badge never shrinks or wraps: at 1024 px the role ellipsises instead. */}
        {phone.online ? (
          <span className="shrink-0 whitespace-nowrap rounded bg-green-500/15 px-2 py-0.5 text-xs font-black text-green-300">
            ONLINE
          </span>
        ) : (
          <span className="shrink-0 whitespace-nowrap rounded bg-red-500/15 px-2 py-0.5 text-xs font-black tabular-nums text-red-300">
            OFFLINE{offlineFor ? ` ${offlineFor}` : ''}
          </span>
        )}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs tabular-nums text-zinc-400">
        <span className={`text-sm font-bold ${lowHz ? '' : 'text-zinc-200'}`}>
          {phone.true_hz_avg > 0 ? `${phone.true_hz_avg.toFixed(1)} Hz` : '— Hz'}
        </span>
        {lowHz && (
          <span className="rounded bg-red-500/20 px-1.5 py-0.5 text-[11px] font-black text-red-300">
            below {LOW_HZ} Hz
          </span>
        )}
        <span title="packets received">{fmtNumber(phone.packets)} pkt</span>
        <span title="app version">
          app {phone.app_version}
          {versionMismatch && phone.online && (
            <span className="ml-1 rounded bg-amber-500/20 px-1 py-0.5 text-[10px] font-bold text-amber-300">
              versions differ
            </span>
          )}
        </span>
        <span title="age of the last telemetry packet">age {fmtAge(phone.telemetry_age_ms)}</span>
      </div>
      <div className="mt-0.5 truncate text-[11px] text-zinc-600" title={phone.device_id}>
        {phone.model} · {phone.device_id}
      </div>
    </div>
  );
}
