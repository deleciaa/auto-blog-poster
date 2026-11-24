"""
멀티 플랫폼 & 한/영 듀얼 자동 포스팅 최종 통합본 (+ 동적 시의성 태그 추출 & 텔레그램 발행 리포트 & 한/영 분리 방어 & 나들이 장소 중복 방지)
1. [it_news]     🌐 [오늘의 Tech Top 10] MM월 DD일 전 세계 IT·테크 핵심 뉴스 (+ 전문가 논평 + 동적 태그)
2. [it_easy]     🌐 [테크 상식] "한줄 훅!" 최신 테크 키워드 쉽게 풀기 (+ 동적 태그)
3. [family_trip] [주말 나들이] "한줄 훅!" + 7살 딸, 5살 아들과 함께한 [장소명] 정복기 (+ 동적 태그 + 권역 로테이션 + 중복 방지)

- 단 1회의 Gemini API 호출로 한국어(KO) + 영어(EN) 동시 생성 (일일 쿼터 50% 절약)
- 정규표현식 기반 다중 구분자 파싱으로 한/영 합본 현상 원천 차단
- 429 한도 초과 시 대체 모델 자동 전환
- 티스토리(쿠키 딕셔너리/리스트 2중 호환 + TinyMCE 직접 주입 + HTML/WYSIWYG 복귀), Blogger, WordPress, Velog 동시 발행
- DuckDuckGo(ddgs) 및 무료 AI 이미지 기반 실제 현장/장소/기술 사진 정밀 검색 폴백 탑재
- 발행 완료 후 총 건수, 플랫폼별 제목/링크, 실패 시 상세 사유 텔레그램 자동 보고
"""
import os
import re
import sys
import html
import json
import time
import random
import argparse
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
import markdown
from google import genai
from google.genai import types

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoAlertPresentException

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

# ---------------------------------------------------------
# 0. 날짜(한국시간 기준) 및 환경 설정
# ---------------------------------------------------------
KST = timezone(timedelta(hours=9))
NOW_KST = datetime.now(KST)
TODAY_KO = f"{NOW_KST.month}월 {NOW_KST.day}일"
TODAY_EN = NOW_KST.strftime("%B %d, %Y")

LOG_DIR = os.path.abspath("./logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
for noisy in ("httpx", "google_genai", "urllib3", "googleapiclient"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("autopost")

env = os.environ.get
GEMINI_API_KEY = env("GEMINI_API_KEY")
IMAGE_SOURCE = (env("IMAGE_SOURCE", "free_ai") or "free_ai").lower()
UNSPLASH_ACCESS_KEY = env("UNSPLASH_ACCESS_KEY")

# 티스토리 설정
TISTORY_IT_BLOG_NAME = env("TISTORY_IT_BLOG_NAME") or "aaroneom"
TISTORY_FAMILY_BLOG_NAME = env("TISTORY_FAMILY_BLOG_NAME") or "lhimepapa"
TISTORY_COOKIES_JSON = env("TISTORY_COOKIES_JSON")

# Blogger API 설정
BLOGGER_ID = env("BLOGGER_ID")
BLOGGER_TOKEN_JSON = env("BLOGGER_TOKEN_JSON")
BLOGGER_TOKEN_PATH = "token.json"

# WordPress API 설정
WORDPRESS_URL = (env("WORDPRESS_URL") or "").rstrip("/")
WORDPRESS_USER = env("WORDPRESS_USER")
WORDPRESS_APP_PASS = env("WORDPRESS_APP_PASS")

# Velog API 설정
VELOG_ACCESS_TOKEN = env("VELOG_ACCESS_TOKEN")
VELOG_REFRESH_TOKEN = env("VELOG_REFRESH_TOKEN")

# 텔레그램 알림 설정
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

# 기발행 이력 관리 파일
HISTORY_PATH = "posted_history.json"

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version="v1beta")
) if GEMINI_API_KEY else None


# ---------------------------------------------------------
# 1. 스마트 모델 폴백 호출기 (429 / 503 자동 방어 + Temperature 제어)
# ---------------------------------------------------------
def call_gemini_smart(contents, use_search=True, temperature=0.7):
    tools = [types.Tool(google_search=types.GoogleSearch())] if use_search else None
    cfg = types.GenerateContentConfig(
        tools=tools,
        temperature=temperature,
    )
    errors = []
    for model_name in ["gemini-2.5-flash", "gemini-2.5-flash-lite"]:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=cfg,
            )
            if not getattr(resp, "text", None):
                raise ValueError("빈 응답 반환")
            log.info("   🤖 Gemini 응답 성공 (모델: %s, temp: %.2f)", model_name, temperature)
            return resp
        except Exception as e:
            err_detail = f"{model_name} -> {e}"
            log.warning("   ⚠️ [Gemini Warning] %s", err_detail)
            errors.append(err_detail)
            time.sleep(3)
    raise RuntimeError("Gemini 원고 생성 실패:\n" + "\n".join(errors))


def strip_non_bmp(text):
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF)


