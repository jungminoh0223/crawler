import json
import asyncio
import logging
import re
from copy import copy
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 스킵할 URL single에선 제외
SKIP_CRAWL_PATTERNS = [
]

# 제외할 이름 패턴
EXCLUDE_NAME_PATTERNS = [
    r'^_?닫기_?$', r'^주문하기$', r'^이전\s*다음$', r'^확인$', r'^동의$', r'^검색$',
    r'^레이어\s*닫기$', r'^목록$', r'^장바구니$', r'^마이샵$', r'^로그인$', r'^회원가입$',
    r'^자세히\s*보기$', r'^더보기$', r'^TOP$', r'^맨위로$', r'^공유하기$', r'^찜하기$',
    r'^비교하기$', r'^바로가기$', r'^이전$', r'^다음$', r'^이전글$', r'^다음글$',
    r'^HOME$', r'^홈$', r'^메뉴$', r'^전체메뉴$', r'^레이어\s*팝업$', r'^팝업\s*닫기$',
    r'^SNS\s*공유$', r'^카카오톡$', r'^페이스북$', r'^트위터$', r'^링크\s*복사$',
    r'신청하기$', r'상담\s*신청', r'가입\s*상담',
]

# 제외할 URL 패턴
EXCLUDE_URL_PATTERNS = [
    '/mobile/view.do',
    '/accessory/accsProductView.do',
    '/orderCartView.do',
    '/orderHistory.do',
    '/wDic/productDetail.do',
    '/login',
    '/member/',
    'javascript:',
    '#',
]

# 링크 추출 전 제거할 요소 (헤더/푸터/메뉴/배너 등)
DECOMPOSE_SELECTORS = [
    '#cfmClHeader', '#cfmClFooter', '#cfmClSkip', '.header', '.footer',
    '.navigation', '.sidebar', '.banner', '.popup', '.overlay', '.sns-area', '.location',
    '.gnb', '.lnb', '.snb', '.util', '.quick', '.ui-tab-lst', '.ui-tab-top-lst',
    '.btn_auto_ga',
    'a[class*="_link"]',  # h-next_link, h-special_link 등
    'a[class*="link-"]',  # link-black, link-point 등
]

# 텍스트 추출 시 제외할 셀렉터 (링크 내부의 불필요한 요소)
EXCLUDE_TEXT_SELECTORS = [
    '.date', '.txt', '.desc', '.category', '.tag', '.badge', '.icon',
    '.num', '.count', '.view', '.hit',
    'span.sub', 'em.sub', '.sub-txt',
    '.blind', '.sr-only', '.hidden', '.hidetxt',
    '.btn', '.more',
    '.linked', '.breadcrumb', '.path', 
]

def should_skip(url):
    return any(p in url for p in SKIP_CRAWL_PATTERNS)

def is_kt_domain(url):
    """kt.com 도메인인지 체크 (모든 서브도메인 허용)"""
    return url.startswith('http') and '.kt.com' in url

def get_base_url(url):
    """URL에서 도메인 부분만 추출 (예: https://shop.kt.com/mobile/plan.do → https://shop.kt.com)"""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"

def is_excluded_name(name):
    return any(re.match(p, name.strip(), re.IGNORECASE) for p in EXCLUDE_NAME_PATTERNS)

def is_excluded_url(url):
    return any(p in url for p in EXCLUDE_URL_PATTERNS)

