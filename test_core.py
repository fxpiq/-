from pathlib import Path

from app.highlights import (
    ClipCandidate,
    TranscriptSegment,
    build_candidates,
    normalize_llm_selection,
)
from app.subtitles import build_ass, escape_filter_path
from app.utils import safe_title, validate_douyin_url


def sample_segments() -> list[TranscriptSegment]:
    return [
        TranscriptSegment(0, 5, "开场先和大家打个招呼。"),
        TranscriptSegment(5, 12, "重点来了，为什么这个方案真的省钱？"),
        TranscriptSegment(12, 20, "因为第一，材料少；第二，安装快！"),
        TranscriptSegment(20, 29, "最后记住这个价格，到手只要三位数。"),
        TranscriptSegment(29, 38, "喜欢的话现在点一下关注。"),
    ]


def test_build_candidates_respects_duration_and_non_overlap():
    candidates = build_candidates(
        sample_segments(), min_duration=10, max_duration=25, limit=5
    )
    assert candidates
    assert len(candidates) <= 5
    for candidate in candidates:
        assert 10 <= candidate.end - candidate.start <= 25
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            overlap = min(left.end, right.end) - max(left.start, right.start)
            shortest = min(
                left.end - left.start, right.end - right.start
            )
            assert overlap / shortest <= 0.35


def test_llm_selection_only_uses_known_candidates():
    candidates = [
        ClipCandidate("c1", 0, 20, 3, "A", "r", "a"),
        ClipCandidate("c2", 30, 50, 2, "B", "r", "b"),
    ]
    selected = normalize_llm_selection(
        {"clips": [{"candidate_id": "c2", "title": "B plus"}]},
        candidates,
        1,
    )
    assert selected[0].candidate_id == "c2"
    assert selected[0].title == "B plus"


def test_ass_output_contains_bounded_events(tmp_path: Path):
    output = build_ass(sample_segments(), 5, 29, tmp_path / "subtitle.ass")
    content = output.read_text(encoding="utf-8")
    assert "[Events]" in content
    assert "Dialogue:" in content
    assert "重点来了" in content
    assert "0:00:" in content


def test_url_and_title_validation():
    assert validate_douyin_url("https://live.douyin.com/123") == (
        "https://live.douyin.com/123"
    )
    assert validate_douyin_url("https://v.douyin.com/abc").startswith("https://")
    try:
        validate_douyin_url("https://example.com/live")
    except ValueError:
        pass
    else:
        raise AssertionError("non-Douyin host should be rejected")
    assert "/" not in safe_title("a/b:c")
    assert ":" not in safe_title("a/b:c")


def test_filter_path_escape():
    assert ":" not in escape_filter_path(Path("/tmp/a:b.ass")).replace(r"\:", "")