def md_to_plain(md_text):
    t = re.sub(r"!\[.*?\]\(.*?\)", "", md_text)
    t = re.sub(r"^\*(?:사진|이미지|Image):.*$", "", t, flags=re.M)
    t = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", t)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__|\*|`)", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ---------------------------------------------------------
# 2. 무료 AI 이미지 + DuckDuckGo(ddgs) 정밀 사진 + 스톡 폴백
# ---------------------------------------------------------
_used_images = set()


def _is_image_reachable(url):
    try:
        r = requests.get(
            url, stream=True, timeout=30,
            headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-1023"}
        )
        ok = r.status_code < 400 and "image" in r.headers.get("Content-Type", "")
        r.close()
        return ok
    except requests.RequestException:
        return False


def get_free_ai_image(en_prompt):
    seed = random.randint(1000, 999999)
    full_prompt = f"{en_prompt}, editorial illustration, clean modern style, high quality, no text, no watermark"
    url = f"https://image.pollinations.ai/prompt/{quote(full_prompt)}?width=1200&height=675&model=flux&seed={seed}&nologo=true"
    log.info("   🎨 무료 AI 이미지 생성: '%s'", en_prompt)
    time.sleep(3)
    if _is_image_reachable(url):
        return url, "AI Illustration (Pollinations.ai)"
    raise RuntimeError("Pollinations 응답 지연")


def _search_duckduckgo(q):
    if not DDGS:
        return None
    try:
        search_query = q if len(q.split()) > 1 else f"Korea {q}"
        log.info("   🔍 DuckDuckGo 실제 정밀 사진 검색: '%s'", search_query)
        with DDGS() as ddgs:
            results = list(ddgs.images(search_query, max_results=8))
            for r in results:
                url = r.get("image")
                if url and url not in _used_images and _is_image_reachable(url):
                    _used_images.add(url)
                    return url, f"Real Photo ({q} via DuckDuckGo)"
    except Exception as e:
        log.warning("   ⚠️ DuckDuckGo 이미지 검색 제한(RateLimit) 또는 에러 발생: %s", e)
    return None


def _search_unsplash(q):
    if not UNSPLASH_ACCESS_KEY:
        return None
    try:
        r = requests.get(
            "https://api.unsplash.com/search/photos",
            headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}", "Accept-Version": "v1"},
            params={"query": q, "per_page": 5, "orientation": "landscape"},
            timeout=10,
        )
        r.raise_for_status()
        for p in r.json().get("results", []):
            url = p.get("urls", {}).get("regular")
            if url and url not in _used_images:
                _used_images.add(url)
                return url, f"Photo by {p.get('user', {}).get('name', 'Creator')} on Unsplash"
    except Exception:
        pass
    return None


def _search_wikimedia(q):
    try:
        r = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            headers={"User-Agent": "auto-blog-poster/1.0"},
            timeout=12,
            params={
                "action": "query", "generator": "search", "gsrnamespace": 6,
                "gsrsearch": f"{q} filetype:bitmap", "gsrlimit": 8,
                "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
                "iiurlwidth": 1200, "format": "json", "origin": "*",
            },
        )
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages", {})
        words = [w.lower() for w in q.split() if len(w) > 3]
        for p in sorted(pages.values(), key=lambda x: x.get("index", 0)):
            ii = (p.get("imageinfo") or [{}])[0]
            if ii.get("mime") not in ("image/jpeg", "image/png"):
                continue
            meta = ii.get("extmetadata", {})
            lic = meta.get("LicenseShortName", {}).get("value", "")
            if not (lic.upper().startswith("CC") or "public domain" in lic.lower()):
                continue
            url = ii.get("thumburl") or ii.get("url")
            title_clean = p.get("title", "").lower()
            if url and url not in _used_images and any(w in title_clean for w in words):
                _used_images.add(url)
                return url, f"Wikimedia Commons ({lic})"
    except Exception:
        pass
    return None


def get_image(en_keyword):
    if IMAGE_SOURCE == "none":
        return None
    
    if IMAGE_SOURCE == "free_ai":
        try:
            return get_free_ai_image(en_keyword)
        except Exception as e:
            log.warning("   ⚠️ 무료 AI 이미지 실패 → 웹 실제 사진(DuckDuckGo) 검색 시도: %s", e)
    
    try:
        res = _search_duckduckgo(en_keyword)
        if res:
            return res
    except Exception:
        pass

    for fn in (_search_unsplash, _search_wikimedia):
        try:
            res = fn(en_keyword)
            if res:
                return res
        except Exception:
            pass
            
    return None


# ---------------------------------------------------------
# 3. 기발행 이력 관리 & 나들이 권역/테마 자동 로테이션
# ---------------------------------------------------------
def load_posted_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("family_trip", [])
                    data.setdefault("it_easy", [])
                    return data
        except Exception as e:
            log.warning("   ⚠️ 기발행 이력 로드 실패 (초기화): %s", e)
    return {"family_trip": [], "it_easy": []}


def save_posted_history(history):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("   ⚠️ 기발행 이력 저장 실패: %s", e)