def extract_links_from_soup(soup, base_url, min_count=1):
    """링크 추출"""
    links, seen = [], set()

    # DECOMPOSE_SELECTORS 적용 (a.link-black 등 제거)
    for sel in DECOMPOSE_SELECTORS:
        for el in soup.select(sel):
            el.decompose()

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith('/'):
            href = f"{base_url}{href}"

        if not is_kt_domain(href):
            continue
        if is_excluded_url(href) or href in seen:
            continue

        # onclick="javascript:..." 있으면 제외 (탭/버튼 등 UI 요소)
        onclick = a.get('onclick', '')
        if 'javascript:' in onclick.lower():
            continue

        text = ''

        # 0. 링크 내에 보이는 텍스트가 있는지 먼저 체크 (.blind 등 제거 후)
        a_check = copy(a)
        for sel in EXCLUDE_TEXT_SELECTORS:
            for el in a_check.select(sel):
                el.decompose()
        visible_text = a_check.get_text(strip=True)
        if not visible_text:
            # 보이는 텍스트가 없으면 스킵 (EVENT_LABEL 있어도 무시)
            continue

        # 1. .plan_tit em
        parent_li = a.find_parent('li')
        if parent_li:
            tit = parent_li.select_one('.plan_tit em')
            if tit:
                text = tit.get_text(strip=True)

        # 2. EVENT_LABEL
        if not text:
            onclick = a.get('onclick', '')
            if onclick and 'EVENT_LABEL' in onclick:
                match = re.search(r"EVENT_LABEL\s*:\s*'([^']+)'", onclick)
                if match:
                    label = match.group(1)
                    text = label.rsplit('_', 1)[0]

        # 3. 링크 텍스트 (불필요한 요소 제거 후 추출)
        if not text:
            a_copy = copy(a)
            for sel in EXCLUDE_TEXT_SELECTORS:
                for el in a_copy.select(sel):
                    el.decompose()
            text = a_copy.get_text(strip=True)

        # 4. title 속성
        if not text:
            text = a.get('title', '').strip()

        if not text or len(text) < 2 or is_excluded_name(text):
            continue

        if len(text) > 150:
            text = text[:150] + '...'

        seen.add(href)
        links.append({'name': text, 'url': href})

    return links if len(links) >= min_count else []


async def extract_with_pagination(frame):
    """페이지네이션이 있는 프레임에서 모든 페이지 추출"""
    all_links, seen = [], set()
    page_num = 1
    base_url = get_base_url(frame.url)

    while True:
        logger.info(f"📄 {page_num}페이지 수집...")

        html = await frame.content()
        soup = BeautifulSoup(html, 'html.parser')

        count_before = len(all_links)

        # projectList 안의 링크만(상품 추출)
        for li in soup.select('ul.projectList li'):
            a = li.select_one('a[href]')
            if not a:
                continue

            href = a.get('href', '')
            if href.startswith('/'):
                href = f"{base_url}{href}"
            if href in seen or is_excluded_url(href):
                continue

            # 제목 추출
            tit = li.select_one('.plan_tit em')
            text = tit.get_text(strip=True) if tit else ''
            if not text:
                img = a.find('img')
                text = img.get('alt', '').strip() if img else ''
            if not text:
                text = a.get_text(strip=True)

            if text and len(text) >= 2:
                seen.add(href)
                all_links.append({'name': text, 'url': href})

        added = len(all_links) - count_before
        logger.info(f"   → +{added}개 (총 {len(all_links)}개)")

        if added == 0 and page_num > 1:
            break

        next_btn = await frame.query_selector(f'a[pageno="{page_num + 1}"]')
        if not next_btn:
            break

        await next_btn.click()
        await frame.page.wait_for_timeout(2000)
        page_num += 1

    return all_links


async def extract_with_pagination_generic(frame):
    """범용 페이지네이션 처리 (모든 li a를 수집)"""
    all_links, seen = [], set()
    page_num = 1
    base_url = get_base_url(frame.url)

    while True:  # 자연 종료 조건으로 탈출
        logger.info(f"📄 {page_num}페이지 수집...")

        html = await frame.content()
        soup = BeautifulSoup(html, 'html.parser')

        # 헤더/푸터/탭 제거
        for sel in DECOMPOSE_SELECTORS:
            for el in soup.select(sel):
                el.decompose()

        count_before = len(all_links)

        # 모든 a[href] 수집 (li 안뿐만 아니라 모든 a 태그)
        for a in soup.find_all('a', href=True):
            href = a.get('href', '').strip()

            # 상대 경로 처리
            if href.startswith('/'):
                href = f"{base_url}{href}"

            # 제외 패턴 체크
            if not href.startswith('http') or is_excluded_url(href) or href in seen:
                continue

            # kt.com 도메인 체크
            if not is_kt_domain(href):
                continue

            # 텍스트 추출
            text = a.get_text(strip=True)
            if text and len(text) >= 2 and not is_excluded_name(text):
                seen.add(href)
                all_links.append({'name': text, 'url': href})

        added = len(all_links) - count_before
        logger.info(f"   → +{added}개 (총 {len(all_links)}개)")

        if added == 0 and page_num > 1:
            break

        next_btn = await frame.query_selector(f'a[pageno="{page_num + 1}"]')
        if not next_btn:
            break

        await next_btn.click()
        await frame.page.wait_for_timeout(2000)
        page_num += 1

    return all_links


