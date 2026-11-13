import os
import re
import sys
import json
import time
import random
import argparse
import logging
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
# 0. 로깅 및 환경 변수 설정
# ---------------------------------------------------------
LOG_DIR = os.path.abspath("./logs")
os.makedirs(LOG_DIR, exist_ok=True)
RUN_ID = time.strftime("%Y%m%d_%H%M%S")
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
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
IMAGE_SOURCE = (env("IMAGE_SOURCE", "free_ai") or "free_ai").lower()
UNSPLASH_ACCESS_KEY = env("UNSPLASH_ACCESS_KEY")

TISTORY_IT_BLOG_NAME = env("TISTORY_IT_BLOG_NAME") or "aaroneom"
TISTORY_FAMILY_BLOG_NAME = env("TISTORY_FAMILY_BLOG_NAME") or "lhimepapa"
TISTORY_COOKIES_JSON = env("TISTORY_COOKIES_JSON")

BLOGGER_ID = env("BLOGGER_ID")
BLOGGER_TOKEN_JSON = env("BLOGGER_TOKEN_JSON")
BLOGGER_TOKEN_PATH = "token.json"

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version="v1beta")
) if GEMINI_API_KEY else None


# ---------------------------------------------------------
# 1. 공통 유틸리티
# ---------------------------------------------------------
def retry(fn, tries=3, delay=3):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            log.warning("재시도 %d/%d: %s", i + 1, tries, e)
            if i == tries - 1:
                raise
            time.sleep(delay * (i + 1))


def strip_non_bmp(text):
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF)


def md_to_plain(md_text):
    t = re.sub(r"!\[.*?\]\(.*?\)", "", md_text)
    t = re.sub(r"^\*(?:사진|이미지):.*$", "", t, flags=re.M)
    t = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", t)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__|\*|`)", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return strip_non_bmp(t).strip()


# ---------------------------------------------------------
# 2. 100% 무료 이미지 파이프라인 (Pollinations AI + 스톡 폴백)
# ---------------------------------------------------------
_used_images = set()