TRIP_THEMES = [
    "서울 도심 속 숨은 박물관, 미술관 또는 역사 유적지 체험",
    "경기도 남부(용인, 수원, 화성, 이천, 안성 등) 체험농장, 수목원 또는 테마파크",
    "경기도 북부(파주, 포천, 양주, 가평, 남양주 등) 자연 생태공원, 숲체험 또는 이색 전시관",
    "인천(송도, 영종도, 강화도 등) 바다·갯벌 체험, 해양박물관 또는 공원 나들이",
    "강원도/충청권(춘천, 원주, 천안, 아산, 태안 등) 당일치기 또는 1박 2일 가족 여행지",
    "아이들이 직접 몸으로 뛰고 직업·과학·안전 체험을 할 수 있는 실내 키즈 체험관",
    "계절에 맞는 전통문화 마을, 식물원, 동물 교감 체험장 또는 공방 원데이클래스",
]
TODAY_TRIP_THEME = TRIP_THEMES[NOW_KST.toordinal() % len(TRIP_THEMES)]


# ---------------------------------------------------------
# 4. 3가지 핵심 카테고리 (동적 시의성 태그 추출 프롬프트 탑재)
# ---------------------------------------------------------
POST_CATEGORIES = {
    "it_news": {
        "name": "1. 오늘의 Tech Top 10 & 전문가 논평",
        "tistory_blog": TISTORY_IT_BLOG_NAME,
        "allow_velog": True,
        "use_search": True,
        "default_tags_ko": ["오늘의테크뉴스", "IT트렌드", "글로벌IT이슈", "테크논평", "AI뉴스"],
        "default_tags_en": ["TechNews", "TechTop10", "ITInsights", "GlobalTech", "EnterpriseIT"],
        "prompt": f"""
당신은 글로벌 IT 인프라·클라우드·네트워크 업계에서 14년 넘게 현장을 뛰어온 IT 전문가입니다.
Google 검색 도구를 사용하여 오늘({TODAY_KO}, 2026년 기준) 전 세계 및 한국 IT·테크 업계에서 실시간으로 가장 뜨겁게 보도되고 있는 신선하고 다양한 핵심 뉴스 10가지와 전문가 논평을 작성하세요.

[출력 형식 및 규칙]
- 본문 중간에 `[IMG: 본문 속 핵심 기술명이나 기업명 및 제품명 (예: NVIDIA AI server rack, Samsung CXL memory)]` 형태의 태그를 정확히 2개 삽입하세요.
- **절대로 추상적이거나 무관한 단어를 쓰지 말고, 본문에서 방금 다룬 실제 기업이나 기술 영문/한글 명칭을 태그 안에 반드시 포함하세요.**
- 글 하단에 이 글을 대표하는 핵심 검색 태그(오늘의 주요 이슈인 젠슨황, 엔비디아, 특정 기술명 등 포함)를 한국어 5개 내외, 영어 5개 내외 각각 쉼표로 구분하여 `TAGS: ...` 형식으로 작성하세요.

===KO_START===
TITLE: 한국어 제목
(한국어 마크다운 본문 - [IMG: 구체적 기술명/기업명 영문] 태그 2개 포함)
TAGS: 젠슨황, 엔비디아, 인공지능, 반도체뉴스, IT트렌드
===EN_START===
TITLE: English Title
(영어 마크다운 본문 - 한국어와 동일한 위치에 동일한 [IMG: 구체적 기술명/기업명 영문] 태그 2개 포함)
TAGS: JensenHuang, NVIDIA, ArtificialIntelligence, TechNews, ITInsights
"""
    },
    "it_easy": {
        "name": "2. 초보자·아이 눈높이 최신 테크 상식",
        "tistory_blog": TISTORY_IT_BLOG_NAME,
        "allow_velog": True,
        "use_search": True,
        "default_tags_ko": ["테크상식", "쉬운IT", "IT용어정리", "신기술트렌드", "초보자가이드"],
        "default_tags_en": ["TechExplained", "TechBasics", "EasyIT", "FutureTech", "TechGlossary"],
        "prompt": f"""
당신은 복잡한 최신 IT·반도체·AI 기술을 초등학생 아이와 비전공자도 단번에 이해할 수 있도록 재미있게 풀어내는 IT 스토리텔러입니다.
Google 검색을 활용하여 최근 테크 및 과학계에서 새롭게 주목받고 있는 신기술 또는 트렌드 키워드 1가지를 선정해 소개하세요.

[출력 형식 및 규칙]
- 본문 중간에 `[IMG: 해당 기술의 실제 명칭 및 관련 기기/웨이퍼 (예: glass substrate wafer, quantum computer processor)]` 형태의 태그를 정확히 2개 삽입하세요.
- **선정된 기술의 실제 시각적 대상이 검색되도록 구체적인 영문 키워드를 작성하세요.**
- 글 하단에 이 글을 대표하는 핵심 검색 태그를 한국어 5개 내외, 영어 5개 내외 각각 쉼표로 구분하여 `TAGS: ...` 형식으로 작성하세요.

===KO_START===
TITLE: 한국어 제목
(한국어 마크다운 본문 - [IMG: 구체적 기술 영문 키워드] 태그 2개 포함)
TAGS: 테크상식, 쉬운IT, 신기술, 양자컴퓨터, AI기초
===EN_START===
TITLE: English Title
(영어 마크다운 본문 - 한국어와 동일한 위치에 동일한 [IMG: 구체적 기술 영문 키워드] 태그 2개 포함)
TAGS: TechExplained, EasyIT, FutureTech, QuantumComputing, TechBasics
"""
    },
    "family_trip": {
        "name": "3. 7살 딸·5살 아들 아빠의 주말 나들이 정복기",
        "tistory_blog": TISTORY_FAMILY_BLOG_NAME,
        "allow_velog": False,  # 주말 나들이 글은 벨로그 미발행
        "use_search": True,
        "default_tags_ko": ["주말나들이", "아이와가볼만한곳", "가족여행", "남매육아", "주말코스추천"],
        "default_tags_en": ["KoreaFamilyTravel", "KidsActivitiesKorea", "WeekendTripKorea", "FamilyOuting", "TravelWithKids"],
        "prompt": f"""
당신은 7살 딸과 5살 아들 아빠입니다. 대한민국에서 7살·5살 남매와 함께 주말에 다녀오기 좋은 실제 나들이 명소 1~2곳을 선정해 실전 정복기를 작성하세요.

[오늘의 필수 선정 테마]
- 반드시 **'{TODAY_TRIP_THEME}'** 테마에 해당하는 실제 장소를 새롭게 발굴하여 선정하세요.
- 뻔한 대형 프랜차이즈 아쿠아리움이나 과천국립과학관 대신, 7살·5살 남매가 실제로 좋아할 만한 구체적이고 특색 있는 코스를 추천하세요.

[출력 형식 및 규칙]
- 본문 중간에 `[IMG: 실제 장소명 영문 + Korea (예: Seoul Children's Grand Park Korea, Paju Heyri Art Valley Korea)]` 형태의 태그를 정확히 2개 삽입하세요.
- **절대로 추상적인 단어(family playing 등)를 쓰지 말고, 본문에서 소개한 실제 나들이 장소의 영문 이름과 'Korea'를 조합하여 태그를 만드세요.**
- 글 하단에 이 글을 대표하는 핵심 검색 태그(선정된 실제 장소명 포함)를 한국어 5개 내외, 영어 5개 내외 각각 쉼표로 구분하여 `TAGS: ...` 형식으로 작성하세요.

===KO_START===
TITLE: 한국어 제목
(한국어 마크다운 본문 - [IMG: 실제 장소명 영문 + Korea] 태그 2개 포함)
TAGS: 주말나들이, 아이와가볼만한곳, 선정장소명, 가족여행, 남매육아
===EN_START===
TITLE: English Title
(영어 마크다운 본문 - 한국어와 동일한 위치에 동일한 [IMG: 실제 장소명 영문 + Korea] 태그 2개 포함)
TAGS: KoreaFamilyTravel, KidsActivitiesKorea, SelectedPlaceName, WeekendTrip, FamilyOuting
"""
    }
}


