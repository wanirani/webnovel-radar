# -*- coding: utf-8 -*-
"""테스트용 합성 데이터 생성기(실서비스 미사용). DRY_RUN 리포트 검증 목적."""
import sys, os, random, math
from datetime import timedelta
sys.path.insert(0, os.path.dirname(__file__))
from common import (now_kst, append_jsonl, save_json, DATA_DIR, STATE_DIR,
                    classify_topic, classify_yt_format, match_watch_titles, load_config)

random.seed(7)
cfg = load_config()
now = now_kst()

YT_TITLES = [
    ("요즘 판타지 웹소설 미쳤다.. 인생작 TOP5 추천", "북튜브연구소", 182000, 512, "롱폼"),
    ("회귀물 명대사 모음 1분 #shorts", "소설한입", 45000, 38, "쇼츠"),
    ("문피아 신작 이거 안 보면 손해 (헌터물)", "장르소설TV", 91000, 431, "롱폼"),
    ("전지적 독자 시점 세계관 10분 정리", "설정덕후", 260000, 640, "롱폼"),
    ("웹소설 작가의 하루 브이로그 | 연재 후기", "글쓰는밤", 33000, 780, "롱폼"),
    ("이 대사에서 소름 돋았다 #웹소설 #shorts", "명장면저장소", 51000, 42, "쇼츠"),
    ("나 혼자만 레벨업 다시 읽는 이유 (리뷰)", "북튜브연구소", 182000, 495, "롱폼"),
    ("노벨피아 숨은 명작 추천 3선", "장르소설TV", 91000, 388, "롱폼"),
    ("헌터물 입문자를 위한 가이드 #shorts", "소설한입", 45000, 55, "쇼츠"),
    ("화산귀환 명장면 티저 컷 모음", "명장면저장소", 51000, 210, "숏폼"),
    ("웹소설 예고편 이렇게 만듭니다 (제작기)", "글쓰는밤", 33000, 620, "롱폼"),
    ("검술명가 막내아들 캐릭터 완전 정리", "설정덕후", 260000, 705, "롱폼"),
    ("문피아 유료화 이후 달라진 점", "웹소설경제", 74000, 540, "롱폼"),
    ("독자들이 뽑은 최고의 회귀물 순위 #shorts", "소설한입", 45000, 48, "쇼츠"),
]

# --- 유튜브: 발굴 + 시간별 통계 (3일치)
tracked = {}
disc_rows_by_day = {}
stats_rows_by_day = {}
peak = {9: .6, 12: .9, 13: .8, 18: 1.2, 19: 1.5, 20: 1.6, 21: 1.7, 22: 1.4, 23: 1.0}

for i, (title, ch, subs, dur, _f) in enumerate(YT_TITLES):
    age_h = random.randint(8, 66)
    pub = now - timedelta(hours=age_h, minutes=random.randint(0, 59))
    pub = pub.replace(hour=random.choice([11, 12, 17, 18, 18, 19, 19, 20, 21, 22]))
    vid = f"vid{i:03d}xyz"
    topic, topics = classify_topic(title)
    rec = {
        "video_id": vid, "title": title, "channel_id": f"ch{i}", "channel": ch,
        "published_at": pub.isoformat(), "published_kst": pub.isoformat(),
        "pub_hour_kst": pub.hour, "pub_weekday_kst": pub.weekday(),
        "duration_s": dur, "format": classify_yt_format(dur, title, ""),
        "topic": topic, "topics": topics, "subs": subs,
        "first_seen": pub.isoformat(), "first_views": 0,
        "watch_hits": match_watch_titles(title, cfg["watch_titles"]),
    }
    tracked[vid] = rec
    dkey = pub.strftime("%Y%m%d")
    disc_rows_by_day.setdefault(dkey, []).append(dict(rec, ts=pub.isoformat()))

    base = random.uniform(400, 2500) * (1 + subs / 120000)
    views = random.randint(500, 3000)
    t = pub + timedelta(hours=1)
    while t <= now:
        w = peak.get(t.hour, .35) * base * math.exp(-((t - pub).total_seconds() / 3600) / 40)
        views += int(max(0, random.gauss(w, w * .25)))
        stats_rows_by_day.setdefault(t.strftime("%Y%m%d"), []).append(
            {"ts": t.isoformat(timespec="minutes"), "video_id": vid, "views": views,
             "likes": int(views * .04), "comments": int(views * .004)})
        t += timedelta(hours=1)

