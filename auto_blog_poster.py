"""
블로그 자동 포스팅 (블로거 / 워드프레스 / 벨로그 = API, 티스토리 / 네이버 / 브런치 = Selenium)

설치:
    pip install google-genai requests markdown google-api-python-client google-auth
    pip install selenium            # 티스토리·네이버·브런치를 '내 PC'에서 돌릴 때만 필요

사용 예:
    python auto_blog_poster.py --dry-run                    # 글만 생성 (게시 안 함)
    python auto_blog_poster.py --platforms api              # blogger, wordpress, velog (GitHub Actions 용)
    python auto_blog_poster.py --platforms web              # tistory, naver, brunch (내 PC 전용)
    python auto_blog_poster.py --platforms tistory          # 하나만
    python auto_blog_poster.py --login-setup                # (내 PC 최초 1회) 크롬 프로필에 로그인 저장

이미지: UNSPLASH_ACCESS_KEY 가 있으면 우선 사용, 없어도 Openverse(키 없이 사용 가능)로 검색.
        PEXELS_API_KEY 는 예전에 발급받은 키가 있을 때만 보조로 사용 (신규 발급은 중단됨).
로그:   콘솔 + ./logs/run_*.log 에 저장. Selenium 실패 시 ./logs/fail_*.png/.html 로 화면도 저장.
"""
import os
import re
import sys
import time
import argparse
import logging

import requests
import markdown
from google import genai

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

try:  # Selenium 은 웹 자동화(티스토리/네이버/브런치)에서만 필요 → 없어도 API 플랫폼은 동작
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import NoAlertPresentException
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

    class NoAlertPresentException(Exception):
        pass


