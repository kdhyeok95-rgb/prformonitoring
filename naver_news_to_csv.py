
# naver_news_to_csv.py (patched)
# -----------------------------------------------------------------------------
# 네이버 뉴스 수집 → 출입명단/우호도 반영 CSV/XLSX 생성
# - SSL 검증 비활성화(verify=False)로 네이버 API/원문 수집 시 인증서 오류 무시
# - 날짜 필터 보강(YYYY-MM-DD 변환 실패 시 제외)
# - 간단 분석 모드(--simple): 기자/매체 파싱 생략 → 제목/게시일/우호도만
# -----------------------------------------------------------------------------

import os
import sys
import csv
import re
import html
import json
import argparse
import urllib.parse
import warnings
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.exceptions import InsecureRequestWarning
from bs4 import BeautifulSoup

# ========= NAVER OPENAPI =========
CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "rQOsWdr3UtdmpdMHBila")
CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "31fPq4Psre")
API_URL_JSON = "https://openapi.naver.com/v1/search/news.json"

# ========= 수집 파라미터 =========
DEFAULT_TOTAL = 1000
PAGE_SIZE = 100
SORT = "date"
MAX_WORKERS = 5
REQ_TIMEOUT = 7
RETRY_TOTAL = 2

# ========= 네트워크/파싱 가드 =========
ALLOW_INSECURE_FALLBACK = True
MAX_HTML_BYTES = 2_000_000
MAX_PARSE_CHARS = 800_000
CONTENT_TYPE_HTML_HINTS = ("text/html", "application/xhtml+xml")

# ========= 기본 우호도 사전(백업) =========
POS_WORDS_DEFAULT = {
    "최고","호재","상승","강세","신기록","수상","인정","증가","개선","흑자","성공",
    "선정","수주","협약","확보","안정","혁신","성과","완료","오픈","사상 최대","달성"
}
NEG_WORDS_DEFAULT = {
    "하락","약세","적자","벌금","제재","고발","논란","비리","부정","부실","파산","리콜",
    "부진","사고","화재","중단","지연","취소","폐쇄","철수","축소","구속","징역","불법",
    "혐의","유출","피해","폭락","과징금","파업","손실","분식","횡령","갑질","해고","부도",
    "중대재해","중처법","산업재해","안전사고"
}
NEU_WORDS_DEFAULT = {
    "발표","개최","출시","예정","검토","논의","개편","변경","업데이트","신규","체결","진행","도입","운영","전략","계획"
}

# ========= 정규표현식/유틸 =========
TAG_RE = re.compile(r"<[^>]+>")
TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]{2,}")
HANGUL_NAME = re.compile(r"([가-힣]{2,4})\s*기자", re.S | re.I)
META_OG_SITENAME_RE = re.compile(
    r'<meta[^>]+property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)["\']', re.I)
META_AUTHOR_RE = re.compile(
    r'<meta[^>]+name=["\']author["\'][^>]*content=["\']([^"\']+)["\']', re.I)

DOMAIN_ALIAS = {
    "www.yna.co.kr":"연합뉴스","yna.co.kr":"연합뉴스",
    "news1.kr":"뉴스1","www.newsis.com":"뉴시스","newsis.com":"뉴시스",
    "biz.chosun.com":"조선비즈","www.chosun.com":"조선일보","chosun.com":"조선일보",
    "www.joongang.co.kr":"중앙일보","joongang.co.kr":"중앙일보",
    "www.hankyung.com":"한국경제","www.mk.co.kr":"매일경제","www.hani.co.kr":"한겨레",
    "www.edaily.co.kr":"이데일리","edaily.co.kr":"이데일리",
}
DOMAIN_SUBSTR_HINTS = [
    ("chosun","조선일보"),("joongang","중앙일보"),("hankyung","한국경제"),
    ("hankyoreh","한겨레"),("donga","동아일보"),("khan","경향신문"),
    ("mt.co.kr","머니투데이"),("mk.co.kr","매일경제"),("biz.chosun","조선비즈"),
    ("yna","연합뉴스"),("newsis","뉴시스"),("edaily","이데일리"),
    ("sedaily","서울경제"),("mbn","MBN"),("etoday","이투데이"),
]

