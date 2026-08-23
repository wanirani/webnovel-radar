# -*- coding: utf-8 -*-
"""일일 브리핑 생성·발송 (KST 07:10경 자동 실행).

- 최근 24시간/7일 수집 데이터를 플랫폼별로 분석
- 주제·형식·업로드 시간대·반응 속도·관심작품 언급 집계
- 도표(PNG) 생성 후 본문 삽입, HTML 리포트를 Gmail SMTP로 발송
- 인기 기준(임계값) 자동 조정 및 조정 내역 보고

DRY_RUN=1 이면 발송 대신 out/sample_report.html 로 저장(이미지 인라인).
"""
import os
import re
import sys
import csv
import io
import json
import base64
import smtplib
import statistics
import requests
from collections import Counter, defaultdict
from datetime import timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
from email.utils import formataddr
from email.header import Header

sys.path.insert(0, os.path.dirname(__file__))
from common import (  # noqa: E402
    load_config, load_json, save_json, read_jsonl_days, log_line,
    now_kst, to_kst, esc, TOPIC_ORDER, classify_blog_format,
    load_thresholds, save_thresholds, adjust_thresholds,
    STATE_DIR, OUT_DIR,
)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

INK = "#1F242E"
SEAL = "#B3261E"
GRAY = "#98A2B3"
WD = ["월", "화", "수", "목", "금", "토", "일"]


# ---------------------------------------------------------------- 폰트
def setup_korean_font():
    available = {f.name for f in font_manager.fontManager.ttflist}
    candidates = ["NanumGothic", "NanumBarunGothic", "Noto Sans CJK KR",
                  "Noto Sans KR", "Malgun Gothic", "AppleGothic"]
    pick = next((c for c in candidates if c in available), None)
    if not pick:  # 팬-CJK Noto 계열은 한글 글리프 포함 → 이름 무관 대체 허용
        pick = next((n for n in sorted(available)
                     if "Nanum" in n or "Noto Sans CJK" in n or "Noto Serif CJK" in n), None)
    if pick:
        plt.rcParams["font.family"] = pick
    plt.rcParams["axes.unicode_minus"] = False


# ---------------------------------------------------------------- 차트
def _style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color("#D0D5DD")
    ax.spines["bottom"].set_color("#D0D5DD")
    ax.tick_params(colors="#475467", labelsize=9)
    ax.yaxis.grid(True, color="#EAECF0", linewidth=0.8)
    ax.set_axisbelow(True)


