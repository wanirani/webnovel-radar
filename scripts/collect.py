# -*- coding: utf-8 -*-
"""매시 실행 수집기.

1) 네이버 블로그 검색 API — 키워드별 정확도순/최신순 상위 노출 스냅샷
2) 유튜브 — 추적 중 영상 통계 갱신(매시) + 신규 영상 발굴(하루 4회)
3) 네이버 블로그 본문 베스트에포트 수집 — 이미지 수/발행시각/공감 수(가능한 경우)

실패는 로그로 남기고 절대 예외로 종료하지 않는다(파이프라인 보호).
"""
import os
import re
import sys
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))
from common import (  # noqa: E402
    load_config, load_json, save_json, append_jsonl, log_line,
    now_kst, to_kst, parse_iso_duration, clean_text,
    classify_topic, classify_yt_format, match_watch_titles,
    load_thresholds, DATA_DIR, STATE_DIR,
)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) webnovel-radar/1.0"}


# ================================================================ 네이버 블로그
def naver_blog_search(query, sort, display, cid, csec):
    r = requests.get(
        "https://openapi.naver.com/v1/search/blog.json",
        params={"query": query, "display": display, "start": 1, "sort": sort},
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("items", [])


def collect_blog(cfg, day_dir, ts):
    cid = os.environ.get("NAVER_CLIENT_ID", "")
    csec = os.environ.get("NAVER_CLIENT_SECRET", "")
    if not cid or not csec:
        log_line("collect", "네이버 API 키 미설정 — 블로그 수집 건너뜀")
        return 0

    registry = load_json(STATE_DIR / "blog_registry.json", {})
    rows, n_err = [], 0
    for kw in cfg["keywords"]["blog"]:
        for sort in ("sim", "date"):
            try:
                items = naver_blog_search(kw, sort, cfg["blog"]["display"], cid, csec)
            except Exception as e:
                n_err += 1
                log_line("collect", f"블로그 검색 실패 [{kw}/{sort}]: {e}")
                continue
            for rank, it in enumerate(items, start=1):
                title = clean_text(it.get("title"))
                desc = clean_text(it.get("description"))
                topic, topics = classify_topic(f"{title} {desc}")
                row = {
                    "ts": ts, "keyword": kw, "sort": sort, "rank": rank,
                    "title": title, "desc": desc,
                    "link": it.get("link", ""),
                    "blogger": it.get("bloggername", ""),
                    "blogger_link": it.get("bloggerlink", ""),
                    "postdate": it.get("postdate", ""),
                    "topic": topic, "topics": topics,
                    "watch_hits": match_watch_titles(f"{title} {desc}", cfg["watch_titles"]),
                }
                rows.append(row)
                # 블로거 레지스트리(활동 빈도·최고 순위) 갱신 — 정확도순 기준
                bl = it.get("bloggerlink", "")
                if bl and sort == "sim":
                    reg = registry.setdefault(bl, {"name": it.get("bloggername", ""),
                                                   "best_rank": rank, "days": {}})
                    reg["name"] = it.get("bloggername", reg["name"])
                    reg["best_rank"] = min(reg.get("best_rank", 999), rank)
                    dkey = ts[:10]
                    reg["days"][dkey] = reg["days"].get(dkey, 0) + 1
            time.sleep(0.15)

    # 레지스트리 14일 초과분 정리
    cutoff = (now_kst() - timedelta(days=14)).strftime("%Y-%m-%d")
    for bl in list(registry.keys()):
        days = {d: c for d, c in registry[bl].get("days", {}).items() if d >= cutoff}
        if days:
            registry[bl]["days"] = days
        else:
            del registry[bl]

    append_jsonl(day_dir / "blog_search.jsonl", rows)
    save_json(STATE_DIR / "blog_registry.json", registry)
    log_line("collect", f"블로그 수집 {len(rows)}건 (오류 {n_err})")
    return len(rows)


# -------- 블로그 본문 베스트에포트 (이미지 수·발행시각·공감 수)
def _naver_post_ids(link):
    """blog.naver.com 링크 → (blogId, logNo) 또는 None."""
    try:
        u = urlparse(link)
        if "blog.naver.com" not in u.netloc:
            return None
        qs = parse_qs(u.query)
        if "blogId" in qs and "logNo" in qs:
            return qs["blogId"][0], qs["logNo"][0]
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 2 and parts[-1].isdigit():
            return parts[-2], parts[-1]
    except Exception:
        pass
    return None


PUB_RE = re.compile(r"(20\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{1,2}):(\d{2})")


def fetch_post_detail(blog_id, log_no):
    """PostView 페이지에서 이미지 수·본문 길이·발행시각 추출 + 공감 수 API 시도."""
    detail = {"img_count": None, "text_len": None, "pub_hour": None, "likes": None}
    try:
        r = requests.get(
            "https://blog.naver.com/PostView.naver",
            params={"blogId": blog_id, "logNo": log_no},
            headers=UA, timeout=12,
        )
        if r.status_code == 200:
            h = r.text
            main = h
            m = re.search(r'<div[^>]+class="[^"]*se-main-container[^"]*".*', h, re.S)
            if m:
                main = m.group(0)[:400000]
            detail["img_count"] = len(re.findall(r"<img[^>]+src=", main))
            detail["text_len"] = len(re.sub(r"<[^>]+>", "", main))
            pm = PUB_RE.search(h)
            if pm:
                detail["pub_hour"] = int(pm.group(4))
    except Exception:
        pass
    try:  # 공감 수 (비공식 엔드포인트 — 실패해도 무시)
        r = requests.get(
            "https://blog.like.naver.com/v1/search/contents",
            params={"suppress_response_codes": "true",
                    "q": f"BLOG[{blog_id}_{log_no}]"},
            headers=UA, timeout=8,
        )
        j = r.json()
        cl = j.get("contents", [{}])[0].get("reactions", [])
        detail["likes"] = sum(x.get("count", 0) for x in cl) if cl else 0
    except Exception:
        pass
    return detail


def collect_blog_details(cfg, ts):
    """오늘 새로 등장한 네이버 블로그 글 중 아직 미수집분을 상한 내에서 상세 수집."""
    seen = load_json(STATE_DIR / "blog_posts.json", {})
    day = now_kst().strftime("%Y%m%d")
    p = DATA_DIR / day / "blog_search.jsonl"
    if not p.exists():
        return 0
    links = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                links.append(row.get("link", ""))
            except Exception:
                pass
    done = 0
    limit = cfg["blog"].get("fetch_post_detail_max_per_run", 20)
    for link in dict.fromkeys(links):  # 순서 유지 중복 제거
        if done >= limit:
            break
        ids = _naver_post_ids(link)
        if not ids:
            continue
        key = f"{ids[0]}_{ids[1]}"
        if key in seen:
            continue
        d = fetch_post_detail(*ids)
        d["fetched"] = ts
        d["link"] = link
        seen[key] = d
        done += 1
        time.sleep(0.4)
    # 30일 초과 항목 정리(파일 비대화 방지)
    if len(seen) > 3000:
        keys = sorted(seen, key=lambda k: seen[k].get("fetched", ""))
        for k in keys[:-2000]:
            del seen[k]
    save_json(STATE_DIR / "blog_posts.json", seen)
    log_line("collect", f"블로그 상세 수집 {done}건")
    return done


# ================================================================ 유튜브
YT = "https://www.googleapis.com/youtube/v3"


def yt_get(endpoint, params, key):
    params = dict(params, key=key)
    r = requests.get(f"{YT}/{endpoint}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def collect_youtube(cfg, day_dir, ts, hour_kst):
    key = os.environ.get("YOUTUBE_API_KEY", "")
    if not key:
        log_line("collect", "유튜브 API 키 미설정 — 유튜브 수집 건너뜀")
        return
    th = load_thresholds(cfg)["youtube"]
    tracked = load_json(STATE_DIR / "yt_tracked.json", {})
    ycfg = cfg["youtube"]

    # --- (a) 신규 발굴: 지정 시각에만 (쿼터 절약)
    if hour_kst in ycfg["discovery_hours_kst"]:
        first = hour_kst == min(ycfg["discovery_hours_kst"])
        new_ids = set()
        for kw in cfg["keywords"]["youtube"]:
            orders = ["date"] + (["viewCount"] if first else [])
            for order in orders:
                hrs = (ycfg["weekly_top_lookback_days"] * 24
                       if order == "viewCount" else ycfg["published_within_hours"])
                after = (datetime.now(timezone.utc) - timedelta(hours=hrs)
                         ).strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    j = yt_get("search", {
                        "part": "snippet", "q": kw, "type": "video",
                        "order": order, "maxResults": ycfg["search_results_per_keyword"],
                        "publishedAfter": after,
                        "regionCode": ycfg.get("region_code", "KR"),
                        "relevanceLanguage": "ko",
                    }, key)
                    for it in j.get("items", []):
                        vid = it.get("id", {}).get("videoId")
                        if vid and vid not in tracked:
                            new_ids.add(vid)
                except Exception as e:
                    log_line("collect", f"유튜브 검색 실패 [{kw}/{order}]: {e}")
                time.sleep(0.1)

        # 상세 + 채널 구독자 → 임계값 필터
        new_ids = list(new_ids)
        added = []
        for i in range(0, len(new_ids), 50):
            chunk = new_ids[i:i + 50]
            try:
                j = yt_get("videos", {"part": "snippet,statistics,contentDetails",
                                      "id": ",".join(chunk)}, key)
            except Exception as e:
                log_line("collect", f"유튜브 상세 실패: {e}")
                continue
            ch_ids = {v["snippet"]["channelId"] for v in j.get("items", [])}
            subs = {}
            ch_ids = list(ch_ids)
            for c in range(0, len(ch_ids), 50):
                try:
                    cj = yt_get("channels", {"part": "statistics",
                                             "id": ",".join(ch_ids[c:c + 50])}, key)
                    for ch in cj.get("items", []):
                        subs[ch["id"]] = int(ch["statistics"].get("subscriberCount", 0) or 0)
                except Exception:
                    pass
            for v in j.get("items", []):
                sn, st = v["snippet"], v.get("statistics", {})
                views = int(st.get("viewCount", 0) or 0)
                sub = subs.get(sn["channelId"], 0)
                if views < th["min_views"] and sub < th["min_subscribers"]:
                    continue
                dur = parse_iso_duration(v.get("contentDetails", {}).get("duration"))
                title, desc = sn.get("title", ""), sn.get("description", "")[:300]
                topic, topics = classify_topic(f"{title} {desc}")
                pub_kst = to_kst(sn.get("publishedAt"))
                rec = {
                    "video_id": v["id"], "title": title,
                    "channel_id": sn["channelId"], "channel": sn.get("channelTitle", ""),
                    "published_at": sn.get("publishedAt"),
                    "published_kst": pub_kst.isoformat() if pub_kst else None,
                    "pub_hour_kst": pub_kst.hour if pub_kst else None,
                    "pub_weekday_kst": pub_kst.weekday() if pub_kst else None,
                    "duration_s": dur,
                    "format": classify_yt_format(dur, title, desc),
                    "topic": topic, "topics": topics,
                    "subs": sub, "first_seen": ts,
                    "first_views": views,
                    "watch_hits": match_watch_titles(f"{title} {desc}", cfg["watch_titles"]),
                }
                tracked[v["id"]] = rec
                added.append(dict(rec, ts=ts))
        if added:
            append_jsonl(day_dir / "yt_discovered.jsonl", added)
        log_line("collect", f"유튜브 발굴 {len(added)}건 (기준 조회수≥{th['min_views']:,} 또는 구독자≥{th['min_subscribers']:,})")

    # --- (b) 통계 갱신 (매시, videos.list는 50개당 1유닛으로 저렴)
    prune_before = now_kst() - timedelta(days=ycfg["track_days"])
    for vid in list(tracked.keys()):
        pk = to_kst(tracked[vid].get("published_at"))
        if pk and pk < prune_before:
            del tracked[vid]
    ids = list(tracked.keys())
    stat_rows = []
    for i in range(0, len(ids), 50):
        try:
            j = yt_get("videos", {"part": "statistics", "id": ",".join(ids[i:i + 50])}, key)
            for v in j.get("items", []):
                st = v.get("statistics", {})
                stat_rows.append({
                    "ts": ts, "video_id": v["id"],
                    "views": int(st.get("viewCount", 0) or 0),
                    "likes": int(st.get("likeCount", 0) or 0),
                    "comments": int(st.get("commentCount", 0) or 0),
                })
        except Exception as e:
            log_line("collect", f"유튜브 통계 실패: {e}")
    if stat_rows:
        append_jsonl(day_dir / "yt_stats.jsonl", stat_rows)
    save_json(STATE_DIR / "yt_tracked.json", tracked)
    log_line("collect", f"유튜브 통계 갱신 {len(stat_rows)}건 (추적 {len(tracked)}개)")


# ================================================================ main
def main():
    cfg = load_config()
    t = now_kst()
    ts = t.isoformat(timespec="minutes")
    day_dir = DATA_DIR / t.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    try:
        collect_blog(cfg, day_dir, ts)
    except Exception as e:
        log_line("collect", f"블로그 수집 단계 오류: {e}")
    try:
        collect_blog_details(cfg, ts)
    except Exception as e:
        log_line("collect", f"블로그 상세 단계 오류: {e}")
    try:
        collect_youtube(cfg, day_dir, ts, t.hour)
    except Exception as e:
        log_line("collect", f"유튜브 수집 단계 오류: {e}")
    print("collect done", ts)


if __name__ == "__main__":
    main()
