name: Auto Blog Poster

on:
  schedule:
    # 매일 한국시간 오전 8시 30분 (UTC 23:30) 자동 실행
    - cron: '30 23 * * *'
  workflow_dispatch: # GitHub Actions 탭에서 수동 즉시 실행 버튼 활성화

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install Google Chrome for Headless Selenium
        uses: browser-actions/setup-chrome@v1

      - name: Install dependencies
        run: |
          pip install google-genai requests markdown google-api-python-client google-auth selenium

      - name: Run Auto Blog Poster (aaroneom & lhimepapa)
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          TISTORY_IT_BLOG_NAME: aaroneom
          TISTORY_FAMILY_BLOG_NAME: lhimepapa
          TISTORY_COOKIES_JSON: ${{ secrets.TISTORY_COOKIES_JSON }}
          BLOGGER_ID: ${{ secrets.BLOGGER_ID }}
          BLOGGER_TOKEN_JSON: ${{ secrets.BLOGGER_TOKEN_JSON }}
        run: |
          python auto_blog_poster.py --platforms tistory

      - name: Upload failure screenshots (if any)
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: error-logs
          path: logs/