# 디버깅용 전역(감지된 출입명단 컬럼 정보)
_WL_INFO = {"media_col": None, "reporter_cols": []}

def _suppress_insecure_warning():
    try:
        import urllib3
        urllib3.disable_warnings(InsecureRequestWarning)
    except Exception:
        pass
    warnings.filterwarnings("ignore", category=InsecureRequestWarning)

def now_kst_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def clean_html(s: str) -> str:
    if not s:
        return ""
    return html.unescape(TAG_RE.sub("", s)).strip()

def to_yyyy_mm_dd(pub_date_raw: str) -> str:
    if not pub_date_raw:
        return ""
    try:
        dt = parsedate_to_datetime(pub_date_raw)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        m = re.search(r"\d{4}-\d{2}-\d{2}", pub_date_raw or "")
        return m.group(0) if m else (pub_date_raw or "")[:10]

def parse_yyyy_mm_dd(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def _make_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=RETRY_TOTAL,
        backoff_factor=0.4,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
    )
    ad = HTTPAdapter(max_retries=retries, pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
    s.mount("http://", ad)
    s.mount("https://", ad)
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s

def netloc(u: str) -> str:
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""

def second_level(nl: str) -> str:
    parts = (nl or "").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "or", "go", "ne") and parts[-1] == "kr":
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return nl

def infer_press_from_url(url: str) -> str:
    nl = netloc(url)
    if not nl:
        return "미분류"
    if nl in DOMAIN_ALIAS:
        return DOMAIN_ALIAS[nl]
    for sub, name in DOMAIN_SUBSTR_HINTS:
        if sub in nl:
            return name
    token = second_level(nl)
    return token.title() if re.fullmatch(r"[a-zA-Z0-9-]+", token or "") else token or "미분류"

def sanitize_press(raw_press: str, title: str, url: str) -> str:
    rp = (raw_press or "").strip()
    if not rp or rp == (title or "").strip():
        rp = infer_press_from_url(url)
    rp = re.sub(r"^\(주\)\s*|^주식회사\s*|\s*주식회사$|\s*\(주\)$", "", rp).strip()
    return rp

def _norm_person(s: str) -> str:
    s = re.sub(r"기자", "", s or "")
    s = re.sub(r"\(.+?\)", "", s)
    s = re.sub(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+", "", s)
    s = re.sub(r"[^가-힣]", "", s)
    return s.strip()

def _from_jsonld_name(obj):
    for key in ("publisher", "provider", "organization", "Organization"):
        v = obj.get(key)
        if isinstance(v, dict) and v.get("name"):
            return str(v["name"]).strip()
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, dict) and first.get("name"):
                return str(first["name"]).strip()
    return None

def _from_jsonld_author(obj):
    for key in ("author", "creator"):
        v = obj.get(key)
        if isinstance(v, dict) and v.get("name"):
            return str(v["name"]).strip()
        if isinstance(v, list) and v:
            for it in v:
                if isinstance(it, dict) and it.get("name"):
                    return str(it["name"]).strip()
    return None

# ========= 빠른 메타 추출(정규식) =========
def fast_meta_extract(html_text: str):
    press = ""
    reporter = ""
    m = META_OG_SITENAME_RE.search(html_text or "")
    if m:
        press = m.group(1).strip()
    a = META_AUTHOR_RE.search(html_text or "")
    if a:
        reporter = _norm_person(a.group(1))
    return press, reporter

# ========= Soup 생성(lxml 우선) =========
def make_soup_lite(html_text: str):
    txt = (html_text or "")[:MAX_PARSE_CHARS]
    try:
        import lxml  # noqa: F401
        return BeautifulSoup(txt, "lxml")
    except Exception:
        return BeautifulSoup(txt, "html.parser")