def parse_block(block_text):
    lines = block_text.splitlines()
    title = None
    tags = []
    body_lines = []
    
    for line in lines:
        line_str = line.strip()
        if not title and re.match(r"^[#*\s]*TITLE\s*[:：]\s*(.+?)[*\s]*$", line_str, re.I):
            m = re.match(r"^[#*\s]*TITLE\s*[:：]\s*(.+?)[*\s]*$", line_str, re.I)
            title = m.group(1).strip()
            continue
        if re.match(r"^[#*\s]*TAGS\s*[:：]\s*(.+?)[*\s]*$", line_str, re.I):
            m = re.match(r"^[#*\s]*TAGS\s*[:：]\s*(.+?)[*\s]*$", line_str, re.I)
            tags = [t.strip() for t in m.group(1).split(",") if t.strip()]
            continue
        body_lines.append(line)
        
    if not title and lines:
        title = re.sub(r"^[#*\s]+|[*\s]+$", "", lines[0])
        body_lines = lines[1:]
        
    return title, "\n".join(body_lines).strip(), tags


def split_bilingual_raw(raw_text):
    """Gemini 응답에서 한국어(KO)와 영어(EN) 블록을 다중 방어 로직으로 안전하게 분리"""
    cleaned = re.sub(r"^[#*\s]*===\s*KO_START\s*===[*\s]*", "", raw_text, flags=re.M | re.I).strip()

    # 1차: ===EN_START=== 구분자 분리 (앞뒤 공백, 대소문자, 마크다운 기호 #, * 허용)
    en_split = re.split(r"\n[#*\s]*===\s*EN_START\s*===[*\s]*\n?", cleaned, maxsplit=1, flags=re.I)
    if len(en_split) == 2:
        return en_split[0].strip(), en_split[1].strip()

    # 2차: 구분자 누락 시 두 번째 TITLE: 라인을 기준으로 분리
    title_matches = list(re.finditer(r"^[#*\s]*TITLE\s*[:：]", cleaned, flags=re.M | re.I))
    if len(title_matches) >= 2:
        split_idx = title_matches[1].start()
        log.warning("   ⚠️ ===EN_START=== 구분자 누락 감지 → 두 번째 TITLE 위치에서 자동 분리 수행")
        return cleaned[:split_idx].strip(), cleaned[split_idx:].strip()

    log.warning("   ⚠️ 영문 블록 분리 실패: 단일 블록으로 처리됩니다.")
    return cleaned, ""


