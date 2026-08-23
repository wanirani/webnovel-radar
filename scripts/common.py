# -*- coding: utf-8 -*-
"""공용 유틸리티: 설정/상태 입출력, KST 시간, 주제·형식 분류기, 임계값 로직."""
import json
import os
import re
import html
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state"
OUT_DIR = ROOT / "out"
KST = timezone(timedelta(hours=9))

for d in (DATA_DIR, STATE_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 기본 IO
def load_config():
    with open(ROOT / "config.json", encoding="utf-8") as f:
        return json.load(f)


def load_json(path, default):
    p = Path(path)
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def append_jsonl(path, rows):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl_days(prefix_name, days, end_kst=None):
    """최근 N일치 data/YYYYMMDD/{prefix_name}.jsonl 을 모두 읽어 리스트로 반환."""
    end_kst = end_kst or now_kst()
    rows = []
    for i in range(days):
        day = (end_kst - timedelta(days=i)).strftime("%Y%m%d")
        p = DATA_DIR / day / f"{prefix_name}.jsonl"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows


def log_line(name, msg):
    p = STATE_DIR / f"{name}_log.txt"
    ts = now_kst().strftime("%Y-%m-%d %H:%M")
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"[{ts} KST] {msg}\n")
    # 로그 파일 500줄 초과 시 최근 300줄만 유지
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) > 500:
            p.write_text("\n".join(lines[-300:]) + "\n", encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 시간
def now_kst():
    return datetime.now(timezone.utc).astimezone(KST)


def to_kst(dt_str):
    """ISO8601(UTC 'Z' 포함) → KST datetime. 실패 시 None."""
    if not dt_str:
        return None
    try:
        s = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(KST)
    except Exception:
        return None


def parse_iso_duration(s):
    """유튜브 ISO8601 재생시간(PT#H#M#S) → 초. 실패 시 None."""
    if not s:
        return None
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", s)
    if not m:
        return None
    h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + sec


# ---------------------------------------------------------------- 텍스트
TAG_RE = re.compile(r"<[^>]+>")


def clean_text(s):
    if not s:
        return ""
    return html.unescape(TAG_RE.sub("", s)).strip()


def esc(s):
    return html.escape(str(s)) if s is not None else ""


# ---------------------------------------------------------------- 주제 분류
# (우선순위 순서: 위쪽 규칙이 먼저 매칭되면 그것이 주(主) 주제)
TOPIC_RULES = [
    ("예고편·티저", ["예고편", "트레일러", "티저", "예고 영상", "런칭 예고"]),
    ("명대사·명장면", ["명대사", "명장면", "명언", "어록", "인생 대사", "레전드 장면"]),
    ("캐릭터·세계관", ["캐릭터", "등장인물", "인물 소개", "세계관", "설정 정리", "인물 관계"]),
    ("작가 후기·창작기", ["작가 후기", "연재 후기", "완결 후기", "집필", "창작기", "웹소설 쓰", "작법", "투고", "컨택", "데뷔"]),
    ("리뷰·감상", ["리뷰", "감상", "읽어봤", "정주행 후기", "완독", "평가", "솔직 후기"]),
    ("추천·큐레이션", ["추천", "순위", "top", "TOP", "랭킹", "인생작", "명작", "모음", "리스트", "골라", "선]"]),
    ("플랫폼·시장 동향", ["문피아", "노벨피아", "카카오페이지", "카카페", "네이버 시리즈", "조아라", "매출", "유료화", "런칭", "신작 소식"]),
    ("미디어믹스", ["웹툰화", "드라마화", "애니화", "영상화", "게임화"]),
]
TOPIC_ORDER = [t for t, _ in TOPIC_RULES] + ["기타·일반"]


def classify_topic(text):
    """제목+요약 텍스트 → (주 주제, 매칭된 전체 주제 리스트)."""
    t = (text or "").lower()
    matched = []
    for topic, kws in TOPIC_RULES:
        for kw in kws:
            if kw.lower() in t:
                matched.append(topic)
                break
    if not matched:
        return "기타·일반", ["기타·일반"]
    return matched[0], matched


# ---------------------------------------------------------------- 형식 분류
def classify_yt_format(duration_s, title, desc):
    text = f"{title} {desc}".lower()
    if "#shorts" in text or "#쇼츠" in text or (duration_s is not None and duration_s <= 61):
        return "쇼츠"
    if duration_s is not None and duration_s <= 240:
        return "숏폼(4분 이하)"
    return "롱폼"


def classify_blog_format(img_count, text_len):
    if img_count is None:
        return "미확인(텍스트 추정)"
    if img_count >= 8:
        return "카드뉴스형(이미지 위주)"
    if img_count >= 3:
        return "이미지+글 혼합"
    return "텍스트 위주"


# ---------------------------------------------------------------- 관심 작품 언급
def match_watch_titles(text, watch_titles):
    t = text or ""
    hits = []
    for w in watch_titles:
        if w and w in t:
            hits.append(w)
    return hits


# ---------------------------------------------------------------- 임계값(적응형)
def load_thresholds(cfg):
    return load_json(STATE_DIR / "thresholds.json", cfg["thresholds_default"])


def save_thresholds(th):
    save_json(STATE_DIR / "thresholds.json", th)


def adjust_thresholds(cfg, th, yt_pool_size, blog_pool_size):
    """풀 크기가 목표 범위를 벗어나면 기준을 완화/강화. 조정 내역 문자열 리스트 반환."""
    pt = cfg["pool_targets"]
    f = pt.get("adjust_factor", 1.4)
    notes = []

    y = th["youtube"]
    if yt_pool_size > pt["youtube"]["high"]:
        old = y["min_views"]
        y["min_views"] = int(min(old * f, pt.get("youtube_min_views_ceil", 300000)))
        y["min_subscribers"] = int(y["min_subscribers"] * f)
        notes.append(f"유튜브 풀 {yt_pool_size}개(목표 상한 {pt['youtube']['high']}) → 기준 강화: 최소 조회수 {old:,}→{y['min_views']:,}")
    elif 0 <= yt_pool_size < pt["youtube"]["low"]:
        old = y["min_views"]
        y["min_views"] = int(max(old / f, pt.get("youtube_min_views_floor", 1000)))
        y["min_subscribers"] = int(max(y["min_subscribers"] / f, 1000))
        notes.append(f"유튜브 풀 {yt_pool_size}개(목표 하한 {pt['youtube']['low']}) → 기준 완화: 최소 조회수 {old:,}→{y['min_views']:,}")

    b = th["blog"]
    if blog_pool_size > pt["blog"]["high"]:
        old = b["max_rank"]
        b["max_rank"] = max(10, int(old / f))
        notes.append(f"블로그 풀 {blog_pool_size}개(상한 {pt['blog']['high']}) → 기준 강화: 검색순위 {old}위 이내→{b['max_rank']}위 이내")
    elif 0 <= blog_pool_size < pt["blog"]["low"]:
        old = b["max_rank"]
        b["max_rank"] = min(60, int(old * f))
        notes.append(f"블로그 풀 {blog_pool_size}개(하한 {pt['blog']['low']}) → 기준 완화: 검색순위 {old}위 이내→{b['max_rank']}위 이내")

    if not notes:
        notes.append(f"풀 크기 적정(유튜브 {yt_pool_size}, 블로그 {blog_pool_size}) → 기준 유지")
    return th, notes