def extract_press_and_reporter(html_text: str):
    if not html_text:
        return ("", "")
    head = (html_text[:4096] or "").lower()
    if "<html" not in head and "<meta" not in head:
        return ("", "")

    # 1) 빠른 메타 추출
    p_fast, r_fast = fast_meta_extract(html_text)
    if p_fast and r_fast:
        return (p_fast, r_fast)

    # 2) Soup 경량 파싱
    soup = make_soup_lite(html_text)

    # press
    press = ""
    m = soup.find("meta", {"property": "og:site_name"})
    if m and m.get("content"):
        press = m["content"].strip()
    if not press:
        for tag in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(tag.string or "")
            except Exception:
                continue
            if isinstance(data, dict):
                n = _from_jsonld_name(data)
                if n:
                    press = n
                    break
            elif isinstance(data, list):
                for obj in data:
                    if isinstance(obj, dict):
                        n = _from_jsonld_name(obj)
                        if n:
                            press = n
                            break
                if press:
                    break

    # reporter
    reporter = ""
    if not reporter:
        for tag in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(tag.string or "")
            except Exception:
                continue
            cand = None
            if isinstance(data, dict):
                cand = _from_jsonld_author(data)
            elif isinstance(data, list):
                for obj in data:
                    if isinstance(obj, dict):
                        cand = _from_jsonld_author(obj)
                        if cand:
                            break
            if cand:
                reporter = _norm_person(cand)
                if reporter:
                    break
    if not reporter:
        ma = soup.find("meta", {"name": "author"})
        if ma and ma.get("content"):
            reporter = _norm_person(ma["content"])
    if not reporter:
        selectors = ["[class*=reporter]", "[class*=byline]", "[class*=author]",
                     "[id*=reporter]", "[id*=byline]", "[id*=author]"]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                txt = el.get_text(" ", strip=True)
                m = HANGUL_NAME.search(txt)
                reporter = _norm_person(m.group(1) if m else txt)
                if reporter:
                    break
    if not reporter:
        art = soup.find("article") or soup.find("body")
        if art:
            txt = art.get_text(" ", strip=True)[:500]
            m = HANGUL_NAME.search(txt)
            if m:
                reporter = _norm_person(m.group(1))

    return (press.strip(), reporter.strip())

# ========= 파일 탐색 =========
def find_file(filename: str):
    candidates = [
        os.path.join(os.getcwd(), filename),
        os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), filename),
        os.path.join("/mnt/data", filename),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None

# ========= CSV 로더(인코딩 자동) =========
def _read_csv_rows_any_encoding(path: str, encodings=("utf-8-sig", "cp949", "utf-8")):
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                cols = [c.strip() for c in (reader.fieldnames or [])]
                return cols, rows
        except Exception:
            continue
    return None, None

# ========= 출입 명단 =========
def load_whitelist():
    path = find_file("당사업장 출입 기자 관련 안내.csv")
    media_set, reporter_set = set(), set()
    _WL_INFO["media_col"] = None
    _WL_INFO["reporter_cols"] = []
    if not path:
        return media_set, reporter_set

    cols, rows = _read_csv_rows_any_encoding(path)
    if cols is None or rows is None:
        return media_set, reporter_set

    # 유연한 컬럼 매핑
    cols_lower = [c.lower() for c in cols]
    def _pick(*cands):
        for c in cands:
            if c in cols:
                return c
            cl = c.lower()
            if cl in cols_lower:
                return cols[cols_lower.index(cl)]
        return None

    # 매체 단일 우선 후보
    col_media = _pick("매체명","언론사","매체","press","media","출입매체","출입 언론사")
    if not col_media:
        # "매체"라는 단어 포함된 첫 컬럼
        for c in cols:
            if "매체" in c:
                col_media = c
                break
    _WL_INFO["media_col"] = col_media

    # 기자 컬럼: 다중 감지
    reporter_cols = []
    # 명시 후보
    explicit = {"출입기자","출입 기자","기자","기자명","기자이름","기자 성명","담당기자","담당 기자",
                "reporter","author","byline"}
    for c in cols:
        cl = c.lower()
        if c in explicit or ("기자" in c) or ("성명" in c) or ("이름" in c) or ("담당" in c) or ("author" in cl) or ("byline" in cl) or ("reporter" in cl):
            reporter_cols.append(c)
    _WL_INFO["reporter_cols"] = reporter_cols

    def _split_multi(cell: str):
        # 콤마/슬래시/세미콜론/파이프/·/•/및
        return [p for p in re.split(r"(?:\s*[,;/|·•]\s*|\s+및\s+)", str(cell).strip()) if p]

    for row in rows:
        # 매체
        if col_media and row.get(col_media):
            m = str(row[col_media]).strip()
            if m:
                media_set.add(m)

        # 기자(복수 열 + 복수 이름)
        for rc in reporter_cols:
            if row.get(rc):
                for token in _split_multi(row[rc]):
                    name = _norm_person(token)
                    if name:
                        reporter_set.add(name)

    return media_set, reporter_set

