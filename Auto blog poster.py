"""
7대 블로그 플랫폼 자동 포스팅 (수정본)

설치:
    pip install google-genai requests markdown selenium google-api-python-client google-auth

사용:
    python auto_blog_poster.py --dry-run                       # 글 생성만 (게시 안 함, 먼저 이걸로 테스트)
    python auto_blog_poster.py --platforms blogger,wordpress   # 일부만 게시
    python auto_blog_poster.py --topic "주말 가족 나들이 + 최신 IT"  # 주제 지정
"""
import os
import re
import time
import argparse
import logging

import requests
import markdown
from google import genai

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoAlertPresentException

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("autopost")

# ---------------------------------------------------------
# 1. 환경 변수
# ---------------------------------------------------------
env = os.environ.get
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")   # 모델명이 바뀌면 환경변수로만 교체
GOOGLE_SEARCH_API_KEY = os.environ.get("GOOGLE_SEARCH_API_KEY")
GOOGLE_CSE_CX = os.environ.get("GOOGLE_CSE_CX")  # 맞춤검색 엔진 ID

BLOGGER_ID = env("BLOGGER_ID")
BLOGGER_TOKEN_PATH = env("BLOGGER_TOKEN_PATH", "token.json")
WORDPRESS_URL = (env("WORDPRESS_URL") or "").rstrip("/")
WORDPRESS_USER = env("WORDPRESS_USER")
WORDPRESS_APP_PASS = env("WORDPRESS_APP_PASS")
MEDIUM_TOKEN = env("MEDIUM_TOKEN")
MEDIUM_USER_ID = env("MEDIUM_USER_ID")                   # 비워두면 /me 로 자동 조회
VELOG_ACCESS_TOKEN = env("VELOG_ACCESS_TOKEN")           # 쿠키 access_token
VELOG_REFRESH_TOKEN = env("VELOG_REFRESH_TOKEN") or env("VELOG_SESSION_TOKEN")  # 쿠키 refresh_token

CHROME_USER_DATA = env("CHROME_USER_DATA_PATH", os.path.abspath("./chrome_profile"))
TISTORY_BLOG_NAME = env("TISTORY_BLOG_NAME")
NAVER_BLOG_ID = env("NAVER_BLOG_ID")
BRUNCH_PUBLISH = env("BRUNCH_PUBLISH", "1") == "1"       # 기본: 임시저장만

HTTP_TIMEOUT = 30
ALL_PLATFORMS = ["blogger", "wordpress", "medium", "velog", "tistory", "naver", "brunch"]

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
    t = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", t)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__|\*|`)", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return strip_non_bmp(t).strip()


def get_google_naver_image(query):
    """구글 검색을 거쳐 네이버 블로그/뉴스에 포함된 실제 사진 URL 추출"""
    if not (GOOGLE_SEARCH_API_KEY and GOOGLE_CSE_CX):
        return None

    # 구글 검색어 뒤에 site:blog.naver.com 을 붙여 네이버 출처 이미지로 한정
    search_query = f"{query} site:blog.naver.com"
    url = "https://www.googleapis.com/customsearch/v1"
    
    params = {
        "key": GOOGLE_SEARCH_API_KEY,
        "cx": GOOGLE_CSE_CX,
        "q": search_query,
        "searchType": "image",  # 이미지 검색 전용
        "num": 3,
        "safe": "active"
    }

    try:
        res = requests.get(url, params=params, timeout=10)
        res.raise_for_status()
        items = res.json().get("items", [])

        for item in items:
            img_url = item.get("link", "")
            # 네이버 이미지 서버(postfiles, blogpf, naver.net 등) 링크 검증
            if "naver" in img_url or "postfiles" in img_url:
                # 핫링크 깨짐 방지용 간단한 HEAD 요청 검증
                try:
                    h = requests.head(img_url, timeout=5, allow_redirects=True)
                    if h.status_code < 400 and "image" in h.headers.get("Content-Type", ""):
                        return img_url
                except requests.RequestException:
                    continue
            else:
                return img_url  # 일반 고화질 관련 이미지 링크 반환

    except Exception as e:
        log.warning("구글-네이버 이미지 수급 실패 (%s): %s", query, e)

    return None

