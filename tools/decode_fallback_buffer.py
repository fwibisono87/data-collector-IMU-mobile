"""
Decodes an orphaned FallbackBufferManager quarantine file (orphan_*.bin) into a CSV
matching the backend's schema (connectivity_robustness_plan.md T19).

orphan_*.bin is length-delimited SensorPacket protobuf: [4-byte BE length][proto bytes],
repeated — the same format FallbackBufferManager writes while buffering offline.

NOTE: after T12 (the always-on phone-local CSV recorder), this file's packets already
exist, complete and analysis-ready, in the phone-local
`<session_id>_<role>.csv` under imu_sessions/. This decoder is forensics for the rare
case where a phone recorded before T12 was deployed, or the local CSV itself was lost —
not the primary recovery path.

Usage (from repo root):
    python tools/decode_fallback_buffer.py orphan_1690000000000_1.bin --device-role chest -o out.csv
"""
import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from master_backend.proto.sensor_packet import SensorPacket  # noqa: E402

_CSV_HEADER = (
    "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,"
    "gyro_x_degs,gyro_y_degs,gyro_z_degs,"
    "label_id,label_name,sequence_number,device_id\n"
)
_DEFAULT_LABEL_ID = 0
_DEFAULT_LABEL_NAME = "0"


def iter_packets(data: bytes):
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        (length,) = struct.unpack_from(">I", data, pos)
        pos += 4
        if pos + length > n:
            break  # truncated final entry — stop, do not emit a partial row
        yield SensorPacket.from_bytes(data[pos : pos + length])
        pos += length


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("orphan_file", help="path to orphan_*.bin")
    p.add_argument("--device-role", default="unknown", help="role to stamp in the metadata line (not in the proto itself)")
    p.add_argument("-o", "--output", default=None, help="output CSV path (default: <orphan_file>.csv)")
    args = p.parse_args()

    src = Path(args.orphan_file)
    if not src.exists():
        raise SystemExit(f"File not found: {src}")

    out_path = Path(args.output) if args.output else src.with_suffix(".csv")
    data = src.read_bytes()

    rows_written = 0
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(f"# source={src.name},role={args.device_role},labels unknown — see plan §7 merge order\n")
        f.write(_CSV_HEADER)
        for pkt in iter_packets(data):
            f.write(
                f"{pkt.timestamp_ms},"
                f"{pkt.acc_x:.6f},{pkt.acc_y:.6f},{pkt.acc_z:.6f},"
                f"{pkt.gyro_x:.6f},{pkt.gyro_y:.6f},{pkt.gyro_z:.6f},"
                f"{_DEFAULT_LABEL_ID},{_DEFAULT_LABEL_NAME},"
                f"{pkt.sequence_number},{pkt.device_id}\n"
            )
            rows_written += 1

    print(f"Decoded {rows_written} packet(s) from {src} -> {out_path}")


if __name__ == "__main__":
    main()