def chart_barh(name, pairs, title, unit=""):
    """pairs: [(label, value)] 내림차순. 1위 막대만 인주색 강조."""
    if not pairs:
        return None
    labels = [p[0] for p in pairs][::-1]
    vals = [p[1] for p in pairs][::-1]
    fig, ax = plt.subplots(figsize=(6.6, max(1.6, 0.42 * len(pairs) + 0.8)), dpi=110)
    colors = [INK] * len(vals)
    colors[-1] = SEAL
    ax.barh(labels, vals, color=colors, height=0.6)
    for i, v in enumerate(vals):
        ax.text(v, i, f" {v:,.0f}{unit}", va="center", fontsize=9, color="#344054")
    ax.set_title(title, fontsize=11, color=INK, loc="left", pad=10)
    _style(ax)
    ax.xaxis.grid(True, color="#EAECF0", linewidth=0.8)
    ax.yaxis.grid(False)
    fig.tight_layout()
    p = OUT_DIR / f"{name}.png"
    fig.savefig(p, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    return p


def chart_hours(name, counts_by_hour, title, ylab, highlight_top=2):
    if not counts_by_hour or sum(counts_by_hour.values()) == 0:
        return None
    hours = list(range(24))
    vals = [counts_by_hour.get(h, 0) for h in hours]
    top = sorted(range(24), key=lambda h: vals[h], reverse=True)[:highlight_top]
    colors = [SEAL if h in top and vals[h] > 0 else INK for h in hours]
    fig, ax = plt.subplots(figsize=(6.8, 2.4), dpi=110)
    ax.bar(hours, vals, color=colors, width=0.72)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f"{h}시" for h in range(0, 24, 2)])
    ax.set_ylabel(ylab, fontsize=9, color="#475467")
    ax.set_title(title, fontsize=11, color=INK, loc="left", pad=10)
    _style(ax)
    fig.tight_layout()
    p = OUT_DIR / f"{name}.png"
    fig.savefig(p, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    return p


# ---------------------------------------------------------------- 유튜브 분석
def yt_analyze(cfg, th, now):
    disc = read_jsonl_days("yt_discovered", 8, now)
    stats = read_jsonl_days("yt_stats", 8, now)
    tracked = load_json(STATE_DIR / "yt_tracked.json", {})

    videos = {}
    for r in disc:
        videos[r["video_id"]] = r
    for vid, r in tracked.items():
        videos.setdefault(vid, r)

    sidx = defaultdict(list)
    for r in stats:
        t = to_kst(r["ts"])
        if t:
            sidx[r["video_id"]].append((t, r["views"], r.get("likes", 0), r.get("comments", 0)))
    for vid in sidx:
        sidx[vid].sort()

    def latest(vid):
        return sidx[vid][-1] if sidx.get(vid) else None

    def views_at(vid, target):
        arr = sidx.get(vid, [])
        best = None
        for t, v, *_ in arr:
            if best is None or abs((t - target).total_seconds()) < abs((best[0] - target).total_seconds()):
                best = (t, v)
        return best

    day_ago = now - timedelta(days=1)
    new24, week = [], []
    for vid, r in videos.items():
        pk = to_kst(r.get("published_kst") or r.get("published_at"))
        if not pk:
            continue
        cur = latest(vid)
        cur_views = cur[1] if cur else r.get("first_views", 0)
        cur_likes = cur[2] if cur else 0
        cur_cmts = cur[3] if cur else 0
        d24 = None
        base = views_at(vid, now - timedelta(hours=24))
        if cur and base and (cur[0] - base[0]).total_seconds() > 3600 * 6:
            d24 = cur[1] - base[1]
        item = dict(r, views=cur_views, likes=cur_likes, comments=cur_cmts,
                    delta24=d24, pub_dt=pk)
        if pk >= now - timedelta(days=7):
            week.append(item)
        if pk >= day_ago:
            new24.append(item)
    new24.sort(key=lambda x: x["views"], reverse=True)
    week_sorted = sorted(week, key=lambda x: x["views"], reverse=True)

    # 시간대별 조회수 증가 속도 (스냅샷 간 증분)
    vel_sum, vel_cnt = defaultdict(float), defaultdict(int)
    pairs = 0
    for vid, arr in sidx.items():
        pk = to_kst(videos.get(vid, {}).get("published_kst") or videos.get(vid, {}).get("published_at"))
        for (t1, v1, *_), (t2, v2, *_) in zip(arr, arr[1:]):
            dt = (t2 - t1).total_seconds() / 3600
            if not (0.5 <= dt <= 3.5):
                continue
            if pk and not (timedelta(hours=6) <= (t2 - pk) <= timedelta(days=7)):
                continue
            rate = max(0.0, (v2 - v1) / dt)
            mid_h = (t1 + (t2 - t1) / 2).hour
            vel_sum[mid_h] += rate
            vel_cnt[mid_h] += 1
            pairs += 1
    velocity = {h: (vel_sum[h] / vel_cnt[h]) for h in vel_cnt if vel_cnt[h] >= 3}

    upload_hours = Counter(x["pub_hour_kst"] for x in week if x.get("pub_hour_kst") is not None)
    weekdays = Counter(x["pub_weekday_kst"] for x in week if x.get("pub_weekday_kst") is not None)

    topic_cnt, topic_views = Counter(), defaultdict(list)
    fmt_cnt, fmt_views = Counter(), defaultdict(list)
    for x in week:
        topic_cnt[x.get("topic", "기타·일반")] += 1
        topic_views[x.get("topic", "기타·일반")].append(x["views"])
        fmt_cnt[x.get("format", "롱폼")] += 1
        fmt_views[x.get("format", "롱폼")].append(x["views"])

    watch = defaultdict(lambda: {"n": 0, "views": 0})
    for x in week:
        for w in x.get("watch_hits", []):
            watch[w]["n"] += 1
            watch[w]["views"] += x["views"]

    return {
        "new24": new24, "week": week_sorted, "pairs": pairs,
        "velocity": velocity, "upload_hours": upload_hours, "weekdays": weekdays,
        "topic_cnt": topic_cnt, "topic_views": topic_views,
        "fmt_cnt": fmt_cnt, "fmt_views": fmt_views, "watch": dict(watch),
        "tracked_n": len(tracked),
    }


def yt_top_comments(cfg, top_videos):
    key = os.environ.get("YOUTUBE_API_KEY", "")
    out = []
    if not key:
        return out
    for v in top_videos[: cfg["youtube"].get("top_comments_videos", 5)]:
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/commentThreads",
                params={"part": "snippet", "videoId": v["video_id"],
                        "order": "relevance", "maxResults": 2,
                        "textFormat": "plainText", "key": key},
                timeout=12,
            )
            for it in r.json().get("items", []):
                c = it["snippet"]["topLevelComment"]["snippet"]
                txt = re.sub(r"\s+", " ", c.get("textDisplay", ""))[:90]
                out.append({"video": v["title"][:28], "text": txt,
                            "likes": int(c.get("likeCount", 0) or 0)})
        except Exception:
            pass
    out.sort(key=lambda x: x["likes"], reverse=True)
    return out[:6]