async def try_extract_from_iframes(page):
    """iframe에서 목록 추출 (HTML 내용 기반)"""

    logger.info(f"🔍 총 {len(page.frames)}개의 프레임 발견")

    # 1차: projectList + 페이지네이션 있는 iframe
    for i, frame in enumerate(page.frames):
        try:
            html = await frame.content()
            soup = BeautifulSoup(html, 'html.parser')

            project_links = soup.select('ul.projectList li a[href]')
            ui_slide_links = soup.select('ul.ui-slide li a[href]')
            has_pageno = soup.select_one('a[pageno]')
            has_bx_pager = soup.select_one('.bx-pager')
            all_ul = soup.find_all('ul', class_=True)

            logger.info(f"  📄 Frame {i}:")
            logger.info(f"     - projectList: {len(project_links)}개")
            logger.info(f"     - ui-slide: {len(ui_slide_links)}개")
            logger.info(f"     - pageno: {'있음' if has_pageno else '없음'}")
            logger.info(f"     - bx-pager: {'있음' if has_bx_pager else '없음'}")
            logger.info(f"     - ul 태그: {len(all_ul)}개 (클래스: {[ul.get('class') for ul in all_ul[:5]]})")

            # Frame 1 상세 분석 (pageno가 있는 프레임)
            if has_pageno:
                all_li = soup.find_all('li')
                li_with_a = [li for li in all_li if li.find('a', href=True)]
                logger.info(f"     🔍 Frame {i} 상세:")
                logger.info(f"        - 전체 li: {len(all_li)}개")
                logger.info(f"        - 링크 있는 li: {len(li_with_a)}개")
                if li_with_a:
                    sample_li = li_with_a[0]
                    sample_a = sample_li.find('a', href=True)
                    logger.info(f"        - 샘플 li 클래스: {sample_li.get('class')}")
                    logger.info(f"        - 샘플 a href: {sample_a.get('href')[:100] if sample_a else 'None'}")
                    logger.info(f"        - 샘플 텍스트: {sample_li.get_text(strip=True)[:100]}")

            if len(project_links) >= 3 and has_pageno:
                logger.info(f"🔍 iframe {i}에서 목록+페이지네이션 발견")
                return await extract_with_pagination(frame)
        except Exception as e:
            logger.error(f"  ❌ Frame {i} 처리 실패: {e}")
            continue

    # 2차: pageno가 있는 프레임 우선 처리 (게시판 목록 - 페이지네이션 처리)
    for i, frame in enumerate(page.frames):
        try:
            html = await frame.content()
            soup = BeautifulSoup(html, 'html.parser')

            has_pageno = soup.select_one('a[pageno]')
            if has_pageno:
                # li가 많은지 확인
                all_li_with_a = soup.select('li a[href]')
                logger.info(f"  📄 Frame {i} pageno 프레임: li a 개수 = {len(all_li_with_a)}")

                if len(all_li_with_a) >= 3:
                    logger.info(f"🔍 iframe {i}에서 pageno 목록 발견, 페이지네이션 처리 시작...")
                    links = await extract_with_pagination_generic(frame)
                    if links:
                        logger.info(f"✅ iframe {i}에서 {len(links)}개 추출 완료")
                        return links
        except Exception as e:
            logger.error(f"  ❌ Frame {i} pageno 처리 실패: {e}")
            continue

    # 3차: projectList 또는 ui-slide만 있는 iframe
    for i, frame in enumerate(page.frames):
        try:
            html = await frame.content()
            soup = BeautifulSoup(html, 'html.parser')

            project_links = soup.select('ul.projectList li a[href]')
            ui_slide_links = soup.select('ul.ui-slide li a[href]')

            if len(project_links) >= 3 or len(ui_slide_links) >= 3:
                base_url = get_base_url(frame.url)
                links = extract_links_from_soup(soup, base_url=base_url, min_count=3)
                if links:
                    pattern_name = 'projectList' if project_links else 'ui-slide'
                    logger.info(f"🔍 iframe {i}에서 목록 발견 ({pattern_name}): {len(links)}개")
                    return links
        except:
            continue

    return []