def _is_image_reachable(url):
    try:
        r = requests.get(
            url,
            stream=True,
            timeout=25,
            headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-1023"},
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
    if _is_image_reachable(url):
        return url, "AI Illustration (Pollinations.ai)"
    raise RuntimeError("Pollinations AI 이미지 응답 지연")


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
            "action": "query",
            "generator": "search",
            "gsrnamespace": 6,
            "gsrsearch": f"{q} filetype:bitmap",
            "gsrlimit": 8,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|mime",
            "iiurlwidth": 1200,
            "format": "json",
            "origin": "*",
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
            log.warning("   ⚠️ 무료 AI 이미지 실패 → 무료 스톡 검색 전환: %s", e)
    for fn in (_search_unsplash, _search_wikimedia):
        try:
            res = fn(en_keyword)
            if res:
                return res
        except Exception:
            pass
    return None


# ---------------------------------------------------------
# 3. 3가지 독립 포스팅 정의 및 생성 (블로그 자동 매핑)
# ---------------------------------------------------------
POST_CATEGORIES = {
    "it_trend": {
        "name": "1. IT 전문가 트렌드 & 이슈 논평",
        "target_blog": TISTORY_IT_BLOG_NAME,
        "tags": ["IT트렌드", "기술칼럼", "AI인사이트", "엔터프라이즈IT", "테크논평"],
        "use_search": True,
        "prompt": """
당신은 글로벌 IT 인프라·클라우드·네트워크 업계에서 14년 넘게 현장을 뛰어온 IT 전문가입니다.
Google 검색 도구를 사용해 **오늘(2026년 11월) 한국 및 글로벌 IT 업계에서 가장 뜨겁게 화제가 되고 있는 최신 기술·산업 이슈 1가지**를 찾아, 현업 전문가의 깊이 있는 정리 및 논평 글을 작성하세요.

[글쓰기 디렉팅]
- 문체: 현업 전문가가 자신의 블로그에 차분하고 날카롭게 정리하는 1인칭 시점('~합니다', '~라고 봅니다'). 과장된 AI 번역투("새로운 지평을 열 것입니다", "결론적으로") 절대 금지.
- 구성 (소제목 4~5개, 공백 포함 2,200~2,800자 분량):
  1. 도입부: 최근 업계에서 이 이슈가 왜 수면 위로 떠올랐는지 현장감 있게 화두를 던집니다.
  2. 이슈의 핵심 팩트 정리: 누가, 무엇을 발표했거나 어떤 기술적 변화가 일어났는지 명확히 정리합니다.
  3. 기술적·구조적 배경 분석: 겉으로 드러난 뉴스 이면에 어떤 인프라·비용·보안·비즈니스 역학이 숨어 있는지 짚어줍니다.
  4. 현업 전문가의 시선(논평): 실제 기업 고객들과 현장에서 부딪히는 입장에서 볼 때, 이 변화가 가져올 현실적인 기회와 경계해야 할 거품(리스크)을 솔직하게 논평합니다.
"""
    },
    "it_easy": {
        "name": "2. 초보자·아이·어르신도 이해하는 쉬운 IT 설명",
        "target_blog": TISTORY_IT_BLOG_NAME,
        "tags": ["쉬운IT", "IT상식", "초보자가이드", "디지털기초", "테크이야기"],
        "use_search": True,
        "prompt": """
당신은 복잡한 IT 기술을 초등학생 아이부터 스마트폰이 낯선 어르신까지 단번에 고개를 끄덕이게 만드는 친절한 IT 전문가입니다.
사람들이 뉴스나 일상에서 자주 듣지만 정작 "그게 정확히 뭐야?"라고 헷갈려 하는 **요즘 IT 트렌드 기술 1가지**(예: 온디바이스 AI, AI 에이전트, 클라우드 백업, 패스키 로그인, 양자보안, 피싱 차단 기술 등 중 택1)를 골라 알기 쉽게 풀어쓰세요.

[글쓰기 디렉팅]
- 문체: 옆집의 친절한 IT 전문가가 차 한잔 마시며 조곤조곤 설명해 주는 따뜻하고 편안한 어조.
- 비유 필수: 어려운 영어 약어나 IT 용어를 그대로 던지지 말고, '동네 도서관', '아파트 경비실', '개인 금고', '똑똑한 비서' 같은 일상생활 속 사물에 빗대어 설명하세요.
- 구성 (소제목 4개, 공백 포함 2,200~2,800자 분량):
  1. 공감 가는 도입: 뉴스나 주변에서 이 단어를 들었을 때 막막했던 경험으로 시작합니다.
  2. 1분 만에 이해하는 핵심 비유: 아이와 어르신도 바로 머릿속에 그림이 그려지도록 쉬운 비유로 원리를 설명합니다.
  3. 우리 일상에서 이미 쓰이고 있는 모습: 실제 스마트폰이나 생활 속에서 어떻게 쓰이고 있는지 체감 사례를 들어줍니다.
  4. 실생활 활용 및 주의할 점: 당장 편리하게 써먹는 방법과 피싱·개인정보 유출을 피하기 위한 현실적인 안전 수칙을 알려줍니다.
"""
    },
    "family_trip": {
        "name": "3. 두 아이(7살 딸·5살 아들) 아빠의 실전 여행 가이드",
        "target_blog": TISTORY_FAMILY_BLOG_NAME,
        "tags": ["아이와가볼만한곳", "주말나들이", "가족여행코스", "육아여행", "주말여행추천"],
        "use_search": True,
        "prompt": """
당신은 7살 딸과 5살 아들을 키우는 현실 육아 아빠입니다.
Google 검색을 활용해 대한민국에서 **7살·5살 남매와 함께 주말에 다녀오기 정말 좋은 실제 여행지/나들이 명소 1곳**(체험 박물관, 생태공원, 수목원, 바다·갯벌 체험, 테마파크 등 계절에 맞는 실제 장소)을 선정하여, **다른 부모들이 이 글만 보고 그대로 따라가도 실패하지 않을 만큼 디테일한 실전 가이드**를 작성하세요.

[글쓰기 디렉팅]
- 문체: 직접 아이들 손을 잡고 다녀온 아빠가 다른 부모들에게 진심으로 추천해 주는 생생하고 담백한 후기·가이드 문체. (허구적인 SF 기술이나 과장된 묘사 절대 금지)
- 구성 (소제목 4~5개, 공백 포함 2,300~2,900자 분량):
  1. 이곳을 선택한 이유: 7살 누나(호기심·체험 위주)와 5살 동생(활동량·안전 위주) 두 연령대를 동시에 만족시킬 수 있는 핵심 매력을 짚어줍니다.
  2. 그대로 따라 하는 반나절 추천 동선: 오전 도착부터 점심, 오후 체험까지 아이들 체력과 낮잠 시간을 고려한 가장 효율적인 이동 순서를 시간대별로 제안합니다.
  3. 아빠가 직접 챙긴 현실 꿀팁 (주차·준비물·복장): 주차장 진입 팁, 유모차나 웨건 반입 여부, 화장실 동선, 여벌 옷이나 간식 등 현장에서 당황하지 않기 위한 실전 노하우를 꼼꼼히 적습니다.
  4. 아이들과 함께 가기 좋은 근처 식사 포인트: 아이들이 먹기 좋은 메뉴나 식사 해결 팁을 자연스럽게 안내합니다.
"""
    }
}


def generate_post(category_key, custom_topic=None):
    cat = POST_CATEGORIES[category_key]
    topic_note = f"\n[지정 주제]: {custom_topic}\n" if custom_topic else ""

    prompt = f"""
{cat['prompt']}
{topic_note}
[절대 준수 규칙]
1. 첫 줄은 반드시 `TITLE: 제목` 형식으로 작성하세요. (제목만 적고 따옴표나 마크다운 기호 금지)
2. 둘째 줄부터 마크다운 본문을 작성하세요.
3. 분량은 **공백 포함 한국어 2,200자 이상 2,800자 이하**로 충실하고 밀도 있게 작성하세요.
4. 소제목은 `## 소제목` 형식을 사용하고, 소제목 위아래로 반드시 빈 줄을 한 줄씩 넣으세요.
5. 본문 흐름상 시각 자료가 들어가면 좋은 위치에 `[IMG: 영문 묘사 3~6단어]` 태그를 **정확히 2개** 배치하세요.
   - 예시: `[IMG: modern cloud server data center blue light]`, `[IMG: happy korean family walking in autumn park]`
   - 반드시 영어로만 적고, 이미지 안에 글자(text)가 들어가는 장면은 피하세요.
6. **치명적 금지 사항:**
   - 본문에 `(확인 필요)`, `(요금 확인 필요)`, `(가격 변동 가능)` 같은 괄호형 AI 메모나 주석을 절대 출력하지 마세요. 정확한 가격이나 영업시간을 모르면 숫자를 지어내지 말고 "방문 전 공식 홈페이지에서 최신 운영 정보를 확인하는 것이 좋습니다"처럼 자연스러운 문장으로 쓰세요.
   - 가상의 화자 이름(예: 김대현 등)을 지어내지 마세요.
"""
    cfg = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]) if cat["use_search"] else None
    resp = retry(lambda: client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg))
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini 응답이 비어 있습니다.")

    text = re.sub(r"\([^)]*확인\s*필요[^)]*\)", "", text)
    text = re.sub(r"\[[^]]*확인\s*필요[^]]*\]", "", text)

    lines = text.splitlines()
    title, body_start = None, 0
    for i, line in enumerate(lines[:6]):
        m = re.match(r"^[#*\s]*(?:\[?제목\]?|TITLE)\s*[:：]\s*(.+?)[*\s]*$", line.strip(), re.I)
        if m:
            title, body_start = m.group(1).strip(), i + 1
            break
    if not title:
        title = re.sub(r"^[#*\s]+|[*\s]+$", "", lines[0])
        body_start = 1
    body = "\n".join(lines[body_start:]).strip()

    def repl(m):
        kw = m.group(1).strip()
        img = get_image(kw)
        if not img:
            return ""
        url, credit = img
        return f"\n\n![{kw}]({url})\n\n*이미지 출처: {credit}*\n\n"

    md_body = re.sub(r"\[IMG:\s*(.+?)\]", repl, body)
    html_body = markdown.markdown(md_body, extensions=["extra", "nl2br"])
    html_body = html_body.replace("<img ", '<img style="max-width:100%;height:auto;border-radius:8px;margin:16px 0;" ')
    html_body = html_body.replace("<h2>", '<h2 style="margin-top:32px;margin-bottom:14px;font-size:1.35em;border-bottom:1px solid #eee;padding-bottom:6px;">')
    return title, md_body, html_body, cat["tags"], cat["target_blog"]


