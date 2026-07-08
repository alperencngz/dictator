"""Persistent, thread-safe history of dictated utterances.

Every batch utterance is appended to ``~/.dictate/history.jsonl`` (one JSON
object per line) and, when ``save_audio`` is on, its voice clip is written to
``~/.dictate/audio/<id>.wav`` so it can later be re-transcribed ("Regenerate").
Everything is on-device and local-only.

The store is shared across threads: the engine writes from a worker thread
while the HUD reads from the main thread, so every public method takes a lock.
``version`` is bumped on every mutation so the HUD's main-thread timer can
cheaply detect that the list changed without diffing the file.
"""

from __future__ import annotations

import json
import os
import threading
import wave
from datetime import datetime
from pathlib import Path

import numpy as np


class HistoryStore:
    def __init__(self, config_dir: Path | None = None, save_audio: bool = True,
                 keep: int = 100, shown: int = 20):
        self.dir = Path(config_dir) if config_dir else (Path.home() / ".dictate")
        self.jsonl = self.dir / "history.jsonl"
        self.audio_dir = self.dir / "audio"
        self.save_audio = save_audio
        self.keep = int(keep)
        self.shown = int(shown)
        self.version = 0
        self._lock = threading.Lock()
        self._counter = 0
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.save_audio:
            self.audio_dir.mkdir(parents=True, exist_ok=True)

    # --- file helpers (callers already hold the lock) ---
    def _read_all(self) -> list[dict]:
        if not self.jsonl.exists():
            return []
        entries: list[dict] = []
        try:
            with open(self.jsonl, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue  # skip a corrupt line rather than lose the file
        except Exception:
            return []
        return entries

    def _write_all(self, entries: list[dict]) -> None:
        tmp = self.jsonl.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, self.jsonl)

    def _write_wav(self, path: Path, audio, sample_rate: int) -> None:
        data = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
        ints = (data * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(ints.tobytes())

    def _find(self, entry_id: str) -> dict | None:
        for e in self._read_all():
            if e.get("id") == entry_id:
                return e
        return None

    def _delete_audio(self, entry: dict) -> None:
        af = entry.get("audio_file")
        if not af:
            return
        try:
            (self.audio_dir / af).unlink()
        except Exception:
            pass

    # --- public API (each locks) ---
    def add(self, text: str, *, audio=None, sample_rate: int = 16000,
            duration_s: float = 0.0, language: str | None = None,
            mode: str = "batch", whisper_tokens: int | None = None,
            app: str = "") -> dict:
        text = text or ""
        with self._lock:
            now = datetime.now()
            self._counter += 1
            entry_id = f"{now:%Y%m%d-%H%M%S-%f}-{self._counter}"
            audio_file = None
            if self.save_audio and audio is not None and len(audio) > 0:
                self.audio_dir.mkdir(parents=True, exist_ok=True)
                audio_file = f"{entry_id}.wav"
                try:
                    self._write_wav(self.audio_dir / audio_file, audio, sample_rate)
                except Exception:
                    audio_file = None  # keep the text entry even if the clip fails
            entry = {
                "id": entry_id,
                "ts": now.isoformat(),
                "text": text,
                "chars": len(text),
                "words": len(text.split()),
                "whisper_tokens": whisper_tokens,
                "duration_s": round(float(duration_s), 3),
                "language": language,
                "mode": mode,
                "app": app or "",
                "audio_file": audio_file,
            }
            entries = self._read_all()
            entries.append(entry)
            pruned: list[dict] = []
            if len(entries) > self.keep:
                cut = len(entries) - self.keep
                pruned = entries[:cut]
                entries = entries[cut:]
            self._write_all(entries)
            for p in pruned:
                self._delete_audio(p)
            self.version += 1
            return entry

    def recent(self, n: int | None = None) -> list[dict]:
        n = self.shown if n is None else n
        with self._lock:
            entries = self._read_all()
        entries.reverse()  # newest first
        return entries[:n]

    def totals(self) -> dict:
        with self._lock:
            entries = self._read_all()
        words = chars = tokens = 0
        duration = 0.0
        for e in entries:
            words += int(e.get("words") or 0)
            chars += int(e.get("chars") or 0)
            tokens += int(e.get("whisper_tokens") or 0)
            duration += float(e.get("duration_s") or 0.0)
        return {
            "words": words,
            "chars": chars,
            "tokens": tokens,
            "utterances": len(entries),
            "duration_s": round(duration, 3),
        }

    def stats(self) -> dict:
        """Dashboard headline metrics from one pass over the whole log.

        Returns ``{"words", "wpm", "streak", "dictations"}`` where:
          - ``words``      total words across every entry,
          - ``wpm``        words per minute of speech
                           (``words / (sum(duration_s) / 60)``; 0 if no audio),
          - ``streak``     consecutive calendar days ending today that each
                           have >= 1 entry (0 if today has none),
          - ``dictations`` number of entries.
        """
        with self._lock:
            entries = self._read_all()
        words = 0
        duration = 0.0
        days: set = set()
        for e in entries:
            words += int(e.get("words") or 0)
            duration += float(e.get("duration_s") or 0.0)
            ts = e.get("ts") or ""
            try:
                days.add(datetime.fromisoformat(ts).date())
            except Exception:
                pass
        minutes = duration / 60.0
        wpm = (words / minutes) if minutes > 0 else 0.0
        # Streak: walk back day-by-day from today while each day has an entry.
        from datetime import date as _date, timedelta as _timedelta
        streak = 0
        day = _date.today()
        while day in days:
            streak += 1
            day = day - _timedelta(days=1)
        return {
            "words": words,
            "wpm": round(wpm, 1),
            "streak": streak,
            "dictations": len(entries),
        }

    def audio_path(self, entry_or_id) -> Path | None:
        with self._lock:
            if isinstance(entry_or_id, dict):
                af = entry_or_id.get("audio_file")
            else:
                entry = self._find(entry_or_id)
                af = entry.get("audio_file") if entry else None
        if not af:
            return None
        return self.audio_dir / af

    def update_text(self, entry_id: str, new_text: str,
                    language: str | None = None) -> dict | None:
        new_text = new_text or ""
        with self._lock:
            entries = self._read_all()
            updated = None
            for e in entries:
                if e.get("id") == entry_id:
                    e["text"] = new_text
                    e["chars"] = len(new_text)
                    e["words"] = len(new_text.split())
                    if language is not None:
                        e["language"] = language
                    updated = e
                    break
            if updated is None:
                return None
            self._write_all(entries)
            self.version += 1
            return updated