async def extract_tabs(page):
    """탭 링크 추출"""
    html = await page.content()
    soup = BeautifulSoup(html, 'html.parser')
    base_url = get_base_url(page.url)

    tabs, seen = [], set()
    for link in soup.find_all('a', href=True):
        img = link.find('img')
        if not img or 'tab' not in img.get('src', '').lower():
            continue

        href = link['href']
        if href.startswith('/'):
            href = f"{base_url}{href}"
        if not href.startswith('http') or href in seen:
            continue

        name = img.get('alt', '') or link.get_text(strip=True)
        if name:
            seen.add(href)
            tabs.append({'name': name, 'url': href})

    if len(tabs) >= 2:
        logger.info(f"🏷️ 탭 발견: {len(tabs)}개")
        return tabs
    return []


async def extract_products(page):
    """상품 추출 (페이지네이션 포함)"""
    base_url = get_base_url(page.url)
    products = []
    seen = set()
    page_num = 1

    while True:
        logger.info(f"📄 상품 페이지 {page_num} 처리 중...")

        html = await page.content()
        soup = BeautifulSoup(html, 'html.parser')
        count_before = len(products)

        # 방식 1: <input name="prodAttr"> 태그
        for inp in soup.find_all('input', {'name': 'prodAttr'}):
            name, no = inp.get('prodnm', ''), inp.get('prodno', '')
            if name and no and no not in seen:
                seen.add(no)
                products.append({
                    'name': name,
                    'url': f"{base_url}/display/olhsGoodsDtl.do?goodsCode={no}"
                })

        # 방식 2: <a prodno="..."> 태그 (onclick="goProdDetail('...')")
        for a in soup.find_all('a', attrs={'prodno': True}):
            no = a.get('prodno', '')
            if not no or no in seen:
                continue

            # 상품명 추출: 부모 <li>에서 prd-tit 또는 img alt 찾기
            name = ''
            parent_li = a.find_parent('li')
            if parent_li:
                tit = parent_li.select_one('.prd-tit')
                if tit:
                    name = tit.get_text(strip=True)
                if not name:
                    img = parent_li.select_one('img[alt]')
                    if img:
                        name = img.get('alt', '').strip()

            # 부모 li에서 못 찾으면 a 태그 내에서 시도 (fallback)
            if not name:
                tit = a.find(class_='prd-tit')
                if tit:
                    name = tit.get_text(strip=True)
            if not name:
                img = a.find('img')
                if img:
                    name = img.get('alt', '').strip()

            if name and no:
                seen.add(no)
                products.append({
                    'name': name,
                    'url': f"{base_url}/display/olhsGoodsDtl.do?goodsCode={no}"
                })

        added = len(products) - count_before
        logger.info(f"   → 페이지 {page_num}: +{added}개 (총 {len(products)}개)")

        # 디버깅: 현재 페이지의 페이지네이션 버튼들 확인
        all_page_btns = await page.locator('a[pageno]').all()
        pageno_list = []
        for btn in all_page_btns:
            pn = await btn.get_attribute('pageno')
            pageno_list.append(pn)
        logger.info(f"   🔍 현재 페이지네이션: {pageno_list}")

        # 다음 페이지 버튼 찾기
        next_btn = page.locator(f'a[pageno="{page_num + 1}"]').first
        next_count = await next_btn.count()

        if next_count == 0:
            logger.info(f"   → 더 이상 페이지 없음 (pageno={page_num + 1} 없음)")
            break

        await next_btn.click()
        await page.wait_for_timeout(2000)
        page_num += 1

    if products:
        logger.info(f"📦 상품 총 {len(products)}개 발견")
    return products