# ---------------------------------------------------------
# 0. .env 로드 + 로깅
# ---------------------------------------------------------
def load_env_file(path=".env"):
    """로컬 실행용. 같은 폴더의 .env 를 읽어 환경변수로 등록 (이미 설정된 값은 유지)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file()
env = os.environ.get

LOG_DIR = os.path.abspath(env("LOG_DIR", "./logs"))
os.makedirs(LOG_DIR, exist_ok=True)
RUN_ID = time.strftime("%Y%m%d_%H%M%S")
LOG_FILE = os.path.join(LOG_DIR, f"run_{RUN_ID}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
for noisy in ("httpx", "google_genai", "urllib3", "googleapiclient"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("autopost")

# ---------------------------------------------------------
# 1. 환경 변수
# ---------------------------------------------------------
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")

UNSPLASH_ACCESS_KEY = env("UNSPLASH_ACCESS_KEY")          # 선택
OPENVERSE_CLIENT_ID = env("OPENVERSE_CLIENT_ID")          # 선택 (없어도 익명으로 동작, 한도만 낮음)
OPENVERSE_CLIENT_SECRET = env("OPENVERSE_CLIENT_SECRET")  # 선택
PEXELS_API_KEY = env("PEXELS_API_KEY")                    # 선택 (신규 발급 중단됨)

BLOGGER_ID = env("BLOGGER_ID")
BLOGGER_TOKEN_PATH = env("BLOGGER_TOKEN_PATH", "token.json")
WORDPRESS_URL = (env("WORDPRESS_URL") or "").rstrip("/")
WORDPRESS_USER = env("WORDPRESS_USER")
WORDPRESS_APP_PASS = env("WORDPRESS_APP_PASS")
VELOG_ACCESS_TOKEN = env("VELOG_ACCESS_TOKEN")            # 선택 (쿠키 access_token, 1시간짜리)
VELOG_REFRESH_TOKEN = env("VELOG_REFRESH_TOKEN") or env("VELOG_SESSION_TOKEN")  # 쿠키 refresh_token

CHROME_USER_DATA = env("CHROME_USER_DATA_PATH", os.path.abspath("./chrome_profile"))
TISTORY_BLOG_NAME = env("TISTORY_BLOG_NAME")
NAVER_BLOG_ID = env("NAVER_BLOG_ID")
BRUNCH_PUBLISH = env("BRUNCH_PUBLISH", "0") == "1"        # 기본: 임시저장만

HTTP_TIMEOUT = 30
API_PLATFORMS = ["blogger", "wordpress", "velog"]
WEB_PLATFORMS = ["tistory", "naver", "brunch"]
ALL_PLATFORMS = API_PLATFORMS + WEB_PLATFORMS

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# ---------------------------------------------------------
# 2. 공통 유틸
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
    """ChromeDriver send_keys 는 이모지 등 BMP 밖 문자에서 예외가 난다."""
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF)


def md_to_plain(md_text):
    t = re.sub(r"!\[.*?\]\(.*?\)", "", md_text)
    t = re.sub(r"^\*사진:.*$", "", t, flags=re.M)
    t = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", t)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__|\*|`)", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return strip_non_bmp(t).strip()


# ---------------------------------------------------------
# 3. 이미지 검색 (Unsplash → Openverse → Pexels)
# ---------------------------------------------------------
_used_images = set()
_en_cache = {}
_ov_token = {"value": None, "tried": False}


def _is_image(url):
    """HEAD 는 막는 서버가 많아서 Range GET 으로 실제 이미지인지 확인."""
    try:
        r = requests.get(url, stream=True, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-1023"})
        ok = r.status_code < 400 and "image" in r.headers.get("Content-Type", "")
        r.close()
        return ok
    except requests.RequestException:
        return False


def _to_english_query(query):
    """한국어 검색어 → 영어 검색어 (Gemini). 실패하면 None."""
    if query in _en_cache:
        return _en_cache[query]
    en = None
    try:
        r = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"다음 사진 검색어를 스톡사진 사이트용 영어 검색어(2~4단어, 일반 명사/장면)로만 답하세요. 설명 금지.\n{query}",
        )
        cand = (r.text or "").strip().splitlines()[0].strip(" \"'.")
        if cand and cand.isascii():
            en = cand
    except Exception as e:
        log.warning("   영어 검색어 변환 실패 (%s): %s", query, e)
    _en_cache[query] = en
    return en


def _search_unsplash(q, lang):
    r = requests.get("https://api.unsplash.com/search/photos",
                     headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}", "Accept-Version": "v1"},
                     params={"query": q, "per_page": 8, "orientation": "landscape"}, timeout=10)
    r.raise_for_status()
    for p in r.json().get("results", []):
        url = p.get("urls", {}).get("regular")
        if url:
            yield (url, f"Photo by {p.get('user', {}).get('name', '')} on Unsplash",
                   p.get("links", {}).get("download_location"))


def _openverse_token():
    """키가 있으면 OAuth 토큰으로 한도 상향, 없으면 익명."""
    if _ov_token["tried"]:
        return _ov_token["value"]
    _ov_token["tried"] = True
    if OPENVERSE_CLIENT_ID and OPENVERSE_CLIENT_SECRET:
        try:
            r = requests.post("https://api.openverse.org/v1/auth_tokens/token/",
                              data={"grant_type": "client_credentials", "client_id": OPENVERSE_CLIENT_ID,
                                    "client_secret": OPENVERSE_CLIENT_SECRET}, timeout=10)
            r.raise_for_status()
            _ov_token["value"] = r.json().get("access_token")
        except Exception as e:
            log.warning("   Openverse 토큰 발급 실패 → 익명으로 진행: %s", e)
    return _ov_token["value"]


def _search_openverse(q, lang):
    headers = {"User-Agent": "auto-blog-poster/1.0"}
    tok = _openverse_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = requests.get("https://api.openverse.org/v1/images/", headers=headers,
                     params={"q": q, "page_size": 10, "license_type": "commercial",
                             "category": "photograph", "mature": "false"}, timeout=15)
    if r.status_code == 429:
        raise RuntimeError("Openverse 요청 한도 초과(429)")
    r.raise_for_status()
    for p in r.json().get("results", []):
        url = p.get("url")
        if url:
            credit = p.get("attribution") or f"{p.get('creator', '')} / {str(p.get('license', '')).upper()} (Openverse)"
            yield url, credit, None


def _search_pexels(q, lang):
    r = requests.get("https://api.pexels.com/v1/search", headers={"Authorization": PEXELS_API_KEY},
                     params={"query": q, "per_page": 8, "orientation": "landscape", "locale": lang}, timeout=10)
    r.raise_for_status()
    for p in r.json().get("photos", []):
        src = p.get("src", {})
        url = src.get("large2x") or src.get("large")
        if url:
            yield url, f"Photo by {p.get('photographer', '')} on Pexels", None


def image_providers():
    p = []
    if UNSPLASH_ACCESS_KEY:
        p.append(("unsplash", _search_unsplash))
    p.append(("openverse", _search_openverse))          # 키 없이도 사용 가능
    if PEXELS_API_KEY:
        p.append(("pexels", _search_pexels))
    return p


def get_image(query):
    """(이미지 URL, 출처 문구) 또는 None. 저작권이 정리된 이미지(상업적 이용 가능)만 사용."""
    providers = image_providers()
    queries = [(query, "ko-KR")]
    en = _to_english_query(query)
    if en:
        queries.append((en, "en-US"))
    log.info("   🖼 이미지 검색: '%s' (영문: %s)", query, en)

    for q, lang in queries:
        for name, fn in providers:
            if name == "unsplash" and lang.startswith("ko"):
                continue                      # Unsplash 는 영어 검색만 효과적
            try:
                n = 0
                for url, credit, track in fn(q, lang):
                    n += 1
                    if url in _used_images or not _is_image(url):
                        continue
                    _used_images.add(url)
                    if track:                 # Unsplash 가이드라인: 사용 시 download 트리거
                        try:
                            requests.get(track, headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}, timeout=5)
                        except requests.RequestException:
                            pass
                    log.info("   🖼 → %s 사용: %s", name, url[:90])
                    return url, credit
                log.info("   🖼 %s('%s'): 후보 %d개, 사용 가능한 이미지 없음", name, q, n)
            except Exception as e:
                log.warning("   🖼 %s('%s') 실패: %s", name, q, e)
    log.warning("   🖼 이미지를 찾지 못해 생략: %s", query)
    return None


# ---------------------------------------------------------
# 4. 콘텐츠 생성
# ---------------------------------------------------------
def generate_post(topic):
    prompt = f"""