# ========= 우호도 기준 =========
def load_sentiment_rules():
    path = find_file("뉴스 우호도 기준.csv")
    pos, neg, neu = set(POS_WORDS_DEFAULT), set(NEG_WORDS_DEFAULT), set(NEU_WORDS_DEFAULT)
    if not path:
        return pos, neg, neu, "fallback(default)"

    cols, rows = _read_csv_rows_any_encoding(path)
    if cols is None or rows is None:
        return pos, neg, neu, "fallback(default)"

    cols_lower = [c.lower() for c in cols]
    def _pick(*cands):
        for c in cands:
            if c in cols:
                return c
            cl = c.lower()
            if cl in cols_lower:
                return cols[cols_lower.index(cl)]
        return None

    col_kw    = _pick("키워드", "keyword", "term", "단어")
    col_lab   = _pick("우호도", "label", "sentiment", "라벨")
    col_pos   = next((c for c in cols if ("긍정" in c) or (c.lower() == "positive")), None)
    col_neg   = next((c for c in cols if ("부정" in c) or (c.lower() == "negative")), None)
    col_neu   = next((c for c in cols if ("중립" in c) or (c.lower() == "neutral")), None)
    col_title = _pick("제목", "뉴스제목", "title")

    loaded = False

    if col_kw and col_lab:
        for row in rows:
            kw  = str(row.get(col_kw, "")).strip()
            lab = str(row.get(col_lab, "")).strip().lower()
            if not kw:
                continue
            if lab in ("부정", "neg", "negative", "비우호"):
                neg.add(kw)
            elif lab in ("중립", "neu", "neutral"):
                neu.add(kw)
            else:
                pos.add(kw)
        loaded = True

    elif (col_pos or col_neg or col_neu):
        def _split_words(cell):
            return [w for w in re.split(r"[,\s;]+", str(cell).strip()) if w]
        for row in rows:
            if col_pos and row.get(col_pos):
                for w in _split_words(row[col_pos]):
                    pos.add(w)
            if col_neg and row.get(col_neg):
                for w in _split_words(row[col_neg]):
                    neg.add(w)
            if col_neu and row.get(col_neu):
                for w in _split_words(row[col_neu]):
                    neu.add(w)
        loaded = True

    elif col_title and col_lab:
        cnt_pos, cnt_neg, cnt_neu = {}, {}, {}
        stop = {"현대제철","당진제철소","단독","종합","속보","기획","사설","칼럼","포토","영상"}
        for row in rows:
            title = str(row.get(col_title,""))
            lab   = str(row.get(col_lab,"")).strip().lower()
            toks = [t for t in TOKEN_RE.findall(title) if t not in stop and len(t)>=2]
            box = cnt_neu
            if lab in ("부정","neg","negative","비우호"): box = cnt_neg
            elif lab in ("긍정","pos","positive","호의"): box = cnt_pos
            for t in toks: box[t] = box.get(t,0)+1
        def _topk(d, topn=200, minfreq=2):
            items = [(k,v) for k,v in d.items() if v>=minfreq]
            items.sort(key=lambda x:-x[1])
            return {k for k,_ in items[:topn]}
        pos |= _topk(cnt_pos); neg |= _topk(cnt_neg); neu |= _topk(cnt_neu)
        loaded = True

    return pos, neg, neu, ("rules(csv)" if loaded else "fallback(default)")

def classify_sentiment(title: str, pos_set: set, neg_set: set, neu_set: set, default_label="긍정") -> str:
    t = (title or "").strip()
    if not t:
        return default_label
    for w in neg_set:
        if w and w in t:
            return "부정"
    for w in pos_set:
        if w and w in t:
            return "긍정"
    for w in neu_set:
        if w and w in t:
            return "중립"
    return default_label