async def extract_from_titled_iframe(page, iframe_title):
    """특정 title의 iframe에서 모든 링크 추출 (페이지네이션 포함)"""
    logger.info(f"🔍 iframe[title=\"{iframe_title}\"] 찾는 중...")

    try:
        # iframe 찾기
        iframe_locator = page.locator(f'iframe[title="{iframe_title}"]')

        # iframe이 존재하는지 확인
        count = await iframe_locator.count()
        if count == 0:
            logger.info(f"   ❌ iframe[title=\"{iframe_title}\"] 없음")
            return []

        logger.info(f"   ✅ iframe[title=\"{iframe_title}\"] 발견!")

        # content_frame 가져오기
        frame = iframe_locator.content_frame

        # iframe 콘텐츠 로딩 대기
        await page.wait_for_timeout(2000)

        # 디버깅: iframe 내용 확인
        try:
            frame_html = await frame.locator('body').inner_html()
            logger.info(f"   🔍 iframe HTML 길이: {len(frame_html)}")
            logger.info(f"   🔍 iframe HTML 샘플:\n{frame_html[:2000]}")
        except Exception as e:
            logger.error(f"   ❌ iframe HTML 가져오기 실패: {e}")

        all_links = []
        seen = set()
        page_num = 1
        base_url = get_base_url(page.url)

        while True: 
            logger.info(f"   📄 페이지 {page_num} 처리 중...")

            # frame에서 모든 링크 가져오기
            links = await frame.locator('a[href]').all()
            logger.info(f"   🔍 찾은 a[href] 개수: {len(links)}")

            # 디버깅: 처음 5개 링크의 href 출력
            for i, link in enumerate(links[:5]):
                try:
                    href = await link.get_attribute('href')
                    text = await link.inner_text()
                    logger.info(f"   🔗 링크 {i}: href={href}, text={text[:50] if text else 'None'}")
                except:
                    pass

            count_before = len(all_links)

            for link in links:
                try:
                    href = await link.get_attribute('href')

                    # href="#"인 경우에만 onclick에서 eventListView 파싱 (범용)
                    if not href or href == '#':
                        onclick = await link.get_attribute('onclick')
                        if onclick and 'eventListView' in onclick:
                            # eventListView(1622,'01','266') 패턴 추출
                            match = re.search(r"eventListView\((\d+),\s*'(\d+)',\s*'(\d+)'\)", onclick)
                            if match:
                                pln_disp_no = match.group(1)
                                pln_disp_type_cd = match.group(2)
                                evt_no = match.group(3)
                                href = f"{base_url}/plan/planDispView.do?plnDispNo={pln_disp_no}&plnDispTypeCd={pln_disp_type_cd}&evtNo={evt_no}"

                    if not href or href == '#':
                        continue

                    href = href.strip()
                    if href.startswith('/'):
                        href = f"{base_url}{href}"

                    if not href.startswith('http') or href in seen:
                        continue

                    if not is_kt_domain(href):
                        continue

                    # 텍스트 추출 - EVENT_LABEL
                    text = ''

                    try:
                        onclick = await link.get_attribute('onclick')
                        if onclick and 'EVENT_LABEL' in onclick:
                            match = re.search(r"EVENT_LABEL\s*:\s*'([^']+)'", onclick)
                            if match:
                                label = match.group(1)
                                # 마지막 _ 이전까지만 추출
                                text = label.rsplit('_', 1)[0]
                    except:
                        pass

                    if text and len(text) >= 2 and not is_excluded_name(text):
                        seen.add(href)
                        all_links.append({'name': text, 'url': href})
                except:
                    continue

            added = len(all_links) - count_before
            logger.info(f"   → 페이지 {page_num}: +{added}개 (총 {len(all_links)}개)")

            # 다음 페이지 버튼 찾기 (같은 pageno가 여러 개 있을 수 있으므로 .first 사용)
            next_btn = frame.locator(f'a[pageno="{page_num + 1}"]:not(.page)').first
            next_count = await next_btn.count()

            if next_count == 0:
                # .page 클래스 없는 버튼이 없으면 아무 버튼이나 시도
                next_btn = frame.locator(f'a[pageno="{page_num + 1}"]').first
                next_count = await next_btn.count()

            if next_count == 0:
                logger.info(f"   → 더 이상 페이지 없음 (pageno={page_num + 1} 없음)")
                break

            # 다음 페이지 클릭
            await next_btn.click()
            await page.wait_for_timeout(2000)
            page_num += 1

        logger.info(f"✅ iframe[title=\"{iframe_title}\"]에서 총 {len(all_links)}개 추출 완료")

        # 범용 필터링: 가장 많이 나온 URL 패턴만 유지
        if all_links:
            all_links = filter_by_dominant_pattern(all_links)
            logger.info(f"🔍 필터링 후: {len(all_links)}개")

        return all_links

    except Exception as e:
        logger.error(f"❌ iframe[title=\"{iframe_title}\"] 처리 실패: {e}")
        return []


