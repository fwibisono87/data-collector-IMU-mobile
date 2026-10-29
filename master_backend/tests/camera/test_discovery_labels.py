"""L7a §3.1: v4l2 labels are the kernel card names, not the by-id strings.

The by-id path stays the ``device_key`` because it survives replugging; the label is what
the operator reads in the camera checklist, so it comes from
``/sys/class/video4linux/<videoN>/name`` ("Integrated Camera") and duplicate names get
`` (2)``, `` (3)`` in device_key order.
"""
from __future__ import annotations

from pathlib import Path

from master_backend.app.camera import discovery


def _fake_v4l2(tmp_path: Path, monkeypatch, *, names: dict[str, str],
               links: dict[str, str], dev_nodes: tuple[str, ...] = ()):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    for link_name, target in links.items():
        (by_id / link_name).symlink_to(target)
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    for node, card in names.items():
        (sysfs / node).mkdir()
        (sysfs / node / "name").write_text(card + "\n", encoding="utf-8")
    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    for node in dev_nodes:
        # A real character device: the fallback loop only considers those.
        (dev_root / node).symlink_to("/dev/zero")
    monkeypatch.setattr(discovery, "_BY_ID", by_id)
    monkeypatch.setattr(discovery, "_SYSFS_V4L", sysfs)
    monkeypatch.setattr(discovery, "_DEV_ROOT", dev_root)
    monkeypatch.setattr(discovery, "_v4l2_can_capture", lambda node: True)
    return by_id, dev_root


def test_labels_are_the_card_name_and_duplicates_are_numbered_in_key_order(tmp_path, monkeypatch):
    by_id, _dev_root = _fake_v4l2(
        tmp_path, monkeypatch,
        links={
            # Sorted by symlink name, so "cam-a" is first: the numbering follows
            # device_key, not the order the nodes were discovered in.
            "cam-a-video-index0": "/dev/null",
            "cam-b-video-index0": "/dev/zero",
        },
        names={"null": "Integrated Camera", "zero": "Integrated Camera"},
    )

    devices = discovery.v4l2_devices()

    assert [d.device_key for d in devices] == [
        str(by_id / "cam-a-video-index0"), str(by_id / "cam-b-video-index0"),
    ]
    assert [d.label for d in devices] == ["Integrated Camera", "Integrated Camera (2)"]
    assert all(d.backend == "v4l2" for d in devices)


def test_distinct_card_names_are_left_alone(tmp_path, monkeypatch):
    _fake_v4l2(
        tmp_path, monkeypatch,
        links={"usb-Cam_One-video-index0": "/dev/null",
               "usb-Cam_Two-video-index0": "/dev/zero"},
        names={"null": "Integrated Camera", "zero": "USB 2.0 Camera"},
    )
    assert [d.label for d in discovery.v4l2_devices()] == [
        "Integrated Camera", "USB 2.0 Camera",
    ]


def test_label_falls_back_to_the_by_id_name_without_sysfs(tmp_path, monkeypatch):
    _fake_v4l2(
        tmp_path, monkeypatch,
        links={"usb-Webcam_01-video-index0": "/dev/null"},
        names={},
    )
    devices = discovery.v4l2_devices()
    assert [d.label for d in devices] == ["Webcam 01"]
    assert devices[0].device_key.endswith("usb-Webcam_01-video-index0")


def test_plain_dev_nodes_use_the_card_name_too(tmp_path, monkeypatch):
    _by_id, dev_root = _fake_v4l2(
        tmp_path, monkeypatch,
        links={},
        names={"video3": "HD Pro Webcam C920"},
        dev_nodes=("video3",),
    )
    devices = discovery.v4l2_devices()
    assert [(d.device_key, d.label) for d in devices] == [
        (str(dev_root / "video3"), "HD Pro Webcam C920"),
    ]