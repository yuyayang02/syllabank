#!/usr/bin/env -S uv run --script
# SPDX-License-Identifier: AGPL-3.0-or-later
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Mandarin Animalese from a local syllable bank.

The speech-unit, phrasing, scheduling, and pitch-curve design is adapted from
Coopanion's src/speech and web/speech pipeline (AGPL-3.0-or-later):
https://github.com/Pal-AI-Lab/Coopanion
"""

from __future__ import annotations

import argparse
import array
import json
import math
import re
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Sequence


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets" / "speech"
BANK_OGG = ASSETS / "zh.ogg"

DEFAULT_PITCH = 365.0
DEFAULT_SPEED = 9.0
DEFAULT_BUBBLE_SPEED = 1.0
DEFAULT_SAMPLE_RATE = 48000
GAIN = 0.45
TONE_AMOUNT = 0.55

VOICE_GAP = {
    ",": 0.14, "，": 0.14, "、": 0.14, ";": 0.20, "；": 0.20, ":": 0.20, "：": 0.20,
    ".": 0.45, "。": 0.45, "!": 0.50, "！": 0.50, "?": 0.50, "？": 0.50,
    "…": 0.30, "~": 0.10, "～": 0.10,
}
TEXT_PAUSE = {
    ",": 5, "，": 5, "、": 5, ";": 6, "；": 6, ":": 6, "：": 6,
    ".": 9, "。": 9, "!": 9, "！": 9, "?": 9, "？": 9, "…": 4, "~": 3, "～": 3,
}
MARKS = re.compile(r"[,.!?;:…~，。！？；：、～]")
WEAK_HAN = set("的了吗呢啊呀吧哦啦么着过")
DIGITS = ["ling", "yi", "er", "san", "si", "wu", "liu", "qi", "ba", "jiu"]
TONES = {
    1: (0.8, 0.8),
    2: (-0.6, 1.6),
    3: (-1.0, -1.8, -0.6),
    4: (1.6, -1.2),
    5: (-0.8, -0.8),
}


@dataclass(frozen=True, slots=True)
class Unit:
    at: int
    key: str
    tone: int
    weak: bool
    pos: int
    of: int
    end: str


@dataclass(frozen=True, slots=True)
class Event:
    unit: Unit
    start: float
    length: float


def is_han(char: str) -> bool:
    return "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff"


def text_step(char: str) -> float:
    return TEXT_PAUSE.get(char, 1.0)


class Speech:
    """Reads the existing character table and recorded syllable bank."""

    def __init__(self, bank_dir: Path = ASSETS) -> None:
        bank_json = bank_dir / "bank.json"
        self.bank_ogg = bank_dir / "zh.ogg"
        payload = json.loads(bank_json.read_text(encoding="utf-8"))
        self.hz = float(payload["hz"])
        self.units = {
            key: (float(value[0]), float(value[1]))
            for key, value in payload["units"].items()
        }
        table = json.loads((bank_dir / "characters.json").read_text(encoding="utf-8"))
        self.han: dict[str, tuple[str, int]] = {}
        for reading, chars in table.items():
            for char in chars:
                self.han[char] = (reading[:-1], int(reading[-1]))

    def analyze(self, text: str) -> list[Unit]:
        items: list[dict[str, object]] = []
        for at, char in enumerate(text):
            if is_han(char):
                reading = self.han.get(char)
                if reading and reading[0] in self.units:
                    items.append({
                        "at": at,
                        "key": reading[0],
                        "tone": reading[1],
                        "weak": char in WEAK_HAN,
                    })
            elif char.isdigit():
                key = DIGITS[int(char)]
                if key in self.units:
                    items.append({"at": at, "key": key, "tone": 0, "weak": False})
        return self._phrases(text, items)

    @staticmethod
    def _phrases(text: str, items: list[dict[str, object]]) -> list[Unit]:
        out: list[Unit] = []
        start = 0
        for index in range(len(items) + 1):
            previous = int(items[index - 1]["at"]) if index else -1
            following = int(items[index]["at"]) if index < len(items) else len(text)
            between = text[previous + 1:following]
            if index < len(items) and not MARKS.search(between):
                continue
            if index == len(items):
                match = re.search(r"[.!?…~。！？～]+", text[previous + 1:])
                end = match.group(0) if match else "。"
            else:
                end = "".join(char for char in between if MARKS.match(char))
            for offset in range(start, index):
                item = items[offset]
                out.append(Unit(
                    at=int(item["at"]),
                    key=str(item["key"]),
                    tone=int(item["tone"]),
                    weak=bool(item["weak"]),
                    pos=offset - start,
                    of=index - start,
                    end=end,
                ))
            start = index
        return out


def schedule(
    text: str,
    units: Sequence[Unit],
    bank_units: dict[str, tuple[float, float]],
    *,
    pitch: float,
    speed: float,
    bubble_speed: float,
    bank_hz: float,
) -> list[Event]:
    now = 0.0
    reveal: list[float] = []
    for char in text:
        now += text_step(char) / (20.0 * bubble_speed)
        reveal.append(now)

    by_at: dict[int, list[Unit]] = {}
    for unit in units:
        by_at.setdefault(unit.at, []).append(unit)

    last = -math.inf
    voice_end = 0.0
    free = 0.0
    gap = 0.0
    rate = pitch / bank_hz
    events: list[Event] = []
    for index, char in enumerate(text):
        at = reveal[index] + 0.02
        if char in VOICE_GAP:
            gap += VOICE_GAP[char]
            free = voice_end + gap
        if voice_end < at - 2:
            last, voice_end, free, gap = -math.inf, at, at, 0.0
        for unit in by_at.get(index, ()):
            length = bank_units[unit.key][1] / rate
            start = max(at, last + 1 / speed, free)
            lag = start - at
            final = unit.pos >= unit.of - 2
            if not final and (lag > 2 or (lag > 1 and unit.weak)):
                continue
            last = start
            voice_end = max(voice_end, start + length)
            gap = 0.0
            events.append(Event(unit, start, length))
    return events


def tone_at(tone: Sequence[float], progress: float) -> float:
    if len(tone) == 1:
        return tone[0]
    place = progress * (len(tone) - 1)
    left = min(int(place), len(tone) - 1)
    right = min(left + 1, len(tone) - 1)
    fraction = place - left
    return tone[left] + (tone[right] - tone[left]) * fraction


def pitch_shape(unit: Unit, jitter: float) -> tuple[float, float, Sequence[float], float]:
    question = bool(re.search(r"[?？]", unit.end))
    exclamation = bool(re.search(r"[!！]", unit.end))
    last = unit.pos == unit.of - 1
    semitone = (0.8 if unit.pos == 0 else 0.0) - 0.06 * unit.pos
    semitone += (jitter - 0.5) * 0.2
    semitone += 1.0 if exclamation else 0.0
    bend = 0.0
    if question and unit.pos == unit.of - 2:
        semitone += 1.5
    if question and last:
        semitone += 2.0
        bend = 3.0
    if "…" in unit.end and last:
        semitone -= 0.8
    gain = 1.26 if exclamation else 1.0
    return semitone, bend, TONES.get(unit.tone, (0.0, 0.0)), gain


def decode_bank(bank_ogg: Path, sample_rate: int) -> array.array:
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(bank_ogg),
            "-f", "f32le", "-ac", "1", "-ar", str(sample_rate), "pipe:1",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"ffmpeg could not decode {bank_ogg}: {detail}")
    samples = array.array("f")
    samples.frombytes(result.stdout)
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        raise RuntimeError("decoded speech bank is empty")
    return samples


def render(
    events: Sequence[Event],
    output: str | Path | BinaryIO,
    bank_units: dict[str, tuple[float, float]],
    bank_hz: float,
    pitch: float,
    sample_rate: int,
    bank_ogg: Path = BANK_OGG,
) -> float:
    if not events:
        raise ValueError("no scheduled speech units")
    if isinstance(output, (str, Path)):
        target = Path(output).resolve()
        if target != ROOT and ROOT not in target.parents:
            raise ValueError("output path must stay inside the project root")

    source = decode_bank(bank_ogg, sample_rate)
    duration = max(event.start + event.length for event in events) + 0.20
    mixed = [0.0] * max(1, math.ceil(duration * sample_rate))
    state = 1
    base_rate = pitch / bank_hz
    for event in events:
        state = (state * 16807) % 2147483647
        semitone, bend, tone, gain = pitch_shape(event.unit, state / 2147483647)
        source_position = bank_units[event.unit.key][0] * sample_rate
        start_sample = round(event.start * sample_rate)
        count = max(1, round(event.length * sample_rate))
        for sample in range(count):
            progress = sample / max(1, count - 1)
            rate = base_rate * 2 ** (
                (semitone + tone_at(tone, progress) * TONE_AMOUNT + bend * progress) / 12
            )
            left = int(source_position)
            if left + 1 >= len(source):
                break
            fraction = source_position - left
            value = source[left] * (1 - fraction) + source[left + 1] * fraction
            target = start_sample + sample
            if target >= len(mixed):
                break
            mixed[target] += value * GAIN * gain
            source_position += rate

    peak = max(abs(value) for value in mixed)
    scale = 0.90 / peak if peak else 0.0
    pcm = array.array("h", (
        max(-32767, min(32767, round(value * scale * 32767)))
        for value in mixed
    ))
    if sys.byteorder == "big":
        pcm.byteswap()
    target_output: str | BinaryIO = str(output) if isinstance(output, Path) else output
    with wave.open(target_output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(pcm.tobytes())
    return duration


def self_test() -> None:
    speech = Speech()
    phrase = speech.analyze("今天好的,你好吗?")
    assert [unit.key for unit in phrase] == [
        "jin", "tian", "hao", "de", "ni", "hao", "ma",
    ]
    assert next(unit for unit in phrase if unit.key == "de").weak
    events = schedule(
        "你好，今天过得怎么样？",
        speech.analyze("你好，今天过得怎么样？"),
        speech.units,
        pitch=DEFAULT_PITCH,
        speed=DEFAULT_SPEED,
        bubble_speed=DEFAULT_BUBBLE_SPEED,
        bank_hz=speech.hz,
    )
    output = __import__("io").BytesIO()
    seconds = render(
        events, output, speech.units, speech.hz, DEFAULT_PITCH, DEFAULT_SAMPLE_RATE,
    )
    assert seconds > 0.2 and output.getvalue().startswith(b"RIFF")
    print(f"self-test passed: {len(events)} units, {seconds:.2f}s WAV")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Mandarin Animalese WAV from the original speech bank.")
    parser.add_argument("text", nargs="?", help="text to speak; when omitted, read UTF-8 text from stdin")
    parser.add_argument("--pitch", type=float, default=DEFAULT_PITCH, help=f"pitch in Hz, default: {DEFAULT_PITCH:g}")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED, help=f"maximum units per second, default: {DEFAULT_SPEED:g}")
    parser.add_argument("--bubble-speed", type=float, default=DEFAULT_BUBBLE_SPEED, help=f"text speed, default: {DEFAULT_BUBBLE_SPEED:g}")
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE, help=f"output sample rate, default: {DEFAULT_SAMPLE_RATE}")
    parser.add_argument("--bank-dir", type=Path, default=ASSETS, help="bank directory, default: assets/speech")
    parser.add_argument("--out", type=Path, default=Path("animalese.wav"), help="output WAV, default: animalese.wav")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return
    text = (args.text if args.text is not None else sys.stdin.read()).strip()
    if not text:
        parser.error("no text")
    if min(args.pitch, args.speed, args.bubble_speed, args.sample_rate) <= 0:
        parser.error("pitch, speed, bubble speed and sample rate must be positive")

    bank_dir = args.bank_dir.resolve()
    if bank_dir != ROOT and ROOT not in bank_dir.parents:
        parser.error("bank directory must stay inside the project root")
    if not (bank_dir / "zh.ogg").exists() or not (bank_dir / "bank.json").exists():
        parser.error(f"incomplete bank directory: {bank_dir}")
    speech = Speech(bank_dir)
    units = speech.analyze(text)
    if not units:
        parser.error("no speakable characters found")
    events = schedule(
        text,
        units,
        speech.units,
        pitch=args.pitch,
        speed=args.speed,
        bubble_speed=args.bubble_speed,
        bank_hz=speech.hz,
    )
    try:
        seconds = render(
            events,
            args.out,
            speech.units,
            speech.hz,
            args.pitch,
            args.sample_rate,
            speech.bank_ogg,
        )
    except (ValueError, FileNotFoundError, RuntimeError) as error:
        parser.error(str(error))
    print(
        f"wrote {args.out}: {len(events)} units, {seconds:.2f}s, "
        f"{args.pitch:g} Hz, {args.speed:g} units/s"
    )


if __name__ == "__main__":
    main()
