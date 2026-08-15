"""TikHub Twitter 客户端单元测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bnb_quant_tool.tikhub_twitter import TikHubTwitterClient


def test_parse_twitter_time():
    ts = TikHubTwitterClient._parse_twitter_time("Sun Jun 14 15:01:13 +0000 2026")
    assert ts > 1700000000


def test_tweet_to_news_item():
    tw = {
        "tweet_id": "123",
        "text": "SEC files charges against Binance",
        "created_at": "Sun Jun 14 15:01:13 +0000 2026",
        "author": {"screen_name": "binance", "name": "Binance"},
    }
    item = TikHubTwitterClient._tweet_to_news_item(tw, "Twitter/@binance")
    assert item["platform"] == "twitter"
    assert "SEC" in item["title"]
    assert item["url"].endswith("/123")


def test_extract_tweets_pinned_and_timeline():
    data = {
        "pinned": {"text": "pinned tweet", "tweet_id": "1"},
        "timeline": [{"text": "timeline tweet", "tweet_id": "2"}],
    }
    tweets = TikHubTwitterClient._extract_tweets(data)
    assert len(tweets) == 2


def test_extract_rest_id():
    assert TikHubTwitterClient._extract_rest_id({"rest_id": 44196397}) == 44196397
    assert TikHubTwitterClient._extract_rest_id({"id": "902926941413453824"}) == 902926941413453824
    assert TikHubTwitterClient._extract_rest_id({}) is None


def test_rest_id_override_used_without_api(monkeypatch):
    client = TikHubTwitterClient(api_key="test-key", request_interval=0, max_retries=0)
    calls = []

    def fake_request(path, params, cache_key):
        calls.append((path, dict(params)))
        if path.endswith("fetch_user_post_tweet"):
            if "rest_id" in params:
                assert params["rest_id"] == 61417559
                return {"timeline": [{"text": "hello", "tweet_id": "9"}]}
            return None
        return None

    monkeypatch.setattr(client, "_request", fake_request)
    tweets, _ = client.fetch_user_posts("ErikVoorhees")
    assert len(tweets) == 1
    assert tweets[0]["text"] == "hello"
    assert any("rest_id" in p for _, p in calls)


def test_should_retry_http_on_transient_400():
    assert TikHubTwitterClient._should_retry_http(
        400, '{"message":"Request failed. Please retry."}',
    )
    assert not TikHubTwitterClient._should_retry_http(404, "not found")


def test_disabled_without_key():
    client = TikHubTwitterClient(api_key="")
    assert client.enabled is False
    assert client.collect_news_items(accounts=["binance"]) == []


def test_disk_cache_skips_api(tmp_path, monkeypatch):
    client = TikHubTwitterClient(
        api_key="test-key",
        cache_seconds=86400,
        cache_dir=str(tmp_path),
        request_interval=0,
        max_retries=0,
    )
    calls = {"n": 0}

    def fake_request(path, params, cache_key):
        calls["n"] += 1
        return {"timeline": [{"text": "cached tweet", "tweet_id": "1"}]}

    monkeypatch.setattr(client, "_request", fake_request)
    items1 = client.collect_news_items(accounts=["binance"])
    assert len(items1) == 1
    assert calls["n"] == 1

    client2 = TikHubTwitterClient(
        api_key="test-key",
        cache_seconds=86400,
        cache_dir=str(tmp_path),
        request_interval=0,
        max_retries=0,
    )
    monkeypatch.setattr(client2, "_request", fake_request)
    items2 = client2.collect_news_items(accounts=["binance"])
    assert len(items2) == 1
    assert calls["n"] == 1  # 重启后仍读磁盘，不再调 API


def test_clear_cache_removes_disk_files(tmp_path):
    client = TikHubTwitterClient(
        api_key="test-key",
        cache_seconds=86400,
        cache_dir=str(tmp_path),
    )
    client._set_cache("user:binance", {"timeline": []})
    assert list(tmp_path.glob("*.json"))
    client.clear_cache()
    assert not list(tmp_path.glob("*.json"))
