import type { Phone } from '../../api/types';
import { EMPTY_PHONES } from '../../lib/empty';
import { fmtDuration } from '../../lib/format';
import { roleHex } from '../../lib/roleColors';
import { useNow } from '../../lib/useNow';
import { useSnapshot } from '../../state/store';

/**
 * Phone health strip while recording (SPEC §5.2): one chip per phone —
 * online, or offline for mm:ss with the standing guidance that the phone is
 * still buffering locally, plus the true sampling rate.
 */
export function PhoneStrip() {
  const phones = useSnapshot((s) => s.snapshot?.phones ?? EMPTY_PHONES);
  return (
    <section className="shrink-0 rounded-lg border border-zinc-800 bg-zinc-900/60 p-2">
      <h2 className="mb-1 text-xs font-black uppercase tracking-wider text-zinc-500">Phones</h2>
      <div className="flex flex-wrap gap-1.5" data-testid="phone-strip">
        {phones.map((p) => (
          <PhoneChip key={p.device_id} phone={p} />
        ))}
      </div>
    </section>
  );
}

function PhoneChip({ phone }: { phone: Phone }) {
  const now = useNow(1000);
  const offlineFor =
    !phone.online && phone.offline_since_ms ? fmtDuration(now - phone.offline_since_ms) : null;
  return (
    <div
      className={`rounded border px-2 py-1 text-xs ${
        phone.online ? 'border-zinc-700 bg-zinc-950' : 'border-red-500/60 bg-red-950/40'
      }`}
      data-testid={`phone-chip-${phone.role}`}
    >
      <div className="flex items-center gap-1.5 font-black">
        <span
          className={`inline-block h-2 w-2 rounded-full ${phone.online ? 'bg-green-400' : 'animate-pulse bg-red-500'}`}
          aria-hidden
        />
        <span style={{ color: roleHex(phone.role) }}>{phone.role}</span>
        {phone.online ? (
          <span className="tabular-nums font-semibold text-zinc-400">
            {phone.true_hz_avg > 0 ? `${phone.true_hz_avg.toFixed(1)} Hz` : '—'}
          </span>
        ) : (
          <span className="tabular-nums text-red-300">OFFLINE {offlineFor ?? ''}</span>
        )}
      </div>
      {!phone.online && (
        <p className="mt-0.5 max-w-56 text-[11px] font-semibold leading-tight text-amber-300">
          data is buffering on the phone — do not stop yet
        </p>
      )}
    </div>
  );
}