def filter_by_dominant_pattern(links):
    """가장 많이 나온 URL 패턴의 링크만 유지 (범용 필터링)"""
    from collections import Counter

    # URL에서 파일명 패턴 추출 (예: olhsPlan.do, planDispView.do)
    def get_url_pattern(url):
        # .do 패턴 추출
        match = re.search(r'/([a-zA-Z]+\.do)', url)
        if match:
            return match.group(1)
        return 'other'

    # 패턴별 카운트
    patterns = [get_url_pattern(link['url']) for link in links]
    pattern_counts = Counter(patterns)

    logger.info(f"   📊 URL 패턴 분포: {dict(pattern_counts)}")

    # 가장 많은 패턴 찾기
    if not pattern_counts:
        return links

    dominant_pattern = pattern_counts.most_common(1)[0][0]
    logger.info(f"   📊 주요 패턴: {dominant_pattern}")

    # 해당 패턴만 유지
    filtered = [link for link in links if get_url_pattern(link['url']) == dominant_pattern]

    return filtered


async def extract_board_links(page):
    """게시판 목록 추출"""
    base_url = get_base_url(page.url)
    all_links, seen = [], set()
    page_num = 1
    zero_count = 0  # 연속으로 새 링크 없는 페이지 카운터

    while True:  # 자연 종료 조건으로 탈출
        logger.info(f"📄 목록 페이지 {page_num} 처리 중...")

        html = await page.content()
        soup = BeautifulSoup(html, 'html.parser')
        count_before = len(all_links)

        # display: none 요소 제거 (숨겨진 메뉴 등)
        for el in soup.find_all(style=lambda s: s and 'display' in s.lower() and 'none' in s.lower()):
            el.decompose()

        # 1. data-apcturl 속성이 있는 a 태그 (이벤트 목록)
        for a in soup.find_all('a', attrs={'data-apcturl': True}):
            url = a.get('data-apcturl', '').strip()
            if not url or url in seen:
                continue

            # 제목 추출: .title > img alt > a 텍스트
            title_el = a.select_one('.title')
            text = title_el.get_text(strip=True) if title_el else ''
            if not text:
                img = a.find('img')
                if img:
                    text = img.get('alt', '').strip()
            if not text:
                text = a.get_text(strip=True)

            if text and len(text) >= 2:
                seen.add(url)
                all_links.append({'name': text, 'url': url})

        # 2. onclick="goDetPage(숫자)" 패턴 (페이지네이션 후에도 계속 추출)
        for a in soup.find_all('a', onclick=True):
            onclick = a.get('onclick', '')
            match = re.search(r'goDetPage\((\d+)\)', onclick)
            if not match:
                continue

            seq = match.group(1)
            url = f"{base_url}/blog/detail.do?seq={seq}"
            if url in seen:
                continue

            # 제목 추출: img alt > a 텍스트
            img = a.find('img')
            text = img.get('alt', '').strip() if img else ''
            if not text:
                text = a.get_text(strip=True)

            if text and len(text) >= 2:
                seen.add(url)
                all_links.append({'name': text, 'url': url})

        # 3. 일반 링크 추출 (게시판) - 위 패턴에서 추출된 게 없는 경우
        if count_before == len(all_links):
            for sel in DECOMPOSE_SELECTORS:
                for el in soup.select(sel):
                    el.decompose()
            for tag in soup(['script', 'style', 'noscript']):
                tag.decompose()

            for link in extract_links_from_soup(soup, base_url=base_url, min_count=1):
                if link['url'] not in seen:
                    seen.add(link['url'])
                    all_links.append(link)

        added = len(all_links) - count_before
        logger.info(f"   → 페이지 {page_num}: +{added}개 (총 {len(all_links)}개)")

        # 연속으로 새 링크 없는 페이지 체크 (무한루프 방지)
        if added == 0:
            zero_count += 1
            if zero_count >= 2:  # 연속 2번 이상 새 링크 없으면 종료
                logger.info("   → 연속 2페이지 새 링크 없음, 종료")
                break
        else:
            zero_count = 0

        # 첫 페이지에서 아무것도 없으면 종료
        if page_num == 1 and len(all_links) < 3:
            break

        # 다음 페이지 버튼 찾기
        next_page = page_num + 1
        next_btn = page.locator(f'a[pageno="{next_page}"]').first
        next_count = await next_btn.count()

        if next_count == 0:
            # 정확한 페이지 번호 매칭 (:text-is로 정확히 일치)
            next_btn = page.locator(f'.paging a:text-is("{next_page}"), .pagination a:text-is("{next_page}")').first
            next_count = await next_btn.count()

        if next_count == 0:
            # ">" 또는 "다음" 화살표 버튼 시도 (다음 페이지 그룹 표시)
            arrow_btn = page.locator('a.dir.next, a.next, a.btn-next, a.next-page').first
            arrow_count = await arrow_btn.count()
            if arrow_count > 0:
                logger.info(f"   ➡️ arrow 버튼 클릭 (페이지 {next_page} 버튼 표시)")
                await arrow_btn.click()
                await page.wait_for_timeout(2000)
                # arrow 클릭 후 다음 페이지 버튼 찾아서 클릭
                next_btn = page.locator(f'a[pageno="{next_page}"]').first
                next_count = await next_btn.count()
                if next_count == 0:
                    next_btn = page.locator(f'.paging a:text-is("{next_page}"), .pagination a:text-is("{next_page}")').first
                    next_count = await next_btn.count()
                if next_count > 0:
                    logger.info(f"   📄 페이지 {next_page} 버튼 클릭")
                    await next_btn.click()
                    await page.wait_for_timeout(2000)
                # arrow 클릭 후에는 항상 continue (버튼 클릭 여부와 관계없이 - arrow 자체가 페이지 이동할 수도 있음)
                page_num += 1
                continue

        if next_count == 0:
            # "더보기" 버튼 시도 (visible만, a 태그 제외)
            more_btn = page.locator('#btn_more:visible, .fjbBtnMore:visible, button:has-text("더보기"):visible, .btn-more:visible, .more-btn:visible, .load-more:visible').first
            more_count = await more_btn.count()
            if more_count > 0:
                # 마지막 페이지인지 확인 (예: "3/3")
                more_text = await more_btn.inner_text()
                match = re.search(r'(\d+)/(\d+)', more_text)
                if match and match.group(1) == match.group(2):
                    break  # 마지막 페이지면 종료

                await more_btn.click()
                await page.wait_for_timeout(2000)
                page_num += 1
                continue

        if next_count == 0:
            # 다음 페이지도 없고 새 링크도 없으면 종료
            if added == 0:
                logger.info("   → 다음 페이지 없음, 종료")
            break

        await next_btn.click()
        await page.wait_for_timeout(2000)
        page_num += 1

    if all_links:
        logger.info(f"📋 목록 총 {len(all_links)}개 링크 추출")
    return all_links if len(all_links) >= 3 else []


