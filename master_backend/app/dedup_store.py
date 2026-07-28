"""
Sequence-number based deduplication (CLAUDE.md §1 Idempotent Ingestion).
Keyed by (device_id, session_id, sequence_number).
Cleared when a session ends.
"""


class DedupStore:
    """Per-(device, session) seen-sequence bitset.

    A set of (device_id, session_id, seq) tuples cost ~200 bytes per packet; at 100 Hz ×
    5 devices × 1 h that is >300 MB RSS. A bytearray bitset costs 1 bit per sequence
    number (~45 KB per device-hour) and answers the same question (plan D10).
    """

    def __init__(self) -> None:
        self._bits: dict[tuple[str, str], bytearray] = {}
        self._count = 0

    def _bucket(self, device_id: str, session_id: str) -> bytearray:
        key = (device_id, session_id)
        b = self._bits.get(key)
        if b is None:
            b = bytearray(8192)          # 65,536 sequence numbers, grows as needed
            self._bits[key] = b
        return b

    def is_duplicate(self, device_id: str, session_id: str, seq: int) -> bool:
        if seq < 0:
            return False
        b = self._bucket(device_id, session_id)
        idx = seq >> 3
        if idx >= len(b):
            return False
        return bool(b[idx] & (1 << (seq & 7)))

    def add(self, device_id: str, session_id: str, seq: int) -> None:
        if seq < 0:
            return
        b = self._bucket(device_id, session_id)
        idx = seq >> 3
        if idx >= len(b):
            b.extend(bytearray(idx - len(b) + 8192))
        if not (b[idx] & (1 << (seq & 7))):
            self._count += 1
        b[idx] |= 1 << (seq & 7)

    def clear(self) -> None:
        self._bits.clear()
        self._count = 0

    @property
    def size(self) -> int:
        return self._count


dedup = DedupStore()