# ---------------------------------------------------------------- 블로그 분석
def blog_analyze(cfg, th, now):
    rows = read_jsonl_days("blog_search", 8, now)
    registry = load_json(STATE_DIR / "blog_registry.json", {})
    details = load_json(STATE_DIR / "blog_posts.json", {})
    det_by_link = {d.get("link"): d for d in details.values() if d.get("link")}

    posts = {}
    for r in rows:
        link = r.get("link")
        if not link:
            continue
        p = posts.setdefault(link, dict(r, best_rank=999, kw_hits=set(), snaps=0))
        p["snaps"] += 1
        p["kw_hits"].add(r["keyword"])
        if r["sort"] == "sim":
            p["best_rank"] = min(p["best_rank"], r["rank"])
        p["title"], p["desc"] = r["title"], r["desc"]
        p["topic"], p["blogger"] = r["topic"], r["blogger"]
        p["postdate"], p["watch_hits"] = r.get("postdate", ""), r.get("watch_hits", [])
    plist = list(posts.values())
    for p in plist:
        p["kw_hits"] = sorted(p["kw_hits"])
        d = det_by_link.get(p["link"], {})
        p["img_count"], p["likes"] = d.get("img_count"), d.get("likes")
        p["pub_hour"] = d.get("pub_hour")
        p["fmt"] = classify_blog_format(d.get("img_count"), d.get("text_len"))

    today = now.strftime("%Y%m%d")
    yest = (now - timedelta(days=1)).strftime("%Y%m%d")
    new24 = sorted([p for p in plist if p.get("postdate") in (today, yest)
                    and p["best_rank"] <= th["blog"]["max_rank"] + 20],
                   key=lambda p: p["best_rank"])
    week = [p for p in plist if p.get("postdate", "") >= (now - timedelta(days=7)).strftime("%Y%m%d")]

    # 인기 블로거 풀 (7일 내 min_posts_7d회 이상 상위 노출)
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    pool = []
    for bl, reg in registry.items():
        days7 = {d: c for d, c in reg.get("days", {}).items() if d >= cutoff}
        exp = sum(days7.values())
        if len(days7) >= th["blog"]["min_posts_7d"] and reg.get("best_rank", 999) <= th["blog"]["max_rank"]:
            pool.append({"blogger": reg.get("name") or bl, "link": bl,
                         "days_active": len(days7), "exposures": exp,
                         "best_rank": reg.get("best_rank", 999)})
    pool.sort(key=lambda x: (-x["exposures"], x["best_rank"]))

    topic_cnt = Counter(p["topic"] for p in week)
    fmt_cnt = Counter(p["fmt"] for p in week if p.get("img_count") is not None)
    fmt_cover = sum(1 for p in week if p.get("img_count") is not None)
    pub_hours = Counter(p["pub_hour"] for p in week if p.get("pub_hour") is not None)
    likes_vals = [p["likes"] for p in week if isinstance(p.get("likes"), int)]

    watch = defaultdict(int)
    for p in week:
        for w in p.get("watch_hits", []):
            watch[w] += 1

    return {"new24": new24, "week": week, "pool": pool,
            "topic_cnt": topic_cnt, "fmt_cnt": fmt_cnt, "fmt_cover": fmt_cover,
            "pub_hours": pub_hours, "likes_vals": likes_vals, "watch": dict(watch),
            "n_posts_7d": len(week)}


# ---------------------------------------------------------------- 문피아 연동(선택)
def munpia_ranks():
    url = os.environ.get("MUNPIA_JSON_URL", "").strip()
    if not url:
        return {}
    try:
        headers = {}
        tok = os.environ.get("MUNPIA_TOKEN", "").strip()
        if tok:
            headers["Authorization"] = f"token {tok}"
        j = requests.get(url, headers=headers, timeout=15).json()
        items = j if isinstance(j, list) else next(
            (v for v in j.values() if isinstance(v, list)), [])
        out = {}
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                continue
            title = next((it[k] for k in it if "title" in k.lower() or "제목" in k), None)
            rank = next((it[k] for k in it if "rank" in k.lower() or "순위" in k), i)
            if title:
                out[str(title).strip()] = rank
        return out
    except Exception as e:
        log_line("report", f"문피아 연동 실패: {e}")
        return {}


