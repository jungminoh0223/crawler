from playwright.sync_api import sync_playwright

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        # 1. KT 메인 페이지 접속
        try:
            page.goto("https://www.kt.com", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"페이지 로딩 실패: {e}")
            browser.close()
            exit()

        print("Page Title:", page.title())

        # 2. 화면에 보이는 모든 a 태그에 hover 시도
        try:
            links = page.locator("a:visible")
            count = links.count()
            print(f"visible a 태그 수: {count}")

            for i in range(count):
                link = links.nth(i)
                try:
                    link.hover(timeout=500)
                    page.wait_for_timeout(100)
                except:
                    pass  # hover 안 되는 애들은 무시
        except Exception as e:
            print(f"링크 처리 중 에러: {e}")

        # 3. 버튼도 클릭 시도
        try:
            buttons = page.locator("button:visible")
            button_count = buttons.count()
            print(f"visible button 태그 수: {button_count}")

            for i in range(button_count):
                button = buttons.nth(i)
                try:
                    button.click(timeout=500)
                    page.wait_for_timeout(100)
                except:
                    pass  # 클릭 안 되는 애들은 무시
        except Exception as e:
            print(f"버튼 처리 중 에러: {e}")

        # 4. 메뉴가 전부 열린 상태의 DOM 저장
        try:
            html = page.content()
            
            with open("kt_menu_full_dom.html", "w", encoding="utf-8") as f:
                f.write(html)
            
            print("메뉴 DOM HTML 저장 완료")
        except Exception as e:
            print(f"파일 저장 실패: {e}")

        browser.close()

except Exception as e:
    print(f"전체 프로세스 실패: {e}")