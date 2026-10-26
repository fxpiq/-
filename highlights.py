from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any


HIGHLIGHT_TERMS = (
    "注意",
    "重点",
    "重点来了",
    "家人们",
    "兄弟们",
    "姐妹们",
    "真的",
    "居然",
    "没想到",
    "原来",
    "结果",
    "原因",
    "为什么",
    "怎么",
    "关键",
    "记住",
    "千万",
    "一定",
    "马上",
    "现在",
    "最后",
    "第一",
    "第二",
    "免费",
    "福利",
    "优惠",
    "到手",
    "上链接",
    "三二一",
    "太值",
    "超值",
    "绝了",
    "离谱",
    "震惊",
    "有料",
    "干货",
    "看这里",
)


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class ClipCandidate:
    candidate_id: str
    start: float
    end: float
    score: float
    title: str
    reason: str
    transcript: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _score_text(text: str, duration: float, target_duration: float) -> float:
    text = _clean_text(text)
    chars = len(re.sub(r"\s", "", text))
    if duration <= 0 or chars == 0:
        return 0.0

    density = min(chars / duration / 4.5, 1.5) * 2.2
    keyword_hits = sum(text.count(term) for term in HIGHLIGHT_TERMS)
    keywords = min(keyword_hits, 6) * 1.15
    punch = min(text.count("！") + text.count("？") + text.count("!"), 4) * 0.45
    numbers = min(len(re.findall(r"\d+(?:\.\d+)?", text)), 3) * 0.22
    complete_sentence = 0.55 if text[-1:] in "。！？!?；;" else 0.0
    duration_fit = max(0.0, 1.25 - abs(duration - target_duration) / target_duration)
    repeated_noise = 0.4 if len(set(text[:80])) < 8 else 0.0
    return density + keywords + punch + numbers + complete_sentence + duration_fit - repeated_noise


def _candidate_title(text: str) -> str:
    cleaned = _clean_text(text)
    first_sentence = re.split(r"[。！？!?；;]", cleaned, maxsplit=1)[0].strip()
    title = first_sentence or cleaned or "精彩片段"
    return title[:28]


def build_candidates(
    segments: list[TranscriptSegment],
    min_duration: float,
    max_duration: float,
    limit: int = 30,
) -> list[ClipCandidate]:
    if not segments or max_duration <= 0 or min_duration <= 0:
        return []

    target_duration = min(max_duration, max(min_duration, (min_duration + max_duration) / 2))
    candidates: list[ClipCandidate] = []
    counter = 0

    for start_index in range(len(segments)):
        start = max(0.0, segments[start_index].start)
        texts: list[str] = []
        for end_index in range(start_index, len(segments)):
            current = segments[end_index]
            end = min(current.end, start + max_duration)
            if end <= start:
                continue
            if current.text:
                texts.append(current.text)
            duration = end - start
            if duration < min_duration:
                continue
            transcript = _clean_text("".join(texts))
            score = _score_text(transcript, duration, target_duration)
            counter += 1
            candidates.append(
                ClipCandidate(
                    candidate_id=f"c{counter:04d}",
                    start=round(start, 3),
                    end=round(end, 3),
                    score=round(score, 4),
                    title=_candidate_title(transcript),
                    reason="语速、关键词、情绪标点和时长匹配度较高",
                    transcript=transcript,
                )
            )
            if current.end >= start + max_duration:
                break

    candidates.sort(key=lambda item: item.score, reverse=True)

    selected: list[ClipCandidate] = []
    for candidate in candidates:
        overlap = False
        for chosen in selected:
            intersection = max(
                0.0,
                min(candidate.end, chosen.end) - max(candidate.start, chosen.start),
            )
            shortest = min(
                candidate.end - candidate.start, chosen.end - chosen.start
            )
            if shortest > 0 and intersection / shortest > 0.35:
                overlap = True
                break
        if not overlap:
            selected.append(candidate)
        if len(selected) >= limit:
            break

    return selected


def normalize_llm_selection(
    response: dict[str, Any],
    candidates: list[ClipCandidate],
    clip_count: int,
) -> list[ClipCandidate]:
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    chosen: list[ClipCandidate] = []
    seen: set[str] = set()

    for item in response.get("clips", []):
        candidate_id = str(item.get("candidate_id", ""))
        candidate = by_id.get(candidate_id)
        if candidate is None or candidate_id in seen:
            continue
        title = _clean_text(str(item.get("title", "")))[:32] or candidate.title
        reason = _clean_text(str(item.get("reason", "")))[:120] or candidate.reason
        chosen.append(
            ClipCandidate(
                candidate_id=candidate.candidate_id,
                start=candidate.start,
                end=candidate.end,
                score=candidate.score,
                title=title,
                reason=reason,
                transcript=candidate.transcript,
            )
        )
        seen.add(candidate_id)
        if len(chosen) >= clip_count:
            break

    if not chosen:
        return candidates[:clip_count]
    return chosen


def merge_transcript_window(
    segments: list[TranscriptSegment], start: float, end: float
) -> str:
    parts = [
        segment.text.strip()
        for segment in segments
        if segment.end > start and segment.start < end and segment.text.strip()
    ]
    return _clean_text("".join(parts))


def score_to_percent(score: float) -> int:
    # The raw score is open-ended; this is only a UI-friendly indicator.
    return max(1, min(99, int(round(50 + 12 * math.tanh(score / 8)))))