def generate_bilingual_posts(category_key):
    cat = POST_CATEGORIES[category_key]
    history = load_posted_history()
    past_items = history.get(category_key, [])

    exclude_instruction = ""
    if past_items:
        recent_list = " / ".join(past_items[-30:])
        exclude_instruction = f"""
[중복 금지 - 절대 제외 목록]
아래 제목/장소/주제는 이미 블로그에 발행했으므로 **절대 다시 선정하지 말고** 완전히 새로운 대상을 고르세요:
- 기발행 목록: {recent_list}
"""

    prompt = f"""
{cat['prompt']}
{exclude_instruction}
[절대 금지 사항 및 출력 규칙]
- 반드시 `===KO_START===` 와 `===EN_START===` 구분자를 마크다운 기호(`**`, `###` 등)나 공백 없이 단독 줄에 정확히 출력하세요.
- 한국어 글과 영어 글은 각각 독립된 완전한 게시글 형태로 작성하세요.
- 괄호형 메모 `(확인 필요)` 등 절대 출력 금지.
- AI 번역투("새로운 지평", "결론적으로", "In conclusion", "delve into") 금지.
- 가상의 화자 이름(김대현 등) 금지.
"""
    temp = 1.05 if category_key == "family_trip" else 0.7
    resp = call_gemini_smart(prompt, use_search=cat.get("use_search", True), temperature=temp)
    raw = (resp.text or "").strip()
    raw = re.sub(r"\([^)]*확인\s*필요[^)]*\)", "", raw)

    ko_part, en_part = split_bilingual_raw(raw)

    title_ko, md_ko_raw, tags_ko = parse_block(ko_part)
    title_en, md_en_raw, tags_en = parse_block(en_part) if en_part else ("", "", [])

    if not tags_ko:
        tags_ko = cat.get("default_tags_ko", ["IT트렌드", "테크뉴스"])
    if not tags_en:
        tags_en = cat.get("default_tags_en", ["TechNews", "ITInsights"])

    if category_key in history and title_ko:
        history[category_key].append(title_ko)
        history[category_key] = history[category_key][-50:]
        save_posted_history(history)

    img_tags = re.findall(r"\[IMG:\s*(.+?)\]", md_ko_raw)
    resolved_imgs = []
    for kw in img_tags[:2]:
        img = get_image(kw.strip())
        if img:
            url, credit = img
            resolved_imgs.append(f"\n\n![{kw.strip()}]({url})\n\n*Image: {credit}*\n\n")
        else:
            resolved_imgs.append("")

    def apply_images(md_text):
        idx = 0
        def _rep(m):
            nonlocal idx
            val = resolved_imgs[idx] if idx < len(resolved_imgs) else ""
            idx += 1
            return val
        return re.sub(r"\[IMG:\s*(.+?)\]", _rep, md_text)

    md_ko = apply_images(md_ko_raw)
    md_en = apply_images(md_en_raw) if md_en_raw else ""
    return title_ko, md_ko, tags_ko, title_en, md_en, tags_en


def md_to_styled_html(md_text):
    html_body = markdown.markdown(md_text, extensions=["extra", "nl2br"])
    html_body = html_body.replace(
        "<img ",
        '<img style="max-width:100%;height:auto;border-radius:8px;margin:16px 0;display:block;" '
    )
    html_body = html_body.replace(
        "<h2>",
        '<h2 style="margin-top:32px;margin-bottom:14px;font-size:1.35em;border-bottom:1px solid #e5e7eb;padding-bottom:6px;">'
    )
    return html_body


# ---------------------------------------------------------
# 5. 티스토리 웹 자동화 (쿠키 딕셔너리/리스트 2중 호환 + TinyMCE 직접 본문 삽입)
# ---------------------------------------------------------
def init_headless_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
    options.add_argument("--disable-blink-features=AutomationControlled")
    return webdriver.Chrome(options=options)


def inject_cookies_for_domain(driver, target_url):
    if not TISTORY_COOKIES_JSON:
        raise RuntimeError("TISTORY_COOKIES_JSON 시크릿 누락")
    
    try:
        cookies = json.loads(TISTORY_COOKIES_JSON)
    except Exception as e:
        raise RuntimeError(f"TISTORY_COOKIES_JSON JSON 파싱 실패: {e}")

    driver.get(target_url)
    time.sleep(2)

    if isinstance(cookies, dict):
        for name, value in cookies.items():
            try:
                driver.add_cookie({"name": name, "value": value, "path": "/"})
            except Exception:
                pass
    elif isinstance(cookies, list):
        for c in cookies:
            if not isinstance(c, dict):
                continue
            cookie_dict = {"name": c.get("name"), "value": c.get("value"), "path": c.get("path", "/")}
            if "domain" in c and "tistory.com" in c["domain"]:
                cookie_dict["domain"] = c["domain"]
            try:
                driver.add_cookie(cookie_dict)
            except Exception:
                pass