async def crawl_page(url):
    """
    크롤링 우선순위:
    1. 스킵 체크
    2. 상품
    3. 게시판
    4. 탭
    5. iframe + 페이지네이션
    6. 없으면 빈 배열
    """
    if should_skip(url):
        logger.info(f"⏭️ 스킵: {url}")
        return {'success': True, 'links': [], 'skipped': True}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            page = await browser.new_page()
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(5000)

            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await page.wait_for_timeout(2000)

            # 0. 페이지 내 모든 iframe을 동적으로 찾아서 처리
            all_iframes = await page.locator('iframe[title]').all()
            logger.info(f"🔍 title 속성이 있는 iframe {len(all_iframes)}개 발견")

            for iframe_el in all_iframes:
                title = await iframe_el.get_attribute('title')
                if title:
                    links = await extract_from_titled_iframe(page, title)
                    if links:
                        await browser.close()
                        return {'success': True, 'links': links}

            # 1. 상품
            products = await extract_products(page)
            if products:
                await browser.close()
                return {'success': True, 'links': products}

            # 2. 게시판
            links = await extract_board_links(page)
            if links:
                await browser.close()
                return {'success': True, 'links': links}

            # 3. 탭
            tabs = await extract_tabs(page)
            if tabs:
                await browser.close()
                return {'success': True, 'links': tabs}

            # 4. iframe + 페이지네이션
            iframe_links = await try_extract_from_iframes(page)
            if iframe_links:
                await browser.close()
                return {'success': True, 'links': iframe_links}

            # 5. 없으면 빈 배열
            await browser.close()
            logger.info("⚠️ 추출 대상 없음")
            return {'success': True, 'links': []}

    except Exception as e:
        logger.error(f"❌ 크롤링 실패: {e}")
        return {'success': False, 'error': str(e), 'links': []}


