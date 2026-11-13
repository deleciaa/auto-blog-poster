import os
import re
import sys
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
# 429 한도 초과 시 다음 모델로 자동 전환되도록 모델 리스트 구성
MODEL_CANDIDATES = [
    env("GEMINI_MODEL", "gemini-2.5-flash"),
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]
IMAGE_SOURCE = (env("IMAGE_SOURCE", "free_ai") or "free_ai").lower()
UNSPLASH_ACCESS_KEY = env("UNSPLASH_ACCESS_KEY")

TISTORY_IT_BLOG_NAME = env("TISTORY_IT_BLOG_NAME") or "aaroneom"
TISTORY_FAMILY_BLOG_NAME = env("TISTORY_FAMILY_BLOG_NAME") or "lhimepapa"
TISTORY_COOKIES_JSON = env("TISTORY_COOKIES_JSON")

BLOGGER_ID = env("BLOGGER_ID")
BLOGGER_TOKEN_JSON = env("BLOGGER_TOKEN_JSON")
BLOGGER_TOKEN_PATH = "token.json"

WORDPRESS_URL = (env("WORDPRESS_URL") or "").rstrip("/")
WORDPRESS_USER = env("WORDPRESS_USER")
WORDPRESS_APP_PASS = env("WORDPRESS_APP_PASS")

VELOG_ACCESS_TOKEN = env("VELOG_ACCESS_TOKEN")
VELOG_REFRESH_TOKEN = env("VELOG_REFRESH_TOKEN")

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version="v1beta")
) if GEMINI_API_KEY else None


# ---------------------------------------------------------
# 1. 스마트 모델 폴백 호출기 (429 / 503 자동 방어)
# ---------------------------------------------------------
def call_gemini_smart(contents, use_search=False):
    cfg = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]) if use_search else None
    last_err = None
    for model_name in dict.fromkeys(MODEL_CANDIDATES):
        for attempt in range(2):
            try:
                return client.models.generate_content(model=model_name, contents=contents, config=cfg)
            except Exception as e:
                last_err = e
                msg = str(e)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    log.warning("   ⚠️ [%s] 일일/분당 한도 초과 → 다음 모델로 즉시 전환", model_name)
                    break  # 다음 모델로 이동
                log.warning("   ⚠️ [%s] 일시 오류 재시도 (%d/2): %s", model_name, attempt + 1, msg[:120])
                time.sleep(3)
    raise last_err


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
# 2. 무료 AI 이미지 + 스톡 폴백
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


def _search_unsplash(q):
    if not UNSPLASH_ACCESS_KEY:
        return None
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
    return None


def _search_wikimedia(q):
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
    return None


def get_image(en_keyword):
    if IMAGE_SOURCE == "none":
        return None
    if IMAGE_SOURCE == "free_ai":
        try:
            return get_free_ai_image(en_keyword)
        except Exception as e:
            log.warning("   ⚠️ 무료 AI 이미지 실패 → 스톡 검색 전환: %s", e)
    for fn in (_search_unsplash, _search_wikimedia):
        try:
            res = fn(en_keyword)
            if res:
                return res
        except Exception:
            pass
    return None


