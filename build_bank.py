#!/usr/bin/env -S uv run --script
# SPDX-License-Identifier: AGPL-3.0-or-later
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "edge-tts>=6.1",
#   "numpy>=1.26",
#   "praat-parselmouth>=0.4",
#   "pypinyin>=0.55",
#   "scipy>=1.11",
# ]
# ///
"""Build a candidate syllable bank with an Edge neural voice.

The audio processing follows Coopanion's scripts/speech-bank/build.py
(AGPL-3.0-or-later): https://github.com/Pal-AI-Lab/Coopanion
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import parselmouth
from edge_tts import Communicate
from parselmouth.praat import call
from pypinyin import Style, pinyin
from scipy.signal import butter, lfilter, sosfilt

from animalese_bank import Speech, render, schedule


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets" / "speech"
DEFAULT_OUT = ROOT / "build" / "xiaoxiao"
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
SR = 48000
BANK_HZ = 364.0
UNIT_VOICED_S = 0.094
CONS_MAX_S = 0.020
VOICED_MAX_S = 0.160
PAD_S = 0.060
LOWPASS = butter(4, 4500, "low", fs=SR, output="sos")


@dataclass(frozen=True, slots=True)
class Carrier:
    syllable: str
    character: str
    tone: int


def gb_rank(character: str) -> int:
    try:
        encoded = character.encode("gb2312")
    except UnicodeEncodeError:
        return 2
    return 0 if encoded[0] < 0xD8 else 1


def choose_carriers(targets: list[str], character_file: Path) -> list[Carrier]:
    table = json.loads(character_file.read_text(encoding="utf-8"))
    groups: dict[str, list[tuple[str, int]]] = {}
    for reading, characters in table.items():
        syllable = reading[:-1]
        tone = int(reading[-1])
        groups.setdefault(syllable, []).extend((character, tone) for character in characters)

    chosen: list[Carrier] = []
    missing: list[str] = []
    for syllable in targets:
        candidates = groups.get(syllable, [])
        if not candidates:
            missing.append(syllable)
            continue

        def rank(item: tuple[str, int]) -> tuple[int, int, int, int, str]:
            character, tone = item
            readings = pinyin(character, style=Style.NORMAL, heteronym=True, errors="ignore")
            count = len(readings[0]) if readings and readings[0] else 1
            return count > 1, gb_rank(character), tone != 1, tone, character

        character, tone = min(candidates, key=rank)
        chosen.append(Carrier(syllable, character, tone))
    if missing:
        raise RuntimeError("no carrier character for syllables: " + ", ".join(missing))
    return chosen


async def record_one(
    carrier: Carrier,
    voice: str,
    cache_dir: Path,
    retries: int,
    timeout: float,
) -> Path:
    identity = f"{voice}|{carrier.syllable}|{carrier.character}".encode()
    path = cache_dir / (hashlib.sha256(identity).hexdigest() + ".mp3")
    if path.exists() and path.stat().st_size > 1000:
        return path
    error: Exception | None = None
    for attempt in range(retries):
        try:
            async def synthesize() -> bytes:
                data = bytearray()
                async for chunk in Communicate(carrier.character, voice).stream():
                    if chunk["type"] == "audio":
                        data.extend(chunk["data"])
                return bytes(data)

            data = await asyncio.wait_for(synthesize(), timeout=timeout)
            if len(data) < 1000:
                raise RuntimeError(f"TTS returned {len(data)} bytes")
            temporary = cache_dir / f".{path.name}.{os.getpid()}.tmp"
            temporary.write_bytes(data)
            os.replace(temporary, path)
            return path
        except Exception as caught:
            error = caught
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Edge TTS failed for {carrier.syllable}/{carrier.character}: {error}")


async def record_all(
    carriers: list[Carrier],
    voice: str,
    cache_dir: Path,
    concurrency: int,
    delay: float,
    retries: int,
    timeout: float,
) -> dict[str, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)
    finished = 0

    async def work(carrier: Carrier) -> tuple[str, Path]:
        nonlocal finished
        async with semaphore:
            path = await record_one(carrier, voice, cache_dir, retries, timeout)
            await asyncio.sleep(delay)
            finished += 1
            print(f"[{finished:3d}/{len(carriers)}] {carrier.syllable:6s} {carrier.character}", flush=True)
            return carrier.syllable, path

    results = await asyncio.gather(*(work(carrier) for carrier in carriers))
    return dict(results)


def decode(data: bytes) -> np.ndarray:
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", "pipe:0",
            "-f", "f32le", "-ac", "1", "-ar", str(SR), "pipe:1",
        ],
        input=data,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return np.frombuffer(result.stdout, np.float32).astype(np.float64)


def cut(samples: np.ndarray) -> np.ndarray:
    sound = parselmouth.Sound(samples, SR)
    pitch = sound.to_pitch_ac(time_step=0.005, pitch_floor=120, pitch_ceiling=700)
    frequency = pitch.selected_array["frequency"]
    times = pitch.xs()
    voiced = np.nonzero(frequency > 0)[0]
    loud = np.nonzero(np.abs(samples) > np.abs(samples).max() * 0.03)[0]
    if not len(loud):
        raise RuntimeError("recorded syllable is silent")
    first = loud[0] / SR
    if len(voiced):
        voice_start, voice_end = times[voiced[0]], times[voiced[-1]]
    else:
        voice_start, voice_end = first, loud[-1] / SR
    start = max(first, voice_start - CONS_MAX_S)
    end = min(voice_end, voice_start + VOICED_MAX_S)
    if end <= start:
        raise RuntimeError("recorded syllable is too short")
    return samples[int(start * SR):int(end * SR)]


def peak_filter(samples: np.ndarray, frequency: float, gain_db: float, q: float) -> np.ndarray:
    amplitude = 10 ** (gain_db / 40)
    omega = 2 * np.pi * frequency / SR
    alpha = np.sin(omega) / (2 * q)
    cosine = np.cos(omega)
    b = np.array([1 + alpha * amplitude, -2 * cosine, 1 - alpha * amplitude])
    a = np.array([1 + alpha / amplitude, -2 * cosine, 1 - alpha / amplitude])
    return lfilter(b / a[0], a / a[0], samples)


def make_unit(samples: np.ndarray) -> np.ndarray:
    samples = cut(samples)
    sound = parselmouth.Sound(samples, SR)
    duration = sound.duration
    manipulation = call(sound, "To Manipulation", 0.005, 100, 800)
    pitch = call("Create PitchTier", "p", 0, duration)
    call(pitch, "Add point", 0, BANK_HZ)
    call(pitch, "Add point", duration * 0.75, BANK_HZ)
    call(pitch, "Add point", duration, BANK_HZ * 2 ** (-0.7 / 12))
    call([pitch, manipulation], "Replace pitch tier")
    factor = min(1.0, UNIT_VOICED_S / max(duration - CONS_MAX_S, 0.02))
    duration_tier = call("Create DurationTier", "d", 0, duration)
    for at, value in (
        (0, 1.0), (CONS_MAX_S, 1.0), (CONS_MAX_S + 0.005, factor), (duration, factor),
    ):
        call(duration_tier, "Add point", at, value)
    call([duration_tier, manipulation], "Replace duration tier")
    output = call(manipulation, "Get resynthesis (overlap-add)").values[0]
    consonant = int(CONS_MAX_S * SR)
    if consonant:
        output[:consonant] *= 10 ** (-5 / 20)
    output = peak_filter(peak_filter(sosfilt(LOWPASS, output), 1050, -6, 0.9), 1900, 4, 1.2)
    voiced = output[consonant:]
    if not len(voiced) or not np.any(voiced):
        raise RuntimeError("processed syllable has no voiced audio")
    output = output / (np.sqrt(np.mean(voiced ** 2)) + 1e-9) * 0.1
    fade_in, fade_out = int(0.008 * SR), int(0.035 * SR)
    output[:fade_in] *= np.linspace(0, 1, fade_in) ** 2
    output[-fade_out:] *= np.cos(np.linspace(0, np.pi / 2, fade_out))
    return output


def build_audio(
    carriers: list[Carrier],
    recordings: dict[str, Path],
    output_dir: Path,
) -> tuple[dict[str, list[float]], float]:
    pad = np.zeros(int(PAD_S * SR))
    parts: list[np.ndarray] = [pad]
    units: dict[str, list[float]] = {}
    cursor = PAD_S
    for index, carrier in enumerate(carriers, 1):
        raw = decode(recordings[carrier.syllable].read_bytes())
        unit = make_unit(raw)
        units[carrier.syllable] = [round(cursor, 4), round(len(unit) / SR, 4)]
        parts.extend((unit, pad))
        cursor += len(unit) / SR + PAD_S
        if index % 25 == 0 or index == len(carriers):
            print(f"processed {index}/{len(carriers)}", flush=True)
    audio = np.concatenate(parts)
    audio = (audio / (np.abs(audio).max() + 1e-9) * 0.9).astype(np.float32)
    target = output_dir / "zh.ogg"
    temporary = output_dir / f".zh.{os.getpid()}.ogg"
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "48k", str(temporary),
        ],
        input=audio.tobytes(),
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    os.replace(temporary, target)
    return units, cursor


def validate(output_dir: Path, units: dict[str, list[float]]) -> None:
    decoded = decode((output_dir / "zh.ogg").read_bytes())
    duration = len(decoded) / SR
    for key, (offset, length) in units.items():
        if offset + length > duration + 0.01:
            raise RuntimeError(f"{key} points outside the OGG")
    print(f"validated {len(units)} units, {duration:.2f}s OGG")


def write_test_wav(output_dir: Path) -> None:
    speech = Speech(output_dir)
    preferred = "你好，今天过得怎么样？"
    expected = {unit.key for unit in Speech(ASSETS).analyze(preferred)}
    if expected.issubset(speech.units):
        text = preferred
    else:
        report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
        text = "".join(item["character"] for item in report["carriers"][:3])
    units = speech.analyze(text)
    if not units:
        print("test WAV skipped: candidate bank has no usable test syllable")
        return
    events = schedule(
        text,
        units,
        speech.units,
        pitch=365,
        speed=9,
        bubble_speed=1,
        bank_hz=speech.hz,
    )
    seconds = render(
        events,
        output_dir / "test.wav",
        speech.units,
        speech.hz,
        365,
        48000,
        speech.bank_ogg,
    )
    print(f"test WAV: {output_dir / 'test.wav'} ({seconds:.2f}s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Mandarin syllable bank with Edge TTS.")
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=0, help="build only the first N target syllables")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--cache-dir", type=Path, default=None)
    args = parser.parse_args()

    output_dir = args.out_dir.resolve()
    if output_dir == ASSETS or ASSETS in output_dir.parents:
        parser.error("candidate output must not modify assets/speech")
    if output_dir != ROOT and ROOT not in output_dir.parents:
        parser.error("output must stay inside the project root")
    if args.concurrency <= 0 or args.delay < 0 or args.retries <= 0 or args.timeout <= 0 or args.limit < 0:
        parser.error("concurrency, retries and timeout must be positive; delay and limit must not be negative")
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = (args.cache_dir or ROOT / "build" / "cache" / args.voice).resolve()
    if cache_dir != ROOT and ROOT not in cache_dir.parents:
        parser.error("cache directory must stay inside the project root")

    template = json.loads((ASSETS / "bank.json").read_text(encoding="utf-8"))
    targets = list(template["units"])
    if args.limit:
        targets = targets[:args.limit]
    carriers = choose_carriers(targets, ASSETS / "characters.json")
    print(f"voice={args.voice} syllables={len(carriers)} output={output_dir}", flush=True)
    started = time.time()
    recordings = asyncio.run(record_all(
        carriers,
        args.voice,
        cache_dir,
        args.concurrency,
        args.delay,
        args.retries,
        args.timeout,
    ))
    units, duration = build_audio(carriers, recordings, output_dir)
    (output_dir / "bank.json").write_text(
        json.dumps({"hz": BANK_HZ, "units": units}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (output_dir / "characters.json").write_text(
        (ASSETS / "characters.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    validate(output_dir, units)
    report = {
        "voice": args.voice,
        "syllables": len(units),
        "duration": round(duration, 3),
        "elapsed": round(time.time() - started, 1),
        "sha256": hashlib.sha256((output_dir / "zh.ogg").read_bytes()).hexdigest(),
        "carriers": [asdict(carrier) for carrier in carriers],
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_test_wav(output_dir)
    print(f"candidate bank ready: {output_dir}")


if __name__ == "__main__":
    main()