async def main():
    print("=" * 60)
    print("🔍 KT URL 크롤러 - 단일 URL 테스트")
    print("=" * 60)

    # URL 입력받기
    url = input("\n크롤링할 URL을 입력하세요: ").strip()

    if not url:
        print("❌ URL이 입력되지 않았습니다.")
        return

    # URL이 http로 시작하지 않으면 자동으로 추가
    if not url.startswith('http'):
        url = f"https://{url}"

    print(f"\n🚀 크롤링 시작: {url}")
    print("-" * 60)

    # 크롤링 실행
    result = await crawl_page(url)

    # 결과 출력
    print("\n" + "=" * 60)
    print("📊 결과")
    print("=" * 60)

    if result['success']:
        if result.get('skipped'):
            print("⏭️  스킵된 URL입니다.")
        else:
            links = result.get('links', [])
            print(f"✅ 성공: {len(links)}개 링크 발견\n")

            if links:
                print("📋 추출된 링크 목록:")
                print("-" * 60)
                for i, link in enumerate(links, 1):
                    print(f"\n{i}. {link['name']}")
                    print(f"   URL: {link['url']}")

                # JSON 파일로 저장 여부 확인
                print("\n" + "=" * 60)
                save = input("\n💾 결과를 JSON 파일로 저장하시겠습니까? (y/n): ").strip().lower()

                if save == 'y':
                    filename = 'crawl_result.json'
                    with open(filename, 'w', encoding='utf-8') as f:
                        json.dump({
                            'url': url,
                            'success': True,
                            'links': links
                        }, f, ensure_ascii=False, indent=2)
                    print(f"✅ 저장 완료: {filename}")
            else:
                print("⚠️  추출된 링크가 없습니다.")
    else:
        print(f"❌ 실패: {result.get('error', '알 수 없는 오류')}")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  사용자에 의해 중단되었습니다.")
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