# ---------------------------------------------------------
# 3. 콘텐츠 생성
# ---------------------------------------------------------
def generate_post(topic):
    prompt = f"""
14년 차 IT 전문가이자 두 아이의 아빠 관점에서 '{topic}' 관련 블로그 포스팅을 한국어로 작성하세요.

[형식 규칙]
- 첫 줄: 'TITLE: 제목' (제목만, 마크다운 기호 금지)
- 둘째 줄부터 마크다운 본문 (## 소제목 사용, 1500~2500자)
- 사진이 필요한 위치에 [IMG: 검색키워드] 를 3개 넣으세요.
  키워드는 네이버 이미지 검색에 바로 쓸 수 있는 구체적인 장소/사물 이름입니다.
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
    if not title:  # 형식을 안 지킨 경우 첫 줄을 제목으로
        title = re.sub(r"^[#*\s]+|[*\s]+$", "", lines[0])
        body_start = 1
    body = "\n".join(lines[body_start:]).strip()

    def repl(m):
        kw = m.group(1).strip()
        url = get_naver_image(kw)
        return f"\n\n![{kw}]({url})\n\n" if url else ""

    md_body = re.sub(r"\[IMG:\s*(.+?)\]", repl, body)
    html_body = markdown.markdown(md_body, extensions=["extra", "nl2br"])
    html_body = html_body.replace("<img ", '<img style="max-width:100%;height:auto;" ')
    return title, md_body, html_body


# ---------------------------------------------------------
# 4. API 모듈 4종
# ---------------------------------------------------------
def post_to_blogger(title, html_body):
    if not BLOGGER_ID:
        raise RuntimeError("BLOGGER_ID 가 설정되지 않았습니다.")
    scopes = ["https://www.googleapis.com/auth/blogger"]
    creds = Credentials.from_authorized_user_file(BLOGGER_TOKEN_PATH, scopes)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(BLOGGER_TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError("token.json 이 만료/무효입니다. OAuth 를 다시 진행하세요.")
    service = build("blogger", "v3", credentials=creds, cache_discovery=False)
    body = {"kind": "blogger#post", "title": title, "content": html_body}
    res = service.posts().insert(blogId=BLOGGER_ID, body=body, isDraft=False).execute()
    return res.get("url")


def post_to_wordpress(title, html_body):
    if not (WORDPRESS_URL and WORDPRESS_USER and WORDPRESS_APP_PASS):
        raise RuntimeError("워드프레스 환경변수 누락")
    res = requests.post(
        f"{WORDPRESS_URL}/wp-json/wp/v2/posts",
        json={"title": title, "content": html_body, "status": "publish"},
        auth=(WORDPRESS_USER, WORDPRESS_APP_PASS),
        timeout=HTTP_TIMEOUT,
    )
    if res.status_code != 201:
        raise RuntimeError(f"{res.status_code} {res.text[:300]}")
    return res.json().get("link")


def post_to_medium(title, html_body):
    """주의: Medium 은 2023년부터 신규 Integration Token 발급을 중단했습니다.
    기존에 발급받아 둔 토큰이 있어야만 동작합니다."""
    if not MEDIUM_TOKEN:
        raise RuntimeError("MEDIUM_TOKEN 누락 (신규 발급 불가, 기존 토큰만 사용 가능)")
    headers = {"Authorization": f"Bearer {MEDIUM_TOKEN}", "Content-Type": "application/json", "Accept": "application/json"}
    user_id = MEDIUM_USER_ID
    if not user_id:
        me = requests.get("https://api.medium.com/v1/me", headers=headers, timeout=HTTP_TIMEOUT)
        me.raise_for_status()
        user_id = me.json()["data"]["id"]
    payload = {"title": title, "contentFormat": "html",
               "content": f"<h1>{title}</h1>{html_body}", "publishStatus": "public"}
    res = requests.post(f"https://api.medium.com/v1/users/{user_id}/posts", json=payload,
                        headers=headers, timeout=HTTP_TIMEOUT)
    if res.status_code not in (200, 201):
        raise RuntimeError(f"{res.status_code} {res.text[:300]}")
    return res.json()["data"]["url"]


def post_to_velog(title, md_body):
    """비공식 GraphQL API 라서 스키마가 바뀌면 깨질 수 있습니다."""
    if not VELOG_REFRESH_TOKEN:
        raise RuntimeError("VELOG_REFRESH_TOKEN 누락")
    cookie = f"refresh_token={VELOG_REFRESH_TOKEN}"
    if VELOG_ACCESS_TOKEN:
        cookie = f"access_token={VELOG_ACCESS_TOKEN}; " + cookie
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
    res = requests.post("https://v2.velog.io/graphql", json={"query": query, "variables": variables},
                        headers={"Cookie": cookie, "Content-Type": "application/json"}, timeout=HTTP_TIMEOUT)
    j = res.json()
    if j.get("errors") or not (j.get("data") or {}).get("writePost"):
        raise RuntimeError(str(j)[:300])
    return f"https://velog.io/@/{j['data']['writePost']['url_slug']}"


# ---------------------------------------------------------
# 5. Selenium 모듈 3종
# ---------------------------------------------------------
def init_driver():
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={CHROME_USER_DATA}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,1000")
    try:
        return webdriver.Chrome(options=options)  # Selenium 4.6+ 는 드라이버 자동 관리
    except Exception:
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


def post_to_tistory(driver, title, html_body):
    """티스토리 Open API 는 종료되어 Selenium 이 맞습니다. 사전에 프로필로 1회 수동 로그인 필요."""
    if not TISTORY_BLOG_NAME:
        raise RuntimeError("TISTORY_BLOG_NAME 누락")
    driver.get(f"https://{TISTORY_BLOG_NAME}.tistory.com/manage/newpost/?type=post")
    time.sleep(2)
    dismiss_alert(driver, accept=False)          # '저장된 글이 있습니다. 이어서 작성?' 팝업은 취소
    wait_el(driver, "#post-title-inp", 15).send_keys(strip_non_bmp(title))

    wait_click(driver, css="#editor-mode-layer-btn-open")
    wait_click(driver, css="#editor-mode-html")
    time.sleep(1)
    dismiss_alert(driver, accept=True)           # '모드 변경 시 서식 유실' 확인창
    time.sleep(1)
    # CodeMirror 는 textarea.value 를 바꿔도 반영되지 않으므로 인스턴스 API 사용
    driver.execute_script("document.querySelector('.CodeMirror').CodeMirror.setValue(arguments[0]);", html_body)

    wait_click(driver, css="#publish-layer-btn")
    wait_click(driver, css="#open20")            # 공개
    wait_click(driver, css="#publish-btn")
    time.sleep(3)
    return driver.current_url


def post_to_naver(driver, title, plain_body):
    if not NAVER_BLOG_ID:
        raise RuntimeError("NAVER_BLOG_ID 누락")
    driver.get(f"https://blog.naver.com/{NAVER_BLOG_ID}?Redirect=Write")
    time.sleep(3)
    try:
        driver.switch_to.frame("mainFrame")
        # 이전 작성글 이어쓰기 / 도움말 팝업 닫기 (없으면 무시)
        for css in (".se-popup-button-cancel", ".se-help-panel-close-button"):
            try:
                driver.find_element(By.CSS_SELECTOR, css).click()
                time.sleep(0.5)
            except Exception:
                pass

        title_el = wait_el(driver, ".se-documentTitle .se-text-paragraph", 15)
        title_el.click()
        title_el.send_keys(strip_non_bmp(title))

        body_el = wait_el(driver, ".se-component.se-text .se-text-paragraph", 10)
        body_el.click()
        body_el.send_keys(plain_body)

        # 해시된 클래스명(publish_btn__xxxx)은 수시로 바뀌므로 부분 일치 사용
        wait_click(driver, css="button[class*='publish_btn']")
        time.sleep(1)
        wait_click(driver, css="button[class*='confirm_btn']")
        time.sleep(3)
        return driver.current_url
    finally:
        driver.switch_to.default_content()


def post_to_brunch(driver, title, plain_body):
    """브런치는 '작가 승인' 계정만 글쓰기가 가능하고 발행 단계가 여러 번이라 기본은 임시저장만 합니다.
    BRUNCH_PUBLISH=1 로 두면 발행 버튼까지 시도합니다."""
    driver.get("https://brunch.co.kr/write")
    time.sleep(3)
    title_el = wait_el(driver, ".cover_title, #wrapBody .title, textarea[placeholder*='제목']", 15)
    title_el.click()
    title_el.send_keys(strip_non_bmp(title))
    body_el = wait_el(driver, ".wrap_body [contenteditable='true'], .wrap_body, .body_wrap", 10)
    body_el.click()
    body_el.send_keys(plain_body)
    wait_click(driver, xpath="//button[contains(.,'저장')]")
    if BRUNCH_PUBLISH:
        time.sleep(1)
        wait_click(driver, xpath="//button[contains(.,'발행')]")
        time.sleep(1)
        wait_click(driver, xpath="//button[contains(.,'발행')][not(@disabled)]")
    time.sleep(2)
    return driver.current_url


# ---------------------------------------------------------
# 6. 실행 컨트롤러
# ---------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="2026년 최신 IT 기술과 주말 가족 나들이")
    ap.add_argument("--platforms", default=",".join(ALL_PLATFORMS))
    ap.add_argument("--dry-run", action="store_true", help="글만 생성하고 게시하지 않음")
    args = ap.parse_args()
    targets = [p.strip() for p in args.platforms.split(",") if p.strip() in ALL_PLATFORMS]

    if not client:
        raise SystemExit("GEMINI_API_KEY 가 설정되지 않았습니다.")

    log.info("글 생성 중...")
    title, md_body, html_body = generate_post(args.topic)
    plain_body = md_to_plain(md_body)
    log.info("제목: %s (본문 %d자)", title, len(plain_body))

    if args.dry_run:
        print("\n===== TITLE =====\n", title, "\n\n===== MARKDOWN =====\n", md_body)
        return

    results = {}

    def run(name, fn, *a):
        if name not in targets:
            return
        try:
            results[name] = ("OK", fn(*a))
            log.info("✅ %s 완료: %s", name, results[name][1])
        except Exception as e:
            results[name] = ("FAIL", str(e))
            log.error("❌ %s 실패: %s", name, e)

    run("blogger", post_to_blogger, title, html_body)
    run("wordpress", post_to_wordpress, title, html_body)
    run("medium", post_to_medium, title, html_body)
    run("velog", post_to_velog, title, md_body)

    web_targets = [p for p in ("tistory", "naver", "brunch") if p in targets]
    if web_targets:
        driver = None
        try:
            driver = init_driver()
            run("tistory", post_to_tistory, driver, title, html_body)
            run("naver", post_to_naver, driver, title, plain_body)
            run("brunch", post_to_brunch, driver, title, plain_body)
        except Exception as e:
            log.error("브라우저 초기화 실패 (같은 프로필의 Chrome 이 열려 있으면 닫으세요): %s", e)
        finally:
            if driver:
                driver.quit()

    print("\n===== 결과 요약 =====")
    for name in ALL_PLATFORMS:
        if name in results:
            status, info = results[name]
            print(f"{'✅' if status == 'OK' else '❌'} {name:10s} {info}")


if __name__ == "__main__":
    main()