# ---------------------------------------------------------------- 실행 제안
def recommend(cfg, yt, bl):
    def top_hours(counter, n=2, default=None):
        if counter:
            return [h for h, _ in Counter(counter).most_common(n)]
        return default or []

    vel_hours = sorted(yt["velocity"], key=lambda h: yt["velocity"][h], reverse=True)[:2]
    yt_hours = vel_hours or top_hours(yt["upload_hours"], 2) or [19, 21]
    bl_hours = top_hours(bl["pub_hours"], 2) or [18, 20]

    def top_topics(cnt, views=None, n=2):
        if views:
            scored = [(t, statistics.mean(v)) for t, v in views.items() if len(v) >= 3]
            scored.sort(key=lambda x: x[1], reverse=True)
            if scored:
                return [t for t, _ in scored[:n]]
        return [t for t, _ in cnt.most_common(n)] or []

    yt_topics = top_topics(yt["topic_cnt"], yt["topic_views"]) or ["명대사·명장면", "추천·큐레이션"]
    bl_topics = top_topics(bl["topic_cnt"]) or ["추천·큐레이션", "리뷰·감상"]

    shorts_share = 0
    tot = sum(yt["fmt_cnt"].values())
    if tot:
        shorts_share = yt["fmt_cnt"].get("쇼츠", 0) / tot

    data_ok = yt["pairs"] >= 30 or bl["n_posts_7d"] >= 30
    tag = "" if data_ok else " <span style='color:#98A2B3'>(초기 기본값 · 데이터 축적 후 자동 보정)</span>"

    def hh(hours):
        return "·".join(f"{h}시" for h in hours) + "대"

    plan = [
        ("네이버 블로그", "주 3회",
         f"① 검색 유입용 큐레이션(‘{bl_topics[0]}’ 포맷에 내 작품 자연 노출) ② 세계관·캐릭터 파일 ③ 연재 일지",
         "글+이미지 5~8장(카드뉴스형 병행)", hh(bl_hours) + tag),
        ("인스타그램", "주 3회",
         "명대사 카드뉴스 / 캐릭터 프로필 카드 / 릴스 예고편(15~30초)",
         "카드뉴스 4~6장 · 릴스", "20~22시대 (수동 관찰로 보정)"),
        ("유튜브", "주 2~3회",
         f"‘{yt_topics[0]}’ 중심 쇼츠 + 격주 1회 ‘{yt_topics[1] if len(yt_topics)>1 else '추천·큐레이션'}’",
         f"쇼츠 30~45초{' (시장 쇼츠 비중 ' + format(shorts_share, '.0%') + ')' if tot else ''}",
         hh(yt_hours) + tag),
    ]
    return plan, yt_hours, bl_hours


def dday_phase(days_left):
    if days_left >= 40:
        return "티저 준비기", "채널 개설·프로필/필명 통일, 장르 키워드 선점 글 2~3편, 커버·카드뉴스 원판 제작"
    if days_left >= 25:
        return "세계관 공개기", "캐릭터 카드뉴스 시리즈 개시, 쇼츠 예고편 1차 공개, 블로그 세계관 연재 시작"
    if days_left >= 10:
        return "본격 티징기", "명대사 카드·쇼츠 발행 주기 상향, 런칭일 고지 고정글, 상호 소통 계정 확보"
    if days_left >= 3:
        return "카운트다운", "D-7/D-3/D-1 카운트다운 콘텐츠, 프롤로그 발췌 선공개, 알림 설정 유도"
    if days_left >= 0:
        return "런칭 주간", "런칭 공지 3채널 동시 발행, 첫 화 링크 고정, 초기 댓글·선작 반응 리포트 확인"
    return "연재기", "주간 하이라이트·독자 반응 소개, 본 리포트의 반응 시간대에 맞춘 연재 시각 운영"


# ---------------------------------------------------------------- HTML 조립
def T(headers, rows, aligns=None, small=False):
    aligns = aligns or ["left"] * len(headers)
    fs = "12px" if small else "13px"
    th = "".join(
        f"<th style='padding:7px 8px;border-bottom:2px solid #14161A;font-size:12px;"
        f"color:#475467;text-align:{a};white-space:nowrap'>{esc(h)}</th>"
        for h, a in zip(headers, aligns))
    trs = ""
    for r in rows:
        tds = "".join(
            f"<td style='padding:7px 8px;border-bottom:1px solid #EAECF0;font-size:{fs};"
            f"color:#1F242E;text-align:{a};vertical-align:top'>{c}</td>"
            for c, a in zip(r, aligns))
        trs += f"<tr>{tds}</tr>"
    return (f"<table cellpadding='0' cellspacing='0' width='100%' "
            f"style='border-collapse:collapse;margin:6px 0 2px'>"
            f"<tr>{th}</tr>{trs}</table>")


def SEC(dot, title, inner, note=""):
    n = (f"<div style='font-size:11px;color:#98A2B3;margin-top:8px;line-height:1.6'>{note}</div>"
         if note else "")
    return f"""
    <tr><td style="padding:22px 26px 6px">
      <div style="font-size:15px;font-weight:700;color:#14161A;letter-spacing:-.2px">
        <span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{dot};margin-right:8px"></span>{title}
      </div>
      <div style="height:1px;background:#14161A;margin:8px 0 12px"></div>
      {inner}{n}
    </td></tr>"""