# ---------------------------------------------------------
# 4. 티스토리 웹 자동화 (루트 + 서브도메인 쿠키 동시 주입)
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
        raise RuntimeError("TISTORY_COOKIES_JSON 시크릿이 설정되지 않았습니다.")
    cookies = json.loads(TISTORY_COOKIES_JSON)
    driver.get(target_url)
    time.sleep(2)
    for c in cookies:
        cookie_dict = {
            "name": c["name"],
            "value": c["value"],
            "path": c.get("path", "/"),
        }
        if "domain" in c and "tistory.com" in c["domain"]:
            cookie_dict["domain"] = c["domain"]
        try:
            driver.add_cookie(cookie_dict)
        except Exception:
            pass


def post_to_tistory(driver, blog_name, title, html_body, tags):
    if not blog_name:
        raise RuntimeError("대상 티스토리 블로그 이름이 설정되지 않았습니다.")

    log.info("   🎯 대상 티스토리 블로그: https://%s.tistory.com", blog_name)
    inject_cookies_for_domain(driver, "https://www.tistory.com")
    inject_cookies_for_domain(driver, f"https://{blog_name}.tistory.com")

    driver.get(f"https://{blog_name}.tistory.com/manage/newpost/?type=post")
    time.sleep(3)

    try:
        driver.switch_to.alert.dismiss()
    except NoAlertPresentException:
        pass

    if any(m in driver.current_url for m in ["accounts.kakao.com", "/auth/login", "tistory.com/login"]):
        raise RuntimeError("티스토리 쿠키 세션이 만료되었습니다. export_cookies.py 로 쿠키를 다시 추출해 GitHub Secrets를 갱신하세요.")

    title_inp = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#post-title-inp")))
    title_inp.clear()
    title_inp.send_keys(strip_non_bmp(title))

    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#editor-mode-layer-btn-open"))).click()
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#editor-mode-html"))).click()
    time.sleep(1)
    try:
        driver.switch_to.alert.accept()
    except NoAlertPresentException:
        pass
    time.sleep(1)

    n = driver.execute_script(
        "var cm = document.querySelector('.CodeMirror');"
        "if(!cm || !cm.CodeMirror) return -1;"
        "cm.CodeMirror.setValue(arguments[0]);"
        "return cm.CodeMirror.getValue().length;",
        strip_non_bmp(html_body),
    )
    if n is None or n < 0:
        raise RuntimeError("티스토리 HTML 에디터(CodeMirror) 로드 실패")

    try:
        tag_inp = driver.find_element(By.CSS_SELECTOR, "#tagText")
        for t in tags[:5]:
            tag_inp.send_keys(t)
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
# 5. Blogger API 발행 (선택)
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


