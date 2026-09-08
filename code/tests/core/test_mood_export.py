"""本端情绪汇总测试（mood-sync impl §3.2）。"""

from __future__ import annotations

from core.mood_config import MOOD_LABELS
from core.mood_export import Emotion, MoodExporter


def test_initial_empty():
    ex = MoodExporter()
    assert ex.current() is None


def test_overwrite_update():
    ex = MoodExporter()
    assert ex.update(Emotion(0.8, 0.6, "happy")) is True
    assert ex.update(Emotion(0.2, 0.1, "sleepy")) is True
    assert ex.current() == Emotion(0.2, 0.1, "sleepy")  # 新值覆盖旧值


def test_invalid_label_rejected():
    ex = MoodExporter()
    ex.update(Emotion(0.8, 0.6, "happy"))
    assert ex.update(Emotion(0.8, 0.6, "love")) is False  # 非法 label
    assert ex.current().label == "happy"  # current 不变


def test_out_of_range_rejected():
    ex = MoodExporter()
    ex.update(Emotion(0.8, 0.6, "happy"))
    # valence 越界
    assert ex.update(Emotion(1.5, 0.6, "happy")) is False
    # arousal 越界
    assert ex.update(Emotion(0.5, 1.5, "happy")) is False
    # 负 valence / 负 arousal
    assert ex.update(Emotion(-1.1, 0.6, "happy")) is False
    assert ex.update(Emotion(0.5, -0.1, "happy")) is False
    # 非有限数
    assert ex.update(Emotion(float("nan"), 0.6, "happy")) is False
    assert ex.current().label == "happy"  # current 不变


def test_boundary_values_accepted():
    ex = MoodExporter()
    assert ex.update(Emotion(-1.0, 0.0, "sad")) is True
    assert ex.update(Emotion(1.0, 1.0, "excited")) is True


def test_all_labels_roundtrip():
    ex = MoodExporter()
    for i, label in enumerate(MOOD_LABELS):
        assert ex.update(Emotion(-0.5 + i * 0.1, 0.5, label)) is True
        assert ex.current().label == label


def test_no_persistence(tmp_path, monkeypatch):
    # 纯内存：全程操作后工作目录零文件（无 DB/文件写入）
    monkeypatch.chdir(tmp_path)
    ex = MoodExporter()
    ex.update(Emotion(0.8, 0.6, "happy"))
    ex.update(Emotion(0.2, 0.1, "sleepy"))
    ex.update(Emotion(0.9, 0.9, "excited"))
    assert ex.current() is not None
    assert list(tmp_path.iterdir()) == []