def post_to_tistory(driver, blog_name, title, html_body, tags):
    if not blog_name:
        raise RuntimeError("대상 티스토리 블로그 이름 누락")

    log.info("   🎯 대상 티스토리: https://%s.tistory.com", blog_name)
    inject_cookies_for_domain(driver, "https://www.tistory.com")
    inject_cookies_for_domain(driver, f"https://{blog_name}.tistory.com")

    driver.get(f"https://{blog_name}.tistory.com/manage/newpost/?type=post")
    time.sleep(3)

    try:
        driver.switch_to.alert.dismiss()
    except NoAlertPresentException:
        pass

    if any(m in driver.current_url for m in ["accounts.kakao.com", "/auth/login", "tistory.com/login"]):
        raise RuntimeError("티스토리 쿠키 만료. export_cookies.py로 갱신 필요.")

    title_inp = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#post-title-inp")))
    title_inp.clear()
    title_inp.send_keys(strip_non_bmp(title))
    time.sleep(0.5)

    safe_html = strip_non_bmp(html_body)

    injected = driver.execute_script(
        """
        if (window.tinymce && tinymce.activeEditor) {
            tinymce.activeEditor.setContent(arguments[0]);
            tinymce.activeEditor.fire('change');
            tinymce.activeEditor.save();
            return tinymce.activeEditor.getContent().length;
        }
        return -1;
        """,
        safe_html,
    )

    if injected and injected > 0:
        log.info("   📝 TinyMCE 에디터 직접 본문 주입 성공: %d자", injected)
    else:
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#editor-mode-layer-btn-open"))).click()
        time.sleep(0.5)
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#editor-mode-html"))).click()
        time.sleep(1)
        try:
            driver.switch_to.alert.accept()
        except NoAlertPresentException:
            pass
        time.sleep(1)

        n = driver.execute_script(
            """
            var cmEl = document.querySelector('.CodeMirror');
            if (!cmEl || !cmEl.CodeMirror) return -1;
            cmEl.CodeMirror.setValue(arguments[0]);
            cmEl.CodeMirror.save();
            return cmEl.CodeMirror.getValue().length;
            """,
            safe_html,
        )
        log.info("   📝 HTML 코드 주입: %d자", n)
        time.sleep(0.5)

        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#editor-mode-layer-btn-open"))).click()
        time.sleep(0.5)
        driver.execute_script(
            """
            var el = document.querySelector('#editor-mode-wysiwyg')
                  || document.querySelector('#editor-mode-layer [id*="wysiwyg"]')
                  || document.querySelector('#editor-mode-layer li:first-child, #editor-mode-layer div[role="menuitem"]:first-child');
            if (el) el.click();
            """
        )
        time.sleep(1)
        try:
            driver.switch_to.alert.accept()
        except NoAlertPresentException:
            pass
        time.sleep(1)

    try:
        tag_inp = driver.find_element(By.CSS_SELECTOR, "#tagText")
        for t in tags[:5]:
            tag_inp.send_keys(strip_non_bmp(t))
            tag_inp.send_keys(Keys.ENTER)
            time.sleep(0.2)
    except Exception:
        pass

    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#publish-layer-btn"))).click()
    time.sleep(1)
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#open20"))).click()
    time.sleep(0.5)
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#publish-btn"))).click()

    WebDriverWait(driver, 20).until(lambda d: "newpost" not in d.current_url)
    return driver.current_url