# ---------------------------------------------------------
# 3. 3가지 핵심 카테고리 (1회 호출로 한국어+영어 동시 생성 -> API 쿼터 50% 절약)
# ---------------------------------------------------------
POST_CATEGORIES = {
    "it_news": {
        "name": "1. 오늘의 Tech Top 10 & 전문가 논평",
        "tistory_blog": TISTORY_IT_BLOG_NAME,
        "allow_velog": True,
        "tags_ko": ["오늘의테크뉴스", "IT트렌드", "글로벌IT이슈", "테크논평", "AI뉴스"],
        "tags_en": ["TechNews", "TechTop10", "ITInsights", "GlobalTech", "EnterpriseIT"],
        "prompt": f"""
당신은 글로벌 IT 인프라·클라우드·네트워크 업계에서 14년 넘게 현장을 뛰어온 IT 전문가입니다.
Google 검색 도구를 사용해 **오늘({TODAY_KO}, 2026년 기준) 전 세계 및 한국 IT·테크 업계에서 가장 화제가 되고 있는 핵심 뉴스 10가지**를 선정해 브리핑하고 현업 전문가의 논평을 작성하세요.

[제목 형식]
- 한국어 제목: `🌐 [오늘의 Tech Top 10] {TODAY_KO} 전 세계 IT·테크 핵심 뉴스`
- 영어 제목: `🌐 [Today's Tech Top 10] {TODAY_EN}: Global IT & Tech News Highlights`

[본문 구성 - 각 언어별 2,500자 이상 풍부하게]
1. 짧은 오프닝: 오늘 글로벌 테크 시장의 전반적인 흐름 한 줄 요약.
2. `## 오늘의 글로벌 Tech Top 10 핵심 브리핑` (영문: `## Today's Global Tech Top 10 Briefing`): 1번부터 10번까지 번호를 매겨 각 뉴스의 핵심 팩트와 한 줄 코멘트 정리.
3. `## 현업 IT 전문가의 심층 논평` (영문: `## Industry Veteran's Deep-Dive Commentary`): 위 뉴스 중 가장 파급력이 큰 1~2가지 흐름이 기업 인프라와 비즈니스에 미칠 실질적 변화와 리스크 분석.
"""
    },
    "it_easy": {
        "name": "2. 초보자·아이 눈높이 최신 테크 상식",
        "tistory_blog": TISTORY_IT_BLOG_NAME,
        "allow_velog": True,
        "tags_ko": ["테크상식", "쉬운IT", "IT용어정리", "반도체기초", "초보자가이드"],
        "tags_en": ["TechExplained", "TechBasics", "EasyIT", "FutureTech", "TechGlossary"],
        "prompt": """
당신은 복잡한 최신 IT·반도체·AI 기술을 초등학생 아이와 비전공자도 단번에 이해할 수 있도록 재미있게 풀어내는 IT 스토리텔러입니다.
Google 검색을 참고하여 **요즘 테크 업계에서 핫하게 떠오르는 핵심 기술 키워드 1가지**(예: 글라스 기판(Glass Substrate), CXL 메모리, 온디바이스 AI, 양자내성암호, AI 에이전트 등 중 택1)를 선정해 소개하세요.

[제목 형식]
- 한국어 제목: `🌐 [테크 상식] "재치있는 한줄 훅!" 기술명 (영문명)` (예: `🌐 [테크 상식] "에너지 효율의 끝판왕!" 글라스 기판 (Glass Substrate)`)
- 영어 제목: `🌐 [Tech Explained] "Catchy Hook!" Technology Name`

[본문 구성 - 각 언어별 2,200자 이상]
1. 왜 요즘 다들 이 기술에 열광할까? (일상 속 불편함과 연결)
2. 초등학생도 1분 만에 이해하는 핵심 비유 (직관적인 사물 비유 필수)
3. 우리 생활은 어떻게 달라질까?
4. 한눈에 정리하는 오늘의 테크 포인트 3줄 요약.
"""
    },
    "family_trip": {
        "name": "3. 7살 딸·5살 아들 아빠의 주말 나들이 정복기",
        "tistory_blog": TISTORY_FAMILY_BLOG_NAME,
        "allow_velog": False,
        "tags_ko": ["주말나들이", "아이와가볼만한곳", "가족여행", "남매육아", "주말코스추천"],
        "tags_en": ["KoreaFamilyTravel", "KidsActivitiesKorea", "WeekendTripKorea", "FamilyOuting", "TravelWithKids"],
        "prompt": """
당신은 7살 딸과 5살 아들을 키우는 현실 육아 아빠입니다.
Google 검색을 활용해 대한민국에서 **7살·5살 남매와 함께 주말에 다녀오기 정말 좋은 실제 나들이 명소 1~2곳 연계 코스**를 선정하여, 다른 부모들이 이 글만 보고 그대로 따라가도 실패하지 않을 실전 정복기를 작성하세요.

[제목 형식]
- 한국어 제목: `[주말 나들이] "매력적인 한줄 훅" + 7살 딸, 5살 아들과 함께한 [실제 장소명] 정복기`
- 영어 제목: `[Weekend Outing in Korea] "Catchy Hook" + Exploring [Place Name] with a 7yo Daughter & 5yo Son`

[본문 구성 - 각 언어별 2,300자 이상]
1. 7살 누나와 5살 동생, 둘 다 만족시킨 코스의 매력
2. 그대로 따라 하는 반나절 실전 이동 동선 (시간대별 제안)
3. 아빠가 직접 겪고 챙긴 현실 꿀팁 (주차장 위치, 예약 팁, 준비물)
4. 아이들과 함께하기 좋은 근처 식사 & 휴식 팁.
"""
    }
}