14년 차 IT 전문가이자 두 아이의 아빠 관점에서 '{topic}' 관련 블로그 포스팅을 한국어로 작성하세요.

[형식 규칙]
- 첫 줄: 'TITLE: 제목' (제목만, 마크다운 기호 금지)
- 둘째 줄부터 마크다운 본문 (## 소제목 사용, 1500~2500자)
- 사진이 필요한 위치에 [IMG: 검색키워드] 를 3개 넣으세요.
  키워드는 무료 스톡사진 사이트에서 찾을 수 있는 일반적인 장면/사물 표현입니다 (예: 가족 나들이 공원, 노트북으로 코딩하는 모습). 특정 상호명·지명·인물명은 피하세요.
- 확실하지 않은 영업시간·가격·최신 수치는 단정하지 말고 '확인 필요'라고 쓰세요.
"""
    resp = retry(lambda: client.models.generate_content(model=GEMINI_MODEL, contents=prompt))
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini 응답이 비어 있습니다.")

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
        return f"\n\n![{kw}]({url})\n\n*사진: {credit}*\n\n"

    md_body = re.sub(r"\[IMG:\s*(.+?)\]", repl, body)
    html_body = markdown.markdown(md_body, extensions=["extra", "nl2br"])
    html_body = html_body.replace("<img ", '<img style="max-width:100%;height:auto;" ')
    return title, md_body, html_body


# ---------------------------------------------------------
# 5. API 플랫폼 (블로거 / 워드프레스 / 벨로그)
# ---------------------------------------------------------
def post_to_blogger(title, html_body):
    if not BLOGGER_ID:
        raise RuntimeError("BLOGGER_ID 가 설정되지 않았습니다.")
    if not os.path.exists(BLOGGER_TOKEN_PATH):
        raise RuntimeError(f"{BLOGGER_TOKEN_PATH} 파일이 없습니다. (Actions 라면 BLOGGER_TOKEN_JSON 시크릿으로 파일을 만들어야 함)")
    scopes = ["https://www.googleapis.com/auth/blogger"]
    creds = Credentials.from_authorized_user_file(BLOGGER_TOKEN_PATH, scopes)
    log.info("   blogger 토큰 유효=%s, 만료됨=%s, refresh_token 있음=%s", creds.valid, creds.expired, bool(creds.refresh_token))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            log.info("   blogger 토큰 갱신 완료")
            try:
                with open(BLOGGER_TOKEN_PATH, "w") as f:
                    f.write(creds.to_json())
            except OSError as e:
                log.warning("   갱신된 토큰 저장 실패(무시): %s", e)
        else:
            raise RuntimeError("token.json 이 무효입니다(refresh_token 없음/폐기). OAuth 를 다시 진행하세요.")
    service = build("blogger", "v3", credentials=creds, cache_discovery=False)
    body = {"kind": "blogger#post", "title": title, "content": html_body}
    res = service.posts().insert(blogId=BLOGGER_ID, body=body, isDraft=False).execute()
    return res.get("url")


def post_to_wordpress(title, html_body):
    if not (WORDPRESS_URL and WORDPRESS_USER and WORDPRESS_APP_PASS):
        raise RuntimeError("워드프레스 환경변수 누락 (WORDPRESS_URL / WORDPRESS_USER / WORDPRESS_APP_PASS)")
    payload = {"title": title, "content": html_body, "status": "publish"}
    auth = (WORDPRESS_USER, WORDPRESS_APP_PASS)
    endpoints = [f"{WORDPRESS_URL}/wp-json/wp/v2/posts", f"{WORDPRESS_URL}/?rest_route=/wp/v2/posts"]
    last = None
    for ep in endpoints:
        res = requests.post(ep, json=payload, auth=auth, timeout=HTTP_TIMEOUT)
        log.info("   wordpress POST %s → HTTP %s", ep, res.status_code)
        if res.status_code == 201:
            return res.json().get("link")
        last = res
        if res.status_code not in (404,):
            break
    hint = {401: " (사용자명/앱 비밀번호 확인. 공백 포함 그대로 입력)",
            403: " (보안 플러그인/호스팅이 REST API 쓰기를 차단했을 수 있음)",
            404: " (REST API 경로 없음: 주소/퍼머링크 설정 또는 wordpress.com 무료 플랜 여부 확인)"}.get(last.status_code, "")
    raise RuntimeError(f"HTTP {last.status_code}{hint}: {last.text[:200]}")


def post_to_velog(title, md_body):
    """비공식 GraphQL API 라서 스키마가 바뀌면 깨질 수 있습니다."""
    if not VELOG_REFRESH_TOKEN:
        raise RuntimeError("VELOG_REFRESH_TOKEN 누락")
    cookie = f"refresh_token={VELOG_REFRESH_TOKEN}"
    if VELOG_ACCESS_TOKEN:
        cookie = f"access_token={VELOG_ACCESS_TOKEN}; " + cookie
    headers = {"Cookie": cookie, "Content-Type": "application/json",
               "Origin": "https://velog.io", "Referer": "https://velog.io/"}
    log.info("   velog 쿠키: refresh_token(%d자)%s", len(VELOG_REFRESH_TOKEN),
             f", access_token({len(VELOG_ACCESS_TOKEN)}자)" if VELOG_ACCESS_TOKEN else ", access_token 없음")

    def gql(query, variables=None):
        res = requests.post("https://v2.velog.io/graphql", json={"query": query, "variables": variables or {}},
                            headers=headers, timeout=HTTP_TIMEOUT)
        log.info("   velog HTTP %s", res.status_code)
        try:
            return res.json()
        except ValueError:
            raise RuntimeError(f"JSON 응답이 아님: HTTP {res.status_code} {res.text[:200]}")

    username = None
    me = gql("query { currentUser { id username } }")
    if me.get("errors"):
        log.warning("   velog 로그인 확인 쿼리 에러(무시하고 계속): %s", str(me["errors"])[:200])
    else:
        user = (me.get("data") or {}).get("currentUser")
        if not user:
            raise RuntimeError("벨로그 인증 실패(currentUser=null): refresh_token 이 만료/오타입니다. "
                               "브라우저 쿠키(refresh_token, access_token)를 새로 복사해 시크릿을 갱신하세요.")
        username = user.get("username")
        log.info("   velog 로그인 확인: @%s", username)

    query = """
    mutation WritePost($title: String, $body: String, $tags: [String], $is_markdown: Boolean,
                       $is_temp: Boolean, $is_private: Boolean, $url_slug: String) {
        writePost(title: $title, body: $body, tags: $tags, is_markdown: $is_markdown,
                  is_temp: $is_temp, is_private: $is_private, url_slug: $url_slug) {
            id
            url_slug
        }
    }
    """
    slug = re.sub(r"[^0-9a-zA-Z가-힣]+", "-", title).strip("-")[:60] + f"-{int(time.time())}"
    variables = {"title": title, "body": md_body, "tags": ["IT", "기술", "가족나들이"],
                 "is_markdown": True, "is_temp": False, "is_private": False, "url_slug": slug}
    j = gql(query, variables)
    post = (j.get("data") or {}).get("writePost")
    if j.get("errors") or not post:
        raise RuntimeError(f"writePost 실패: {str(j)[:300]}")
    return f"https://velog.io/@{username}/{post.get('url_slug', slug)}" if username else f"slug={post.get('url_slug', slug)}"


# ---------------------------------------------------------
# 6. Selenium 플랫폼 (티스토리 / 네이버 / 브런치)  ※ 내 PC 전용
# ---------------------------------------------------------
def step(platform, msg):
    log.info("   ▶ [%s] %s", platform, msg)


def snap(driver, platform):
    """실패 시 화면/HTML 저장 → 어떤 화면에서 막혔는지 확인용."""
    try:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        base = os.path.join(LOG_DIR, f"fail_{platform}_{RUN_ID}")
        driver.save_screenshot(base + ".png")
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        log.error("   📸 [%s] 실패 화면 저장: %s.png / .html  (URL=%s)", platform, base, driver.current_url)
    except Exception as e:
        log.warning("   화면 저장 실패: %s", e)


def init_driver():
    if not SELENIUM_OK:
        raise RuntimeError("selenium 이 설치되어 있지 않습니다: pip install selenium")
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={CHROME_USER_DATA}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,1000")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    log.info("   Chrome 프로필: %s", CHROME_USER_DATA)
    try:
        return webdriver.Chrome(options=options)   # Selenium 4.6+ 가 드라이버 자동 관리
    except Exception as e:
        log.warning("   기본 드라이버 실패(%s) → webdriver_manager 로 재시도", str(e)[:150])
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager
        return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)


def wait_click(driver, css=None, xpath=None, timeout=10):
    loc = (By.CSS_SELECTOR, css) if css else (By.XPATH, xpath)
    el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable(loc))
    el.click()
    return el


def wait_el(driver, css, timeout=10):
    return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.CSS_SELECTOR, css)))


def dismiss_alert(driver, accept=True):
    try:
        a = driver.switch_to.alert
        a.accept() if accept else a.dismiss()
        return True
    except NoAlertPresentException:
        return False


def check_login(driver, platform, markers):
    url = driver.current_url
    if any(m in url for m in markers):
        raise RuntimeError(f"{platform} 로그인 세션이 없습니다 (현재 URL: {url}). "
                           f"먼저 `python auto_blog_poster.py --login-setup` 으로 로그인하세요.")


def login_setup():
    """최초 1회: 자동화용 크롬 프로필에 직접 로그인해 세션을 저장."""
    driver = init_driver()
    try:
        for name, url in [("티스토리(카카오 로그인)", "https://www.tistory.com/auth/login"),
                          ("네이버", "https://nid.naver.com/nidlogin.login"),
                          ("브런치(카카오 로그인)", "https://brunch.co.kr")]:
            driver.get(url)
            input(f"\n[{name}] 열린 크롬에서 로그인(2단계 인증 포함)을 끝낸 뒤 여기서 Enter ▶ ")
        print("\n로그인 세션이 프로필에 저장되었습니다. 이제 --platforms web 으로 실행하세요.")
    finally:
        driver.quit()


def post_to_tistory(driver, title, html_body):
    P = "tistory"
    if not TISTORY_BLOG_NAME:
        raise RuntimeError("TISTORY_BLOG_NAME 누락")
    step(P, "글쓰기 페이지 이동")
    driver.get(f"https://{TISTORY_BLOG_NAME}.tistory.com/manage/newpost/?type=post")
    time.sleep(2)
    if dismiss_alert(driver, accept=False):
        step(P, "'이어서 작성' 팝업 → 취소")
    check_login(driver, P, ["accounts.kakao.com", "/auth/login", "tistory.com/login"])
    step(P, f"현재 URL: {driver.current_url}")

    step(P, "제목 입력")
    wait_el(driver, "#post-title-inp", 15).send_keys(strip_non_bmp(title))

    step(P, "HTML 모드로 전환")
    wait_click(driver, css="#editor-mode-layer-btn-open")
    wait_click(driver, css="#editor-mode-html")
    time.sleep(1)
    if dismiss_alert(driver, accept=True):
        step(P, "모드 변경 확인창 → 확인")
    time.sleep(1)

    step(P, "본문(HTML) 주입")
    n = driver.execute_script(
        "var cm = document.querySelector('.CodeMirror'); if(!cm || !cm.CodeMirror) return -1;"
        "cm.CodeMirror.setValue(arguments[0]); return cm.CodeMirror.getValue().length;", html_body)
    if n is None or n < 0:
        raise RuntimeError("HTML 편집기(CodeMirror)를 찾지 못했습니다. 티스토리 편집기 UI 가 바뀐 것 같습니다.")
    step(P, f"본문 주입 확인: {n}자")

    step(P, "발행 설정 열기 → 공개 → 발행")
    wait_click(driver, css="#publish-layer-btn")
    wait_click(driver, css="#open20")
    wait_click(driver, css="#publish-btn")
    step(P, "발행 완료 대기")
    try:
        WebDriverWait(driver, 20).until(lambda d: "newpost" not in d.current_url)
    except Exception:
        raise RuntimeError("발행 버튼은 눌렀지만 글쓰기 화면을 벗어나지 못했습니다(발행 실패 가능). 저장된 화면을 확인하세요.")
    return driver.current_url


def post_to_naver(driver, title, plain_body):
    P = "naver"
    if not NAVER_BLOG_ID:
        raise RuntimeError("NAVER_BLOG_ID 누락")
    step(P, "글쓰기 페이지 이동")
    driver.get(f"https://blog.naver.com/{NAVER_BLOG_ID}?Redirect=Write")
    time.sleep(3)
    check_login(driver, P, ["nid.naver.com"])
    step(P, f"현재 URL: {driver.current_url}")
    try:
        try:
            WebDriverWait(driver, 10).until(EC.frame_to_be_available_and_switch_to_it((By.ID, "mainFrame")))
            step(P, "mainFrame 진입")
        except Exception:
            step(P, "mainFrame 없음 → 최상위 문서에서 진행")

        for css in (".se-popup-button-cancel", ".se-help-panel-close-button"):
            try:
                driver.find_element(By.CSS_SELECTOR, css).click()
                step(P, f"팝업 닫음: {css}")
                time.sleep(0.5)
            except Exception:
                pass

        step(P, "제목 입력")
        title_el = wait_el(driver, ".se-documentTitle .se-text-paragraph", 15)
        title_el.click()
        title_el.send_keys(strip_non_bmp(title))

        step(P, f"본문 입력 ({len(plain_body)}자)")
        body_el = wait_el(driver, ".se-component.se-text .se-text-paragraph", 10)
        body_el.click()
        body_el.send_keys(plain_body)

        step(P, "발행 버튼 클릭")
        wait_click(driver, css="button[class*='publish_btn']")
        time.sleep(1)
        step(P, "발행 확인 버튼 클릭")
        wait_click(driver, css="button[class*='confirm_btn']")
        step(P, "발행 완료 대기")
        try:
            WebDriverWait(driver, 20).until(
                lambda d: "Redirect=Write" not in d.current_url and "postwrite" not in d.current_url.lower())
        except Exception:
            raise RuntimeError("발행 버튼은 눌렀지만 글쓰기 화면을 벗어나지 못했습니다(발행 실패 가능). 저장된 화면을 확인하세요.")
        return driver.current_url
    finally:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass


def post_to_brunch(driver, title, plain_body):
    """브런치는 '작가 승인' 계정만 글쓰기가 가능. 기본은 임시저장까지만(BRUNCH_PUBLISH=1 이면 발행 시도)."""
    P = "brunch"
    step(P, "글쓰기 페이지 이동")
    driver.get("https://brunch.co.kr/write")
    time.sleep(3)
    check_login(driver, P, ["accounts.kakao.com"])
    step(P, f"현재 URL: {driver.current_url}")

    step(P, "제목 입력")
    title_el = wait_el(driver, ".cover_title, #wrapBody .title, textarea[placeholder*='제목']", 15)
    title_el.click()
    title_el.send_keys(strip_non_bmp(title))
    step(P, f"본문 입력 ({len(plain_body)}자)")
    body_el = wait_el(driver, ".wrap_body [contenteditable='true'], .wrap_body, .body_wrap", 10)
    body_el.click()
    body_el.send_keys(plain_body)

    step(P, "저장 버튼 클릭")
    wait_click(driver, xpath="//button[contains(.,'저장')]")
    if BRUNCH_PUBLISH:
        step(P, "발행 시도")
        time.sleep(1)
        wait_click(driver, xpath="//button[contains(.,'발행')]")
        time.sleep(1)
        wait_click(driver, xpath="//button[contains(.,'발행')][not(@disabled)]")
    time.sleep(2)
    return driver.current_url + ("" if BRUNCH_PUBLISH else "  (임시저장 시도 - 브런치 '내 서랍'에서 확인)")


# ---------------------------------------------------------
# 7. 설정 점검 / 실행 컨트롤러
# ---------------------------------------------------------
def check_config(targets):
    checks = {
        "blogger": [("BLOGGER_ID", bool(BLOGGER_ID)), (f"{BLOGGER_TOKEN_PATH} 파일", os.path.exists(BLOGGER_TOKEN_PATH))],
        "wordpress": [("WORDPRESS_URL", bool(WORDPRESS_URL)), ("WORDPRESS_USER", bool(WORDPRESS_USER)),
                      ("WORDPRESS_APP_PASS", bool(WORDPRESS_APP_PASS))],
        "velog": [("VELOG_REFRESH_TOKEN", bool(VELOG_REFRESH_TOKEN))],
        "tistory": [("TISTORY_BLOG_NAME", bool(TISTORY_BLOG_NAME)), ("selenium 설치", SELENIUM_OK)],
        "naver": [("NAVER_BLOG_ID", bool(NAVER_BLOG_ID)), ("selenium 설치", SELENIUM_OK)],
        "brunch": [("selenium 설치", SELENIUM_OK)],
    }
    log.info("── 설정 점검 ──")
    for t in targets:
        missing = [n for n, ok in checks[t] if not ok]
        if missing:
            log.warning("  ⚠️ %-10s 누락: %s", t, ", ".join(missing))
        else:
            log.info("  ✅ %-10s 설정 확인", t)
    log.info("  🖼 이미지 검색: %s", " → ".join(n for n, _ in image_providers()))


def expand_platforms(arg):
    out = []
    for p in [x.strip().lower() for x in arg.split(",") if x.strip()]:
        if p == "all":
            out += ALL_PLATFORMS
        elif p == "api":
            out += API_PLATFORMS
        elif p == "web":
            out += WEB_PLATFORMS
        elif p in ALL_PLATFORMS:
            out.append(p)
        else:
            log.warning("알 수 없는 플랫폼 무시: %s (사용 가능: %s)", p, ", ".join(ALL_PLATFORMS))
    seen = set()
    return [p for p in ALL_PLATFORMS if p in out and not (p in seen or seen.add(p))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="2026년 최신 IT 기술과 주말 가족 나들이")
    ap.add_argument("--platforms", default="all", help="all | api | web | blogger,wordpress,velog,tistory,naver,brunch")
    ap.add_argument("--dry-run", action="store_true", help="글만 생성하고 게시하지 않음")
    ap.add_argument("--login-setup", action="store_true", help="티스토리/네이버/브런치 로그인 세션 저장(내 PC 최초 1회)")
    args = ap.parse_args()

    if args.login_setup:
        login_setup()
        return

    targets = expand_platforms(args.platforms)
    log.info("로그 파일: %s", LOG_FILE)
    log.info("대상 플랫폼: %s", ", ".join(targets) or "(없음)")
    if not client:
        raise SystemExit("GEMINI_API_KEY 가 설정되지 않았습니다.")
    check_config(targets)

    t0 = time.time()
    log.info("── 글 생성 ──")
    title, md_body, html_body = generate_post(args.topic)
    plain_body = md_to_plain(md_body)
    n_img = len(re.findall(r"!\[.*?\]\(http", md_body))
    log.info("제목: %s", title)
    log.info("본문 %d자 / 이미지 %d장 / 생성 소요 %.1f초", len(plain_body), n_img, time.time() - t0)

    if args.dry_run:
        print("\n===== TITLE =====\n", title, "\n\n===== MARKDOWN =====\n", md_body)
        return

    results = {}

    def run(name, fn, *a, driver=None):
        if name not in targets:
            return
        log.info("── %s 게시 시작 ──", name)
        t = time.time()
        try:
            results[name] = ("OK", fn(*a))
            log.info("✅ %s 완료 (%.1f초): %s", name, time.time() - t, results[name][1])
        except Exception as e:
            results[name] = ("FAIL", str(e))
            log.error("❌ %s 실패 (%.1f초): %s", name, time.time() - t, e)
            if driver:
                snap(driver, name)

    run("blogger", post_to_blogger, title, html_body)
    run("wordpress", post_to_wordpress, title, html_body)
    run("velog", post_to_velog, title, md_body)

    if any(p in targets for p in WEB_PLATFORMS):
        driver = None
        try:
            log.info("── 브라우저 시작 ──")
            driver = init_driver()
            run("tistory", post_to_tistory, driver, title, html_body, driver=driver)
            run("naver", post_to_naver, driver, title, plain_body, driver=driver)
            run("brunch", post_to_brunch, driver, title, plain_body, driver=driver)
        except Exception as e:
            log.error("❌ 브라우저 시작 실패 (같은 프로필의 Chrome 이 열려 있으면 모두 닫으세요): %s", e)
            for p in WEB_PLATFORMS:
                if p in targets and p not in results:
                    results[p] = ("FAIL", f"브라우저 시작 실패: {e}")
        finally:
            if driver:
                driver.quit()

    print("\n===== 결과 요약 =====")
    for name in ALL_PLATFORMS:
        if name in results:
            status, info = results[name]
            print(f"{'✅' if status == 'OK' else '❌'} {name:10s} {info}")
    print(f"\n상세 로그: {LOG_FILE}")
    if any(s == "FAIL" for s, _ in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