# ---------------------------------------------------------
# 6. 멀티 API 플랫폼 (Blogger / WordPress / Velog)
# ---------------------------------------------------------
def post_to_blogger(title, html_body, tags):
    if BLOGGER_TOKEN_JSON and not os.path.exists(BLOGGER_TOKEN_PATH):
        with open(BLOGGER_TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(BLOGGER_TOKEN_JSON)
    if not BLOGGER_ID or not os.path.exists(BLOGGER_TOKEN_PATH):
        raise RuntimeError("BLOGGER_ID 또는 BLOGGER_TOKEN_JSON 누락")

    creds = Credentials.from_authorized_user_file(BLOGGER_TOKEN_PATH, ["https://www.googleapis.com/auth/blogger"])
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    service = build("blogger", "v3", credentials=creds, cache_discovery=False)
    body = {"kind": "blogger#post", "title": title, "content": html_body, "labels": tags}
    res = service.posts().insert(blogId=BLOGGER_ID, body=body, isDraft=False).execute()
    return res.get("url")


def post_to_wordpress(title, html_body):
    if not (WORDPRESS_URL and WORDPRESS_USER and WORDPRESS_APP_PASS):
        raise RuntimeError("WordPress 설정 누락")
    res = requests.post(
        f"{WORDPRESS_URL}/wp-json/wp/v2/posts",
        json={"title": title, "content": html_body, "status": "publish"},
        auth=(WORDPRESS_USER, WORDPRESS_APP_PASS),
        timeout=30,
    )
    if res.status_code != 201:
        raise RuntimeError(f"WordPress HTTP {res.status_code}: {res.text[:150]}")
    return res.json().get("link")


def post_to_velog(title, md_body, tags):
    if not VELOG_REFRESH_TOKEN:
        raise RuntimeError("VELOG_REFRESH_TOKEN 누락")
    url = "https://v2.velog.io/graphql"
    cookie = f"refresh_token={VELOG_REFRESH_TOKEN}"
    if VELOG_ACCESS_TOKEN:
        cookie = f"access_token={VELOG_ACCESS_TOKEN}; " + cookie
    headers = {"Cookie": cookie, "Content-Type": "application/json", "Origin": "https://velog.io", "Referer": "https://velog.io/"}

    r1 = requests.post(url, json={"query": "query { __typename }"}, headers=headers, timeout=20)
    new_access = r1.cookies.get_dict().get("access_token")
    if new_access:
        headers["Cookie"] = f"access_token={new_access}; refresh_token={VELOG_REFRESH_TOKEN}"

    query = """
    mutation WritePost($title: String, $body: String, $tags: [String], $is_markdown: Boolean,
                       $is_temp: Boolean, $is_private: Boolean, $url_slug: String, $meta: JSON!) {
        writePost(title: $title, body: $body, tags: $tags, is_markdown: $is_markdown,
                  is_temp: $is_temp, is_private: $is_private, url_slug: $url_slug, meta: $meta) {
            url_slug
            user { username }
        }
    }
    """
    slug = re.sub(r"[^0-9a-zA-Z가-힣]+", "-", title).strip("-")[:50] + f"-{int(time.time())}"
    variables = {"title": title, "body": md_body, "tags": tags[:5], "is_markdown": True, "is_temp": False, "is_private": False, "url_slug": slug, "meta": {}}
    res = requests.post(url, json={"query": query, "variables": variables}, headers=headers, timeout=30).json()
    post = (res.get("data") or {}).get("writePost")
    if not post:
        raise RuntimeError(f"Velog 발행 실패: {str(res)[:200]}")
    return f"https://velog.io/@{post['user']['username']}/{post['url_slug']}"


# ---------------------------------------------------------
# 7. 텔레그램 발행 리포트 발송기
# ---------------------------------------------------------
def send_telegram_report(results, target_count=18):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("⚠️ 텔레그램 환경변수(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) 누락으로 리포트 전송을 건너뜁니다.")
        return

    ok_items = [v for v in results.values() if v["status"] == "OK"]
    fail_items = [v for v in results.values() if v["status"] == "FAIL"]

    lines = [
        f"📊 <b>[{TODAY_KO}] 블로그 자동 발행 리포트</b>",
        f"✅ 성공: <b>{len(ok_items)}건</b> / ❌ 실패: <b>{len(fail_items)}건</b> (목표: {target_count}건)",
        ""
    ]

    platform_groups = {}
    for item in results.values():
        platform_groups.setdefault(item["platform"], []).append(item)

    for platform, items in platform_groups.items():
        ok_cnt = sum(1 for i in items if i["status"] == "OK")
        lines.append(f"<b>▸ {html.escape(platform)} ({ok_cnt}/{len(items)})</b>")
        for it in items:
            safe_title = html.escape(it["title"])
            if it["status"] == "OK":
                lines.append(f"  ✅ [{it['lang']}] <a href='{it['info']}'>{safe_title}</a>")
            else:
                safe_err = html.escape(str(it["info"])[:150])
                lines.append(f"  ❌ [{it['lang']}] {safe_title}")
                lines.append(f"     └ <i>사유: {safe_err}</i>")
        lines.append("")

    full_text = "\n".join(lines).strip()
    chunks = [full_text[i:i + 3800] for i in range(0, len(full_text), 3800)]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for chunk in chunks:
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            r.raise_for_status()
            log.info("📨 텔레그램 발행 리포트 전송 완료")
        except Exception as e:
            log.error("🚨 텔레그램 리포트 전송 실패: %s", e)


# ---------------------------------------------------------
# 8. 메인 컨트롤러
# ---------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="all", help="all | it_news | it_easy | family_trip")
    ap.add_argument("--platforms", default="all", help="all | tistory,blogger,wordpress,velog")
    ap.add_argument("--langs", default="ko,en", help="ko,en | ko | en")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not client:
        raise SystemExit("GEMINI_API_KEY 시크릿이 없습니다.")

    raw_platforms = [p.strip().lower() for p in args.platforms.split(",") if p.strip()]
    targets = ["tistory", "blogger", "wordpress", "velog"] if "all" in raw_platforms else raw_platforms
    langs = [l.strip().lower() for l in args.langs.split(",") if l.strip()]
    categories = list(POST_CATEGORIES.keys()) if args.category == "all" else [args.category]

    driver = None
    if not args.dry_run and "tistory" in targets:
        driver = init_headless_driver()

    results = {}
    try:
        for cat_key in categories:
            if cat_key not in POST_CATEGORIES:
                continue
            cat = POST_CATEGORIES[cat_key]
            log.info("\n==================================================")
            log.info("✍️  [%s] 한/영 듀얼 원고 생성 시작 (1회 호출 절약 모드)", cat["name"])
            if cat_key == "family_trip":
                log.info("🧭 오늘의 나들이 선정 테마: %s", TODAY_TRIP_THEME)

            try:
                title_ko, md_ko, tags_ko, title_en, md_en, tags_en = generate_bilingual_posts(cat_key)
            except Exception as e:
                log.error("🚨 [%s] 원고 생성(Gemini API) 실패: %s", cat["name"], e)
                results[f"{cat_key}:gemini"] = {
                    "platform": "Gemini 원고 생성",
                    "lang": "KO/EN",
                    "title": cat["name"],
                    "status": "FAIL",
                    "info": f"원고 생성 단계 실패: {str(e)}",
                }
                continue

            log.info("📌 [KO] 제목: %s | 태그: %s", title_ko, ", ".join(tags_ko))
            if title_en:
                log.info("📌 [EN] Title: %s | Tags: %s", title_en, ", ".join(tags_en))

            posts_to_publish = []
            if "ko" in langs and title_ko:
                posts_to_publish.append(("KO", title_ko, md_ko, md_to_styled_html(md_ko), tags_ko))
            if "en" in langs and title_en:
                posts_to_publish.append(("EN", title_en, md_en, md_to_styled_html(md_en), tags_en))

            for lang_code, title, md_body, html_body, tags in posts_to_publish:
                if args.dry_run:
                    print(f"\n=== [{cat_key} | {lang_code}] {title} ===\nTags: {tags}\n{md_body[:500]}...\n")
                    continue

                if "tistory" in targets and driver:
                    p_label = f"티스토리 ({cat['tistory_blog']})"
                    try:
                        url = post_to_tistory(driver, cat["tistory_blog"], title, html_body, tags)
                        results[f"{cat_key}[{lang_code}]:tistory({cat['tistory_blog']})"] = {
                            "platform": p_label, "lang": lang_code, "title": title, "status": "OK", "info": url
                        }
                        log.info("✅ [티스토리:%s][%s] 발행 완료", cat["tistory_blog"], lang_code)
                    except Exception as e:
                        results[f"{cat_key}[{lang_code}]:tistory({cat['tistory_blog']})"] = {
                            "platform": p_label, "lang": lang_code, "title": title, "status": "FAIL", "info": str(e)
                        }
                        log.error("❌ [티스토리:%s][%s] 실패: %s", cat["tistory_blog"], lang_code, e)
                        driver.save_screenshot(os.path.join(LOG_DIR, f"fail_tistory_{cat_key}_{lang_code}.png"))

                if "blogger" in targets and BLOGGER_ID:
                    try:
                        url = post_to_blogger(title, html_body, tags)
                        results[f"{cat_key}[{lang_code}]:blogger"] = {
                            "platform": "Blogger", "lang": lang_code, "title": title, "status": "OK", "info": url
                        }
                        log.info("✅ [Blogger][%s] 발행 완료: %s", lang_code, url)
                    except Exception as e:
                        results[f"{cat_key}[{lang_code}]:blogger"] = {
                            "platform": "Blogger", "lang": lang_code, "title": title, "status": "FAIL", "info": str(e)
                        }
                        log.error("❌ [Blogger][%s] 실패: %s", lang_code, e)

                if "wordpress" in targets and WORDPRESS_URL:
                    try:
                        url = post_to_wordpress(title, html_body)
                        results[f"{cat_key}[{lang_code}]:wordpress"] = {
                            "platform": "WordPress", "lang": lang_code, "title": title, "status": "OK", "info": url
                        }
                        log.info("✅ [WordPress][%s] 발행 완료: %s", lang_code, url)
                    except Exception as e:
                        results[f"{cat_key}[{lang_code}]:wordpress"] = {
                            "platform": "WordPress", "lang": lang_code, "title": title, "status": "FAIL", "info": str(e)
                        }
                        log.error("❌ [WordPress][%s] 실패: %s", lang_code, e)

                if "velog" in targets and cat["allow_velog"] and VELOG_REFRESH_TOKEN:
                    try:
                        url = post_to_velog(title, md_body, tags)
                        results[f"{cat_key}[{lang_code}]:velog"] = {
                            "platform": "Velog", "lang": lang_code, "title": title, "status": "OK", "info": url
                        }
                        log.info("✅ [Velog][%s] 발행 완료: %s", lang_code, url)
                    except Exception as e:
                        results[f"{cat_key}[{lang_code}]:velog"] = {
                            "platform": "Velog", "lang": lang_code, "title": title, "status": "FAIL", "info": str(e)
                        }
                        log.error("❌ [Velog][%s] 실패: %s", lang_code, e)

                time.sleep(6)
    finally:
        if driver:
            driver.quit()

    if not args.dry_run:
        print("\n===== 최종 멀티 플랫폼 & 한/영 발행 요약 =====")
        for k, item in results.items():
            print(f"{'✅' if item['status'] == 'OK' else '❌'} {k:38s} -> {item['info']}")

        send_telegram_report(results, target_count=18)

        if any(item["status"] == "FAIL" for item in results.values()):
            sys.exit(1)


if __name__ == "__main__":
    main()
