from app.douyin_live import parse_live_page


def test_parse_embedded_douyin_live_stream():
    page = r'''
    <script>
    self.__pace_f.push([1,"{\"data\":{\"stream_url\":{\"flv_pull_url\":{\"FULL_HD1\":\"http://pull.example/live.flv?x=1\\u0026y=2\"},\"hls_pull_url_map\":{\"FULL_HD1\":\"http://pull.example/live.m3u8?x=1\\u0026y=2\",\"HD1\":\"http://pull.example/hd.m3u8\"}},\"nickname\":\"测试主播\"}}"])
    </script>
    '''
    parsed = parse_live_page(page)
    assert parsed["protocol"] == "hls"
    assert parsed["quality"] == "FULL_HD1"
    assert parsed["url"] == "http://pull.example/live.m3u8?x=1&y=2"
    assert parsed["title"] == "测试主播"


def test_parse_flv_fallback():
    page = r'''
    {"stream_url":{"flv_pull_url":{"HD1":"https://pull.example/live.flv?a=1\\u0026b=2"}}}
    '''
    parsed = parse_live_page(page)
    assert parsed["protocol"] == "flv"
    assert parsed["url"] == "https://pull.example/live.flv?a=1&b=2"

