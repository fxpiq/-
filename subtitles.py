from __future__ import annotations

import math
import re
from pathlib import Path

from .highlights import TranscriptSegment


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, int(round(seconds * 100)))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _display_width(char: str) -> int:
    return 1 if ord(char) < 128 else 2


def _wrap_line(text: str, max_width: int = 30) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    lines: list[str] = []
    current = ""
    width = 0
    for char in text:
        char_width = _display_width(char)
        if current and width + char_width > max_width:
            split_at = max(
                current.rfind(mark) for mark in ("，", "。", "！", "？", "；", " ")
            )
            if split_at >= max(3, len(current) // 2):
                lines.append(current[: split_at + 1].strip())
                current = current[split_at + 1 :] + char
            else:
                lines.append(current.strip())
                current = char
            width = sum(_display_width(item) for item in current)
        else:
            current += char
            width += char_width
    if current.strip():
        lines.append(current.strip())
    return lines


def _escape_ass(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r", "")
        .replace("\n", r"\N")
    )


def build_ass(
    segments: list[TranscriptSegment],
    clip_start: float,
    clip_end: float,
    output_path: Path,
) -> Path:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK SC,62,&H00FFFFFF,&H000000FF,&H00101010,&H70000000,-1,0,0,0,100,100,1,0,1,4,1,2,100,100,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    for segment in segments:
        start = max(segment.start, clip_start)
        end = min(segment.end, clip_end)
        if end - start < 0.08 or not segment.text.strip():
            continue
        lines = _wrap_line(segment.text)
        if not lines:
            continue
        groups = [lines[index : index + 2] for index in range(0, len(lines), 2)]
        weights = [max(1, sum(len(line) for line in group)) for group in groups]
        total_weight = sum(weights)
        cursor = start
        relative_end = end - clip_start
        for index, group in enumerate(groups):
            duration = (end - start) * weights[index] / total_weight
            group_start = cursor - clip_start
            group_end = (
                relative_end
                if index == len(groups) - 1
                else min(relative_end, cursor + duration - clip_start)
            )
            text = r"\N".join(_escape_ass(line) for line in group)
            events.append(
                "Dialogue: 0,"
                f"{_ass_time(group_start)},{_ass_time(group_end)},Default,,0,0,0,,{text}"
            )
            cursor += duration

    output_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return output_path


def escape_filter_path(path: Path) -> str:
    # FFmpeg's filter parser treats these characters specially.
    return (
        str(path.resolve())
        .replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace(",", r"\,")
    )


def estimate_readability(segments: list[TranscriptSegment]) -> int:
    if not segments:
        return 0
    clean = "".join(segment.text for segment in segments)
    if not clean:
        return 0
    punctuation = len(re.findall(r"[，。！？；,.!?;]", clean))
    return max(1, min(99, int(60 + math.log2(len(clean) + 1) * 3 + punctuation)))