def IMG(cid_map, path, alt):
    if not path:
        return ""
    cid = f"img{len(cid_map)}"
    cid_map[cid] = path
    return (f"<img src='cid:{cid}' alt='{esc(alt)}' width='620' "
            f"style='display:block;width:100%;max-width:620px;border:1px solid #EAECF0;"
            f"border-radius:6px;margin:8px 0'/>")


def num(v):
    return f"{v:,}" if isinstance(v, (int, float)) else "—"


def short(s, n):
    s = s or ""
    return esc(s if len(s) <= n else s[: n - 1] + "…")


def build_html(cfg, now, yt, bl, comments, plan, notes, mp, ig_note):
    cid_map = {}
    launch = cfg["my_novel"]["launch_date"]
    d_left = (to_kst(launch + "T00:00:00+09:00").date() - now.date()).days
    dtxt = f"D-{d_left}" if d_left >= 0 else f"D+{-d_left}"
    phase, phase_todo = dday_phase(d_left)
    datestr = f"{now.year}. {now.month}. {now.day}.({WD[now.weekday()]}) 07:10 KST 기준"

    # ---- 핵심 요약
    hl = []
    if yt["new24"]:
        t0 = yt["new24"][0]
        hl.append(f"유튜브 신규 상위: <b>{short(t0['title'],30)}</b> — {num(t0['views'])}회"
                  + (f" (24h +{num(t0['delta24'])})" if t0.get("delta24") else ""))
    if yt["velocity"]:
        vh = max(yt["velocity"], key=yt["velocity"].get)
        hl.append(f"유튜브 반응 최고 시간대: <b style='color:{SEAL}'>{vh}시대</b> "
                  f"(시간당 평균 +{yt['velocity'][vh]:,.0f}회)")
    if bl["new24"]:
        hl.append(f"블로그 신규 상위노출 {len(bl['new24'])}건 · 활성 인기 블로거 {len(bl['pool'])}명")
    allw = Counter()
    for w, v in yt["watch"].items():
        allw[w] += v["n"]
    for w, v in bl["watch"].items():
        allw[w] += v
    if allw:
        w0 = allw.most_common(1)[0]
        hl.append(f"7일 최다 언급 작품: <b>{esc(w0[0])}</b> ({w0[1]}건)")
    if not hl:
        hl.append("수집 첫날입니다 — 24시간 후부터 지표가 채워집니다.")
    hl_html = "".join(f"<div style='padding:3px 0;font-size:13px;color:#1F242E'>"
                      f"<span style='color:{SEAL};font-weight:700'>›</span> {h}</div>" for h in hl)

    # ---- 유튜브 섹션
    ytab = T(
        ["#", "제목 / 채널", "형식", "주제", "업로드", "조회수", "24h증가"],
        [[i + 1,
          f"<a href='https://www.youtube.com/watch?v={esc(v['video_id'])}' "
          f"style='color:#14161A;text-decoration:none'><b>{short(v['title'],34)}</b></a>"
          f"<br><span style='color:#667085;font-size:11px'>{short(v['channel'],18)}"
          f" · 구독 {num(v.get('subs'))}</span>",
          esc(v.get("format", "")), esc(v.get("topic", "")),
          (f"{v['pub_dt'].month}/{v['pub_dt'].day} {v['pub_dt'].hour}시"
           f"({WD[v['pub_dt'].weekday()]})") if v.get("pub_dt") else "—",
          num(v["views"]),
          (f"<b style='color:{SEAL}'>+{num(v['delta24'])}</b>" if v.get("delta24") else "—")]
         for i, v in enumerate(yt["new24"][: cfg["report"]["top_n"]])],
        ["center", "left", "center", "center", "center", "right", "right"], small=True,
    ) if yt["new24"] else "<div style='color:#98A2B3;font-size:13px'>최근 24시간 내 기준 충족 신규 영상 없음</div>"

    c_yt_topic = chart_barh("yt_topic", yt["topic_cnt"].most_common(8),
                            "유튜브 · 주제 분포 (최근 7일, 편)")
    c_yt_hours = chart_hours("yt_hours", yt["upload_hours"],
                             "유튜브 · 업로드 시간대 분포 (최근 7일)", "업로드 편수")
    c_yt_vel = chart_hours("yt_vel", {h: round(v) for h, v in yt["velocity"].items()},
                           "유튜브 · 시간대별 조회수 증가 속도 (시간당 평균, 회)", "회/시간") \
        if yt["velocity"] else None
    tv = [(t, int(statistics.mean(v))) for t, v in yt["topic_views"].items() if len(v) >= 2]
    tv.sort(key=lambda x: x[1], reverse=True)
    eng = ("<div style='font-size:12px;color:#475467;margin-top:6px'>주제별 평균 조회수(반응 효율): "
           + " · ".join(f"<b>{esc(t)}</b> {v:,}" for t, v in tv[:4]) + "</div>") if tv else ""
    cm = ""
    if comments:
        cm = ("<div style='font-size:12px;font-weight:700;color:#14161A;margin-top:12px'>독자 반응 샘플 (상위 영상 베스트 댓글)</div>"
              + "".join(f"<div style='font-size:12px;color:#475467;padding:3px 0'>"
                        f"“{esc(c['text'])}” <span style='color:#98A2B3'>— {esc(c['video'])}"
                        f" · 좋아요 {c['likes']:,}</span></div>" for c in comments))
    fmts = " · ".join(f"{esc(k)} {v}편" for k, v in yt["fmt_cnt"].most_common())
    yt_inner = (ytab + IMG(cid_map, c_yt_topic, "유튜브 주제 분포") + eng
                + IMG(cid_map, c_yt_hours, "유튜브 업로드 시간대")
                + (IMG(cid_map, c_yt_vel, "유튜브 반응 속도") if c_yt_vel else
                   f"<div style='font-size:12px;color:#98A2B3'>시간대별 반응 속도: 스냅샷 {yt['pairs']}쌍 축적 중 (30쌍 이상부터 표시)</div>")
                + (f"<div style='font-size:12px;color:#475467;margin-top:4px'>형식 분포: {fmts}</div>" if fmts else "")
                + cm)
    yt_note = ("추적 영상 " + str(yt["tracked_n"]) + "개 · ‘24h증가’는 매시 스냅샷 차분값. "
               "타 채널의 실제 시청자 유입 시각은 외부에서 볼 수 없어, 공개 조회수의 증가 속도로 근사합니다.")

    # ---- 블로그 섹션
    btab = T(
        ["#", "제목 / 블로거", "형식", "주제", "최고순위", "공감", "링크"],
        [[i + 1,
          f"<b>{short(p['title'],36)}</b><br><span style='color:#667085;font-size:11px'>"
          f"{short(p['blogger'],16)} · {esc(p.get('postdate',''))}</span>",
          esc(p.get("fmt", "")), esc(p.get("topic", "")),
          f"{p['best_rank']}위" if p["best_rank"] < 999 else "—",
          num(p.get("likes")) if p.get("likes") is not None else "—",
          f"<a href='{esc(p['link'])}' style='color:{SEAL};font-size:11px'>보기</a>"]
         for i, p in enumerate(bl["new24"][: cfg["report"]["top_n"]])],
        ["center", "left", "center", "center", "center", "right", "center"], small=True,
    ) if bl["new24"] else "<div style='color:#98A2B3;font-size:13px'>최근 24~48시간 발행분 중 상위 노출 신규 글 없음</div>"

    ptab = T(
        ["활성 인기 블로거", "7일 노출", "활동일", "최고순위"],
        [[f"<a href='{esc(b['link'])}' style='color:#14161A;text-decoration:none'>"
          f"<b>{short(b['blogger'],20)}</b></a>",
          f"{b['exposures']}회", f"{b['days_active']}일", f"{b['best_rank']}위"]
         for b in bl["pool"][:8]],
        ["left", "right", "right", "right"], small=True,
    ) if bl["pool"] else "<div style='color:#98A2B3;font-size:13px'>인기 블로거 풀 축적 중 (2~3일 소요)</div>"

    c_bl_topic = chart_barh("bl_topic", bl["topic_cnt"].most_common(8),
                            "블로그 · 주제 분포 (최근 7일, 건)")
    c_bl_hours = chart_hours("bl_hours", bl["pub_hours"],
                             "블로그 · 발행 시간대 분포 (표본 기준)", "발행 건수")
    likes_txt = ""
    if bl["likes_vals"]:
        likes_txt = (f"<div style='font-size:12px;color:#475467;margin-top:4px'>공감 반응(표본 {len(bl['likes_vals'])}건): "
                     f"평균 {statistics.mean(bl['likes_vals']):,.0f} · 최대 {max(bl['likes_vals']):,}</div>")
    fcov = (f" · 형식 판별 표본 {bl['fmt_cover']}건" if bl["fmt_cover"] else "")
    fmtb = " · ".join(f"{esc(k)} {v}건" for k, v in bl["fmt_cnt"].most_common())
    bl_inner = (btab + "<div style='height:10px'></div>" + ptab
                + IMG(cid_map, c_bl_topic, "블로그 주제 분포")
                + (IMG(cid_map, c_bl_hours, "블로그 발행 시간대") if c_bl_hours else "")
                + (f"<div style='font-size:12px;color:#475467'>형식 분포: {fmtb}</div>" if fmtb else "")
                + likes_txt)
    bl_note = ("네이버 블로그는 조회수를 공개하지 않아 검색 상위노출·공감·활동빈도를 인기 대리지표로 사용합니다. "
               "발행 시각·이미지 수는 본문 표본 수집분에서만 판별됩니다" + fcov + ".")

    # ---- 관심 작품·홍보효과 프록시
    wrows = []
    for w, c in allw.most_common(10):
        yv = yt["watch"].get(w, {})
        mprk = mp.get(w)
        wrows.append([f"<b>{esc(w)}</b>",
                      f"{yv.get('n', 0)}편 / {num(yv.get('views', 0))}회",
                      f"{bl['watch'].get(w, 0)}건",
                      (f"<b style='color:{SEAL}'>{esc(mprk)}위</b>" if mprk else "—")])
    wtab = T(["작품", "유튜브 언급(편/조회 합)", "블로그 언급", "문피아 순위"],
             wrows, ["left", "right", "right", "right"], small=True) if wrows else \
        "<div style='color:#98A2B3;font-size:13px'>관심 작품 언급 집계 중 — config.json의 watch_titles에 경쟁작을 추가하세요.</div>"
    w_note = ("‘홍보효과’는 콘텐츠 언급량과 플랫폼 순위의 동반 변화를 관찰하는 상관 지표입니다(인과 단정 불가). "
              "문피아 순위 열은 기존 문피아 모니터링 저장소를 연동(MUNPIA_JSON_URL)하면 자동 표시됩니다.")

    # ---- 실행 제안
    ptab2 = T(["채널", "주당", "콘텐츠(우선 주제)", "형식", "권장 시간대"],
              [[f"<b>{p[0]}</b>", p[1], p[2], p[3], p[4]] for p in plan],
              ["left", "center", "left", "left", "left"], small=True)
    phase_html = (f"<div style='margin-top:10px;padding:10px 12px;border-left:3px solid {SEAL};"
                  f"background:#FBF7F6;font-size:13px;color:#1F242E'>"
                  f"<b>{dtxt} · {phase}</b> — {phase_todo}</div>")

    # ---- 시스템
    sys_html = ("".join(f"<div style='font-size:12px;color:#475467;padding:2px 0'>· {esc(n)}</div>"
                        for n in notes)
                + f"<div style='font-size:12px;color:#98A2B3;padding:2px 0'>· 수집 현황: "
                  f"유튜브 추적 {yt['tracked_n']}개 · 블로그 7일 표본 {bl['n_posts_7d']}건 · 반응속도 스냅샷 {yt['pairs']}쌍</div>")

    body = f"""<!doctype html><html lang="ko"><body style="margin:0;padding:0;background:#F2F3F5">
<table cellpadding="0" cellspacing="0" width="100%" style="background:#F2F3F5"><tr><td align="center" style="padding:18px 8px">
<table cellpadding="0" cellspacing="0" width="680" style="max-width:680px;width:100%;background:#FFFFFF;border:1px solid #E4E7EC">
  <tr><td style="background:#14161A;padding:22px 26px">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td>
        <div style="font-size:11px;letter-spacing:3px;color:#98A2B3">WEBNOVEL MARKETING RADAR</div>
        <div style="font-size:21px;font-weight:800;color:#FFFFFF;margin-top:4px;letter-spacing:-.3px">웹소설 마케팅 레이더 · 데일리 브리핑</div>
        <div style="font-size:12px;color:#98A2B3;margin-top:6px">{datestr} · 《{esc(cfg['my_novel']['title'])}》 {esc(cfg['my_novel']['platform'])} 런칭</div>
      </td>
      <td align="right" valign="top" width="86">
        <div style="display:inline-block;border:2px solid {SEAL};color:{SEAL};font-weight:800;font-size:17px;padding:10px 8px;letter-spacing:1px;background:#FFFFFF">{dtxt}</div>
      </td>
    </tr></table>
  </td></tr>
  {SEC(SEAL, "오늘의 핵심", hl_html)}
  {SEC("#FF0000", "유튜브", yt_inner, yt_note)}
  {SEC("#03C75A", "네이버 블로그", bl_inner, bl_note)}
  {SEC("#B667C8", "인스타그램", ig_note,
       "인스타그램은 공식 API 제약(비즈니스 계정·검수·해시태그 30개/주 한도)으로 자동 수집이 제한됩니다. "
       "manual/instagram_notes.md에 관찰 메모를 남기면 다음 날 브리핑에 그대로 실립니다.")}
  {SEC("#475467", "관심 작품 언급 · 홍보효과 프록시", wtab, w_note)}
  {SEC(SEAL, "오늘의 실행 제안", ptab2 + phase_html,
       "권장값은 전일까지의 수집 데이터로 매일 자동 갱신됩니다. 초기 1~2주는 표본이 작아 기본값과 혼용됩니다.")}
  {SEC("#98A2B3", "시스템 · 인기 기준 자동조정", sys_html)}
  <tr><td style="padding:16px 26px;background:#FAFAFB;border-top:1px solid #EAECF0">
    <div style="font-size:11px;color:#98A2B3;line-height:1.7">
      매일 07:10 KST 자동 발송 · 설정 변경: config.json (키워드·관심작품·발굴 시각) ·
      본 리포트의 수치는 공개 데이터 기반 추정치이며, 소수 표본 구간은 해석에 유의하십시오.
    </div>
  </td></tr>
</table></td></tr></table></body></html>"""
    return body, cid_map