# ========= 네이버 뉴스 API (SSL 검증 비활성화) =========
def naver_news_search(query: str, total: int = DEFAULT_TOTAL, sort: str = SORT):
    _suppress_insecure_warning()
    headers = {"X-Naver-Client-Id": CLIENT_ID, "X-Naver-Client-Secret": CLIENT_SECRET}
    items, start = [], 1
    while len(items) < total:
        display = min(PAGE_SIZE, total - len(items))
        params = {"query": query, "display": display, "start": start, "sort": sort}
        resp = requests.get(API_URL_JSON, headers=headers, params=params, timeout=REQ_TIMEOUT, verify=False)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("items", [])
        if not batch:
            break
        items.extend(batch)
        start += display
        if start > 1000:  # 네이버 뉴스 API start 파라미터 한계
            break
    return items[:total]

# ========= 원문 HTML 수집 (SSL 실패 시 verify=False 재시도) =========
def fetch_html(url: str, session: requests.Session) -> str:
    if not url:
        return ""
    try:
        r = session.get(url, timeout=REQ_TIMEOUT, verify=True)
    except requests.exceptions.SSLError:
        if ALLOW_INSECURE_FALLBACK:
            _suppress_insecure_warning()
            try:
                r = session.get(url, timeout=REQ_TIMEOUT, verify=False)
            except Exception:
                return ""
        else:
            return ""
    except Exception:
        return ""

    ctype = (r.headers.get("Content-Type", "").lower())
    if not any(hint in ctype for hint in CONTENT_TYPE_HTML_HINTS):
        return ""
    clen = r.headers.get("Content-Length")
    if clen:
        try:
            if int(clen) > MAX_HTML_BYTES:
                return ""
        except Exception:
            pass

    text = r.text or ""
    if len(text) > MAX_PARSE_CHARS:
        text = text[:MAX_PARSE_CHARS]
    return text

# ========= 대화형 입력 유틸 =========
def ask(prompt: str, default: str = "") -> str:
    try:
        val = input(prompt).strip()
    except EOFError:
        val = ""
    return val if val else default

def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    try:
        val = input(f"{prompt} ({suffix}): ").strip().lower()
    except EOFError:
        val = ""
    if not val:
        return default
    return val in ("y", "yes", "ㅛ", "예", "ㅇ", "네")

def resolve_params_with_prompts(args):
    # 1) 검색어
    query = args.query if args.query else ask("1. 무엇을 검색하시겠습니까? (기본: 현대제철): ", "현대제철")

    # 2) 날짜 기준
    start_str = args.start_date if args.start_date else ask("2-1. 시작일을 입력하세요 (YYYY-MM-DD, 공란=제한 없음): ", "")
    end_str   = args.end_date   if args.end_date   else ask("2-2. 종료일을 입력하세요 (YYYY-MM-DD, 공란=제한 없음): ", "")
    start_dt = parse_yyyy_mm_dd(start_str) if start_str else None
    end_dt   = parse_yyyy_mm_dd(end_str)   if end_str   else None

    # 3) 출입 언론사 기준 필터
    whitelist_only = args.whitelist_only if args.whitelist_only is not None else ask_yes_no(
        "3. 당진제철소 출입 언론사를 기준으로 수집하시겠습니까?", default=False
    )

    # 4) 엑셀 출력 여부
    excel = args.excel if args.excel is not None else ask_yes_no(
        "4. 엑셀 파일로 출력하시겠습니까?", default=True
    )

    # 5) 간단 분석 모드
    simple_mode = args.simple if args.simple is not None else ask_yes_no(
        "5. 간단 분석 모드(제목/날짜/우호도만)로 진행하시겠습니까?", default=False
    )

    # 최대 수집 건수
    total = args.total if args.total is not None else DEFAULT_TOTAL
    total = max(1, min(1000, int(total)))
    print(f"[INFO] 요청 수집 건수: {total}건 (기본 1000 / 네이버 API 최대 1000)")

    out_csv = args.out_csv  # 지정 없으면 자동 이름
    return query, total, out_csv, whitelist_only, start_dt, end_dt, excel, simple_mode