def generate_bilingual_posts(category_key):
    """단 1회의 API 호출로 한국어(KO)와 영어(EN) 포스팅을 동시에 생성하여 API 한도를 절반으로 아낍니다."""
    cat = POST_CATEGORIES[category_key]
    prompt = f"""
{cat['prompt']}

[출력 형식 - 반드시 아래 구분자(===KO_START===, ===EN_START===)를 정확히 지켜서 출력하세요]
===KO_START===
TITLE: 한국어 제목
(한국어 마크다운 본문 - 중간에 [IMG: 영문 묘사 3~6단어] 태그 2개 삽입)
===EN_START===
TITLE: English Title
(자연스러운 영어 로컬라이징 마크다운 본문 - 한국어 본문과 동일한 위치에 동일한 [IMG: 영문 묘사 3~6단어] 태그 2개 삽입)

[절대 금지]
- 괄호형 메모 `(확인 필요)` 등 절대 출력 금지.
- AI 번역투("새로운 지평", "결론적으로", "In conclusion", "delve into") 금지.
- 가상의 화자 이름(김대현 등) 금지.
"""
    resp = call_gemini_smart(prompt, use_search=cat.get("use_search", True))
    raw = (resp.text or "").strip()
    raw = re.sub(r"\([^)]*확인\s*필요[^)]*\)", "", raw)

    # KO / EN 블록 분리
    ko_part, en_part = raw, ""
    if "===EN_START===" in raw:
        parts = raw.split("===EN_START===", 1)
        ko_part = parts[0].replace("===KO_START===", "").strip()
        en_part = parts[1].strip()

    def parse_block(block_text):
        lines = block_text.splitlines()
        title, body_start = None, 0
        for i, line in enumerate(lines[:5]):
            m = re.match(r"^[#*\s]*TITLE\s*[:：]\s*(.+?)[*\s]*$", line.strip(), re.I)
            if m:
                title, body_start = m.group(1).strip(), i + 1
                break
        if not title:
            title = re.sub(r"^[#*\s]+|[*\s]+$", "", lines[0])
            body_start = 1
        return title, "\n".join(lines[body_start:]).strip()

    title_ko, md_ko_raw = parse_block(ko_part)
    title_en, md_en_raw = parse_block(en_part) if en_part else ("", "")

    # 이미지 생성 (2장만 생성해 KO와 EN 본문에 동일하게 삽입)
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
    return title_ko, md_ko, title_en, md_en


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
# 4. 티스토리 웹 자동화 (CodeMirror + React 상태 강제 동기화)
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
    cookies = json.loads(TISTORY_COOKIES_JSON)
    driver.get(target_url)
    time.sleep(2)
    for c in cookies:
        cookie_dict = {"name": c["name"], "value": c["value"], "path": c.get("path", "/")}
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

    # 1. 제목 입력
    title_inp = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#post-title-inp")))
    title_inp.clear()
    title_inp.send_keys(strip_non_bmp(title))
    time.sleep(0.5)

    # 2. HTML 모드로 전환
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#editor-mode-layer-btn-open"))).click()
    time.sleep(0.5)
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#editor-mode-html"))).click()
    time.sleep(1)
    try:
        driver.switch_to.alert.accept()
    except NoAlertPresentException:
        pass
    time.sleep(1)

    # 3. CodeMirror 에 HTML 주입 + 변경 이벤트 강제 발생
    safe_html = strip_non_bmp(html_body)
    n = driver.execute_script(
        """
        var cmEl = document.querySelector('.CodeMirror');
        if (!cmEl || !cmEl.CodeMirror) return -1;
        var cm = cmEl.CodeMirror;
        cm.setValue(arguments[0]);
        cm.save();
        var ta = cm.getTextArea();
        if (ta) {
            ta.dispatchEvent(new Event('input', { bubbles: true }));
            ta.dispatchEvent(new Event('change', { bubbles: true }));
        }
        return cm.getValue().length;
        """,
        safe_html,
    )
    if n is None or n <= 0:
        raise RuntimeError("티스토리 HTML 에디터(CodeMirror) 주입 실패")
    log.info("   📝 HTML 코드 주입 및 이벤트 동기화 완료: %d자", n)
    time.sleep(1)

    # 4. 기본모드로 안전 복귀 (모드 전환 버튼 텍스트/셀렉터 유연하게 탐색)
    try:
        mode_btn = WebDriverWait(driver, 6).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#editor-mode-layer-btn-open")))
        mode_btn.click()
        time.sleep(0.5)
        wysiwyg_btn = driver.find_element(By.XPATH, "//div[@id='editor-mode-layer']//div[contains(., '기본모드')] | //*[@id='editor-mode-wysiwyg']")
        wysiwyg_btn.click()
        time.sleep(1)
        try:
            driver.switch_to.alert.accept()
        except NoAlertPresentException:
            pass
        log.info("   🔄 기본모드(WYSIWYG) 동기화 완료")
        time.sleep(1)
    except Exception as e:
        log.info("   ℹ️ 기본모드 복귀 스킵 (HTML 모드 유지 발행 진행): %s", str(e)[:60])

    # 5. 태그 입력
    try:
        tag_inp = driver.find_element(By.CSS_SELECTOR, "#tagText")
        for t in tags[:5]:
            tag_inp.send_keys(strip_non_bmp(t))
            tag_inp.send_keys(Keys.ENTER)
            time.sleep(0.2)
    except Exception:
        pass

    # 6. 발행 버튼 클릭
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#publish-layer-btn"))).click()
    time.sleep(1)
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#open20"))).click()
    time.sleep(0.5)
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#publish-btn"))).click()

    WebDriverWait(driver, 20).until(lambda d: "newpost" not in d.current_url)
    return driver.current_url