# ---------------------------------------------------------------- CSV 첨부
def build_csv(yt, bl):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["platform", "title", "creator", "format", "topic",
                "published/postdate", "views", "delta24h/likes", "link"])
    for v in yt["new24"][:30]:
        w.writerow(["youtube", v["title"], v["channel"], v.get("format"),
                    v.get("topic"), v.get("published_kst"), v["views"],
                    v.get("delta24"), f"https://www.youtube.com/watch?v={v['video_id']}"])
    for p in bl["new24"][:30]:
        w.writerow(["naver_blog", p["title"], p["blogger"], p.get("fmt"),
                    p.get("topic"), p.get("postdate"), "", p.get("likes"), p["link"]])
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------- 발송
def send_mail(cfg, subject, html, cid_map, csv_bytes):
    addr = os.environ["GMAIL_ADDRESS"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("REPORT_TO", addr)

    root = MIMEMultipart("related")
    root["Subject"] = str(Header(subject, "utf-8"))
    root["From"] = formataddr((str(Header("웹소설 마케팅 레이더", "utf-8")), addr))
    root["To"] = to
    alt = MIMEMultipart("alternative")
    root.attach(alt)
    alt.attach(MIMEText("HTML 메일을 지원하는 환경에서 확인해 주세요.", "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    for cid, path in cid_map.items():
        with open(path, "rb") as f:
            img = MIMEImage(f.read(), _subtype="png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        root.attach(img)
    att = MIMEApplication(csv_bytes, _subtype="csv")
    att.add_header("Content-Disposition", "attachment",
                   filename=f"radar_{now_kst().strftime('%Y%m%d')}.csv")
    root.attach(att)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(addr, pw)
        s.sendmail(addr, [to], root.as_string())


# ---------------------------------------------------------------- main
def main():
    setup_korean_font()
    cfg = load_config()
    now = now_kst()
    th = load_thresholds(cfg)

    yt = yt_analyze(cfg, th, now)
    bl = blog_analyze(cfg, th, now)
    comments = [] if os.environ.get("DRY_RUN") else yt_top_comments(cfg, yt["new24"])
    mp = munpia_ranks()
    plan, *_ = recommend(cfg, yt, bl)

    # 임계값 자동 조정 (요구사항: 개체 수에 따라 기준 상·하향)
    th, notes = adjust_thresholds(cfg, th, len(yt["new24"]), len(bl["pool"]))
    save_thresholds(th)
    for n in notes:
        log_line("report", n)

    # 인스타그램 수동 노트
    ig_path = os.path.join(os.path.dirname(__file__), "..", "manual", "instagram_notes.md")
    ig_note = "<div style='color:#98A2B3;font-size:13px'>이번 주 수동 관찰 메모가 아직 없습니다. 주 2회, 회당 15분 관찰을 권장합니다.</div>"
    try:
        with open(ig_path, encoding="utf-8") as f:
            txt = f.read().strip()
        if txt:
            paras = "".join(f"<div style='font-size:13px;color:#1F242E;padding:2px 0'>{esc(l)}</div>"
                            for l in txt.splitlines() if l.strip() and not l.startswith("#"))
            if paras:
                ig_note = paras
    except Exception:
        pass

    html, cid_map = build_html(cfg, now, yt, bl, comments, plan, notes, mp, ig_note)
    csv_bytes = build_csv(yt, bl)

    d_left = (to_kst(cfg["my_novel"]["launch_date"] + "T00:00:00+09:00").date() - now.date()).days
    subject = (f"{cfg['report']['subject_prefix']} {now.month}/{now.day}({WD[now.weekday()]}) "
               f"데일리 브리핑 — 런칭 D-{d_left}" if d_left >= 0 else
               f"{cfg['report']['subject_prefix']} {now.month}/{now.day}({WD[now.weekday()]}) 데일리 브리핑")

    if os.environ.get("DRY_RUN"):
        inline = html
        for cid, path in cid_map.items():
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            inline = inline.replace(f"cid:{cid}", f"data:image/png;base64,{b64}")
        out = OUT_DIR / "sample_report.html"
        out.write_text(inline, encoding="utf-8")
        print("DRY_RUN → ", out)
        return

    try:
        send_mail(cfg, subject, html, cid_map, csv_bytes)
        log_line("report", f"발송 완료 → {os.environ.get('REPORT_TO','(미설정)')}")
        print("mail sent")
    except Exception as e:
        log_line("report", f"발송 실패: {e}")
        raise


if __name__ == "__main__":
    main()