# ========= 메인 =========
def run(query: str, total: int, out_csv: str | None, whitelist_only: bool,
        start_dt: date | None, end_dt: date | None, excel: bool, simple_mode: bool=False):
    collected_at = now_kst_str()
    media_whitelist, reporter_whitelist = load_whitelist()

    # 디버깅 정보 표시
    media_col = _WL_INFO.get("media_col")
    rep_cols = _WL_INFO.get("reporter_cols", [])
    print(f"[INFO] 출입매체 {len(media_whitelist)}건, 출입기자 {len(reporter_whitelist)}건 로드 완료")
    print(f"[INFO] (명단 컬럼) 매체: {media_col or '-'} | 기자: {rep_cols or '-'}")

    pos_set, neg_set, neu_set, rule_source = load_sentiment_rules()
    print(f"[INFO] 우호도 소스: {rule_source} (pos:{len(pos_set)}, neg:{len(neg_set)}, neu:{len(neu_set)})")
    if start_dt or end_dt:
        print(f"[INFO] 날짜 필터: {start_dt or '-'} ~ {end_dt or '-'} (게시일 기준)")

    api_items = naver_news_search(query, total=total, sort=SORT)
    print(f"[INFO] 네이버 API 수집: {len(api_items)}건")

    # works 준비
    works = []
    for it in api_items:
        link = it.get("link") or ""
        originallink = it.get("originallink") or ""
        target_url = originallink if originallink else link
        works.append((it, target_url))

    html_map = {}
    if not simple_mode:
        session = _make_session()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            fut2url = {ex.submit(fetch_html, url, session): (it, url) for it, url in works if url}
            for fut in as_completed(fut2url):
                it, url = fut2url[fut]
                try:
                    html_map[url] = fut.result()
                except Exception:
                    html_map[url] = ""

    rows = []
    for it, url in works:
        title = clean_html(it.get("title", ""))
        desc = clean_html(it.get("description", ""))
        pub_date_str = to_yyyy_mm_dd(it.get("pubDate", ""))

        # 날짜 필터(게시일 기준) - 변환 실패 시 제외
        if start_dt or end_dt:
            pd = parse_yyyy_mm_dd(pub_date_str) if pub_date_str else None
            if not pd:
                continue
            if start_dt and pd < start_dt:
                continue
            if end_dt and pd > end_dt:
                continue

        link = it.get("link") or ""
        originallink = it.get("originallink") or ""
        html_text = "" if simple_mode else html_map.get(url, "")

        # 간단 모드: 매체/기자 파싱 생략
        if simple_mode:
            press, reporter = "", ""
        else:
            press, reporter = ("", "")
            if html_text:
                press, reporter = extract_press_and_reporter(html_text)

        press = sanitize_press(press, title, url or link or originallink) if not simple_mode else ""
        reporter = (reporter or "").strip()
        reporter_norm = _norm_person(reporter)

        is_media_in = "Y" if (not simple_mode and press in media_whitelist) else ("-" if simple_mode else "N")
        is_reporter_in = "Y" if (not simple_mode and reporter_norm and (reporter_norm in reporter_whitelist)) else ("-" if simple_mode else "N")

        # 간단 모드에서는 출입매체 필터 무시(파싱을 안 하므로)
        if (not simple_mode) and whitelist_only and is_media_in != "Y":
            continue

        sentiment = classify_sentiment(title, pos_set, neg_set, neu_set, default_label="긍정")

        row = {
            "수집일시": collected_at,
            "검색어": query,
            "제목": title,
            "요약": desc,
            "게시일": pub_date_str,
            "우호도": sentiment,
            "링크": (originallink or link),  # 원본 우선
            "원문링크": originallink,
            "도메인": netloc(url or link or originallink),
            "비고": "",
            "배포처": "네이버뉴스(포털)"
        }
        if not simple_mode:
            row.update({
                "매체": press,
                "기자": reporter,
                "출입매체": is_media_in,
                "출입기자": is_reporter_in,
            })
        rows.append(row)

    if not rows:
        print("[WARN] 결과가 비어 있습니다.")
        return

    # 출력 컬럼
    if simple_mode:
        OUTPUT_FIELDS = ["검색어", "제목", "게시일", "우호도", "링크", "비고"]
    else:
        OUTPUT_FIELDS = [
            "검색어", "제목", "게시일", "매체", "기자",
            "출입매체", "출입기자", "우호도", "링크", "비고"
        ]

    # 파일 경로 생성
    if not out_csv:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_q = urllib.parse.quote(query)
        suffix = "_simple" if simple_mode else ""
        out_csv = f"naver_news{suffix}_{safe_q}_{ts}.csv"
    else:
        if not out_csv.lower().endswith(".csv"):
            out_csv = os.path.splitext(out_csv)[0] + ".csv"

    # CSV 저장
    encoding = "utf-8-sig"
    try:
        with open(out_csv, "w", encoding=encoding, newline="") as f:
            w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in OUTPUT_FIELDS})
        print(f"[OK] 저장: {out_csv} (encoding={encoding})  총 {len(rows)}건")
    except Exception as e:
        print(f"[ERR] UTF-8-SIG 저장 실패 → cp949 재시도: {e}")
        with open(out_csv, "w", encoding="cp949", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in OUTPUT_FIELDS})
        print(f"[OK] 저장: {out_csv} (encoding=cp949)  총 {len(rows)}건")

    # 엑셀 저장(선택)
    if excel:
        try:
            import pandas as pd
            df = pd.DataFrame([{k: r.get(k, "") for k in OUTPUT_FIELDS} for r in rows], columns=OUTPUT_FIELDS)
            xlsx_path = os.path.splitext(out_csv)[0] + ".xlsx"
            df.to_excel(xlsx_path, index=False)
            print(f"[OK] 엑셀 저장: {xlsx_path}")
        except Exception as e:
            print(f"[WARN] 엑셀 저장 실패(패키지 미설치 또는 권한 문제): {e}")
            print("       → CSV는 정상 저장되었습니다. (엑셀 저장 원하면: pip install pandas openpyxl)")