# ---------------------------------------------------------
# 5. 멀티 API 플랫폼 (Blogger / WordPress / Velog)
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
# 6. 메인 컨트롤러
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
            title_ko, md_ko, title_en, md_en = generate_bilingual_posts(cat_key)
            log.info("📌 [KO] 제목: %s (%d자)", title_ko, len(md_to_plain(md_ko)))
            if title_en:
                log.info("📌 [EN] Title: %s (%d chars)", title_en, len(md_to_plain(md_en)))

            posts_to_publish = []
            if "ko" in langs and title_ko:
                posts_to_publish.append(("KO", title_ko, md_ko, md_to_styled_html(md_ko), cat["tags_ko"]))
            if "en" in langs and title_en:
                posts_to_publish.append(("EN", title_en, md_en, md_to_styled_html(md_en), cat["tags_en"]))

            for lang_code, title, md_body, html_body, tags in posts_to_publish:
                if args.dry_run:
                    print(f"\n=== [{cat_key} | {lang_code}] {title} ===\n{md_body[:500]}...\n")
                    continue

                if "tistory" in targets and driver:
                    try:
                        url = post_to_tistory(driver, cat["tistory_blog"], title, html_body, tags)
                        results[f"{cat_key}[{lang_code}]:tistory({cat['tistory_blog']})"] = ("OK", url)
                        log.info("✅ [티스토리:%s][%s] 발행 완료", cat["tistory_blog"], lang_code)
                    except Exception as e:
                        results[f"{cat_key}[{lang_code}]:tistory({cat['tistory_blog']})"] = ("FAIL", str(e))
                        log.error("❌ [티스토리:%s][%s] 실패: %s", cat["tistory_blog"], lang_code, e)
                        driver.save_screenshot(os.path.join(LOG_DIR, f"fail_tistory_{cat_key}_{lang_code}.png"))

                if "blogger" in targets and BLOGGER_ID:
                    try:
                        url = post_to_blogger(title, html_body, tags)
                        results[f"{cat_key}[{lang_code}]:blogger"] = ("OK", url)
                        log.info("✅ [Blogger][%s] 발행 완료: %s", lang_code, url)
                    except Exception as e:
                        results[f"{cat_key}[{lang_code}]:blogger"] = ("FAIL", str(e))
                        log.error("❌ [Blogger][%s] 실패: %s", lang_code, e)

                if "wordpress" in targets and WORDPRESS_URL:
                    try:
                        url = post_to_wordpress(title, html_body)
                        results[f"{cat_key}[{lang_code}]:wordpress"] = ("OK", url)
                        log.info("✅ [WordPress][%s] 발행 완료: %s", lang_code, url)
                    except Exception as e:
                        results[f"{cat_key}[{lang_code}]:wordpress"] = ("FAIL", str(e))
                        log.error("❌ [WordPress][%s] 실패: %s", lang_code, e)

                if "velog" in targets and cat["allow_velog"] and VELOG_REFRESH_TOKEN:
                    try:
                        url = post_to_velog(title, md_body, tags)
                        results[f"{cat_key}[{lang_code}]:velog"] = ("OK", url)
                        log.info("✅ [Velog][%s] 발행 완료: %s", lang_code, url)
                    except Exception as e:
                        results[f"{cat_key}[{lang_code}]:velog"] = ("FAIL", str(e))
                        log.error("❌ [Velog][%s] 실패: %s", lang_code, e)

                time.sleep(6)
    finally:
        if driver:
            driver.quit()

    if not args.dry_run:
        print("\n===== 최종 멀티 플랫폼 & 한/영 발행 요약 =====")
        for k, (status, info) in results.items():
            print(f"{'✅' if status == 'OK' else '❌'} {k:38s} -> {info}")
        if any(s == "FAIL" for s, _ in results.values()):
            sys.exit(1)


if __name__ == "__main__":
    main()
