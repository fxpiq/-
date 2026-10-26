from __future__ import annotations

import html as html_lib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _netscape_cookie_header(cookie_file: Path) -> str:
    if not cookie_file.exists():
        return ""
    cookies: list[str] = []
    try:
        lines = cookie_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        domain, _, _, _, _, name, value = fields[:7]
        if "douyin.com" in domain:
            cookies.append(f"{name}={value}")
    return "; ".join(cookies)


def _extract_balanced_object(value: str, start: int) -> str | None:
    opening = value.find("{", start)
    if opening < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(opening, len(value)):
        char = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return value[opening : index + 1]
    return None


def _normalize_embedded_json(value: str) -> str:
    # The live page embeds JSON inside a JavaScript string, so quotes are escaped.
    return (
        value.replace(r"\\\"", '"')
        .replace(r"\"", '"')
        .replace(r"\/", "/")
    )


def _decode_embedded_string(value: str) -> str:
    value = html_lib.unescape(value)
    return re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        value,
    )


def parse_live_page(page: str) -> dict[str, Any]:
    normalized = _normalize_embedded_json(page)
    for marker in ('"stream_url":', '\\"stream_url\\":'):
        for match in re.finditer(re.escape(marker), normalized):
            candidate_text = _extract_balanced_object(normalized, match.end())
            if not candidate_text:
                continue
            try:
                stream_data = json.loads(candidate_text)
            except json.JSONDecodeError:
                continue

            hls_map = stream_data.get("hls_pull_url_map") or {}
            flv_map = stream_data.get("flv_pull_url") or {}
            for quality in ("FULL_HD1", "HD1", "SD1", "SD2"):
                if hls_map.get(quality):
                    return {
                        "url": _decode_embedded_string(hls_map[quality]),
                        "quality": quality,
                        "protocol": "hls",
                        "title": _extract_title(normalized),
                    }
            for quality in ("FULL_HD1", "HD1", "SD1", "SD2"):
                if flv_map.get(quality):
                    return {
                        "url": _decode_embedded_string(flv_map[quality]),
                        "quality": quality,
                        "protocol": "flv",
                        "title": _extract_title(normalized),
                    }
            hls_url = stream_data.get("hls_pull_url")
            if isinstance(hls_url, str) and hls_url.startswith(("http://", "https://")):
                return {
                    "url": _decode_embedded_string(hls_url),
                    "quality": "default",
                    "protocol": "hls",
                    "title": _extract_title(normalized),
                }
    raise RuntimeError(
        "抖音直播页未返回媒体流。可能尚未开播、直播已结束，或页面要求登录/人机验证。"
    )


def _extract_title(page: str) -> str:
    for pattern in (
        r'"nickname"\s*:\s*"([^"]{1,80})"',
        r'"title"\s*:\s*"([^"]{1,120})"',
        r"<title[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, page, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = re.sub(r"\s+", " ", html_lib.unescape(match.group(1))).strip()
            if title and not title.startswith("$"):
                return title
    return ""


def resolve_douyin_live(url: str, cookie_file: Path) -> dict[str, Any]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://live.douyin.com/",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    cookie_header = _netscape_cookie_header(cookie_file)
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            page = body.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 429}:
            raise RuntimeError(
                "抖音拒绝访问直播页，通常需要有效 cookies 或触发了风控验证。"
            ) from exc
        raise RuntimeError(f"读取抖音直播页失败：HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"连接抖音直播页失败：{exc}") from exc

    parsed = parse_live_page(page)
    parsed["headers"] = {
        "User-Agent": USER_AGENT,
        "Referer": url,
    }
    return parsed