# ========= CLI =========
def parse_args():
    p = argparse.ArgumentParser(description="네이버 뉴스 수집 → 출입명단/우호도 반영 CSV/XLSX 생성")
    p.add_argument("-q", "--query", default=None, help="검색어 (미지정 시 프롬프트)")
    p.add_argument("-t", "--total", type=int, default=None,
                   help=f"최대 수집 건수(1~1000). 미지정 시 기본 {DEFAULT_TOTAL}")
    p.add_argument("--start-date", default=None, help="시작일 YYYY-MM-DD (미지정 시 프롬프트)")
    p.add_argument("--end-date", default=None, help="종료일 YYYY-MM-DD (미지정 시 프롬프트)")
    p.add_argument("--whitelist-only", dest="whitelist_only", action="store_true", help="출입 매체 기사만 포함")
    p.add_argument("--no-whitelist-only", dest="whitelist_only", action="store_false", help="출입 매체 필터 미적용")
    p.add_argument("--excel", dest="excel", action="store_true", help="엑셀 파일 추가 저장")
    p.add_argument("--no-excel", dest="excel", action="store_false", help="엑셀 저장 안 함")
    p.add_argument("--simple", dest="simple", action="store_true", help="간단 분석 모드(제목/날짜/우호도만)")
    p.add_argument("--no-simple", dest="simple", action="store_false", help="정밀 분석 모드")
    p.set_defaults(whitelist_only=None, excel=None, simple=False)
    p.add_argument("-o", "--out", dest="out_csv", default=None, help="출력 CSV 경로(확장자 .csv)")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    query, total, out_csv, whitelist_only, start_dt, end_dt, excel, simple_mode = resolve_params_with_prompts(args)
    try:
        run(query, total, out_csv, whitelist_only, start_dt, end_dt, excel, simple_mode=simple_mode)
    except requests.HTTPError as e:
        print(f"[HTTP ERROR] {e} | 응답본문: {getattr(e, 'response', None) and getattr(e.response,'text','')[:200]}")
    except Exception as e:
        print(f"[FATAL] {e}")