for d, rows in disc_rows_by_day.items():
    append_jsonl(DATA_DIR / d / "yt_discovered.jsonl", rows)
for d, rows in stats_rows_by_day.items():
    append_jsonl(DATA_DIR / d / "yt_stats.jsonl", rows)
save_json(STATE_DIR / "yt_tracked.json", tracked)

# --- 블로그: 3일 × 시간별 스냅샷 축약(하루 3회 스냅샷으로 근사)
BLOGS = [
    ("판타지 웹소설 추천 BEST 7 — 정주행 보장", "책벌레의서재", 14, 2340, "카드"),
    ("회귀물 입문자를 위한 완벽 가이드", "장르소설연구소", 6, 1890, "글"),
    ("문피아 이번 주 신작 훑어보기", "웹소설일지", 3, 640, "글"),
    ("나 혼자만 레벨업, 지금 읽어도 재밌을까 (리뷰)", "밤의독서가", 9, 1420, "혼합"),
    ("헌터물 세계관 설정 총정리", "설정노트", 11, 980, "카드"),
    ("웹소설 작가 지망생의 연재 후기 3개월차", "글쓰는직장인", 2, 510, "글"),
    ("전지적 독자 시점 명대사 모음집", "문장수집가", 12, 3100, "카드"),
    ("화산귀환 완독 후기 — 이래서 1위구나", "밤의독서가", 9, 1650, "혼합"),
    ("검술명가 막내아들 캐릭터 관계도", "설정노트", 11, 870, "카드"),
    ("웹소설 후기: 이번 달 읽은 12작품 총평", "책벌레의서재", 14, 2100, "글"),
]
registry, details = {}, {}
blog_rows_by_day = {}
for day_off in range(3):
    d = now - timedelta(days=day_off)
    dkey = d.strftime("%Y%m%d")
    for snap_h in (8, 14, 21):
        ts = d.replace(hour=snap_h, minute=7).isoformat(timespec="minutes")
        for kw in cfg["keywords"]["blog"][:4]:
            order = BLOGS[:]
            random.shuffle(order)
            for rank, (title, name, imgs, likes, _k) in enumerate(order[:7], 1):
                pd = (d - timedelta(days=random.choice([0, 0, 1, 1, 2]))).strftime("%Y%m%d")
                topic, topics = classify_topic(title)
                link = f"https://blog.naver.com/{name}/22{abs(hash(title)) % 10 ** 8}"
                blog_rows_by_day.setdefault(dkey, []).append({
                    "ts": ts, "keyword": kw, "sort": "sim", "rank": rank,
                    "title": title, "desc": title + " — 오늘의 장르소설 이야기",
                    "link": link, "blogger": name,
                    "blogger_link": f"blog.naver.com/{name}",
                    "postdate": pd, "topic": topic, "topics": topics,
                    "watch_hits": match_watch_titles(title, cfg["watch_titles"]),
                })
                reg = registry.setdefault(f"blog.naver.com/{name}",
                                          {"name": name, "best_rank": rank, "days": {}})
                reg["best_rank"] = min(reg["best_rank"], rank)
                reg["days"][ts[:10]] = reg["days"].get(ts[:10], 0) + 1
                details[link.split("/")[-1] + name] = {
                    "img_count": imgs, "text_len": 3000, "likes": likes,
                    "pub_hour": random.choice([10, 18, 19, 19, 20, 21, 22]),
                    "fetched": ts, "link": link}
for d, rows in blog_rows_by_day.items():
    append_jsonl(DATA_DIR / d / "blog_search.jsonl", rows)
save_json(STATE_DIR / "blog_registry.json", registry)
save_json(STATE_DIR / "blog_posts.json", details)
print("sample data ready")