# ---------------------------------------------------------
# 6. 실행 메인
# ---------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="all", help="all | it_trend | it_easy | family_trip")
    ap.add_argument("--platforms", default="tistory", help="tistory,blogger")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not client:
        raise SystemExit("GEMINI_API_KEY 시크릿이 없습니다.")

    targets = [p.strip().lower() for p in args.platforms.split(",") if p.strip()]
    categories = list(POST_CATEGORIES.keys()) if args.category == "all" else [args.category]

    driver = None
    if not args.dry_run and "tistory" in targets:
        driver = init_headless_driver()

    results = {}
    try:
        for cat_key in categories:
            if cat_key not in POST_CATEGORIES:
                continue
            cat_name = POST_CATEGORIES[cat_key]["name"]
            log.info("\n==================================================")
            log.info("✍️  [%s] 생성 시작", cat_name)
            title, md_body, html_body, tags, target_blog = generate_post(cat_key)
            plain_body = md_to_plain(md_body)
            n_img = len(re.findall(r"!\[.*?\]\(http", md_body))
            log.info("📌 제목: %s (공백포함 %d자, 이미지 %d장)", title, len(plain_body), n_img)

            if args.dry_run:
                print(f"\n--- [{cat_name} -> {target_blog}.tistory.com] {title} ---\n{md_body}\n")
                continue

            if "tistory" in targets and driver:
                try:
                    url = post_to_tistory(driver, target_blog, title, html_body, tags)
                    results[f"{cat_key}:tistory({target_blog})"] = ("OK", url)
                    log.info("✅ [티스토리:%s] 발행 완료: %s", target_blog, url)
                except Exception as e:
                    results[f"{cat_key}:tistory({target_blog})"] = ("FAIL", str(e))
                    log.error("❌ [티스토리:%s] 실패: %s", target_blog, e)
                    driver.save_screenshot(os.path.join(LOG_DIR, f"fail_tistory_{cat_key}.png"))

            if "blogger" in targets and BLOGGER_ID:
                try:
                    url = post_to_blogger(title, html_body, tags)
                    results[f"{cat_key}:blogger"] = ("OK", url)
                    log.info("✅ [Blogger] 발행 완료: %s", url)
                except Exception as e:
                    results[f"{cat_key}:blogger"] = ("FAIL", str(e))
                    log.error("❌ [Blogger] 실패: %s", e)

            if len(categories) > 1:
                time.sleep(8)
    finally:
        if driver:
            driver.quit()

    if not args.dry_run:
        print("\n===== GitHub Actions 발행 요약 =====")
        for k, (status, info) in results.items():
            print(f"{'✅' if status == 'OK' else '❌'} {k:32s} -> {info}")
        if any(s == "FAIL" for s, _ in results.values()):
            sys.exit(1)


if __name__ == "__main__":
    main()
