import json
import asyncio
import logging
import re
from copy import copy
from typing import List, Dict
from pathlib import Path
from datetime import datetime
from collections import Counter
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 스킵할 URL 패턴
SKIP_CRAWL_PATTERNS = [
    'olhsPlan.do',          # 브랜드관 (기획전 상세)
    'phoneView.do',         # 중고폰 보상
    'whyKTSIM.do',          # 인기 추천 요금제
    '/direct/',             # 다이렉트 페이지
    'yogoEvent.do',         # 요고 이벤트
    '/wire/',               # 유선(인터넷/TV) 전체
    'soho/marketing.do',    # 소상공인 마케팅 상세
    'soho/productDetail.do',# 소상공인 상품 상세
    '/benefit/',            # 혜택 랜딩 페이지
    'hotdeal.kt.com',       # 핫딜 쇼핑몰
    '/deal/',               # 5시 핫픽, 출석체크 등 이벤트
    '/rental/',             # 가전구독
    '/recommend/',          # 나의 추천코드 등 개인 페이지
    'offerwall.do',         # 캐시리워드
    'supportAmtList.do',    # 휴대폰 지원금 안내
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
    '/login',
    '/member/',
    'javascript:',
    '#',
]

# 텍스트 추출 시 제외할 셀렉터 (링크 내부의 불필요한 요소)
EXCLUDE_TEXT_SELECTORS = [
    '.date', '.txt', '.desc', '.category', '.tag', '.badge', '.icon',
    '.num', '.count', '.view', '.hit',
    'span.sub', 'em.sub', '.sub-txt',
    '.blind', '.sr-only', '.hidden',
    '.btn', '.more',
]

def should_skip(url):
    return any(p in url for p in SKIP_CRAWL_PATTERNS)

def is_kt_domain(url):
    """kt.com 도메인인지 체크 (모든 서브도메인 허용)"""
    return url.startswith('http') and '.kt.com' in url

def get_base_url(url):
    """URL에서 도메인 부분만 추출"""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"

def is_excluded_name(name):
    return any(re.match(p, name.strip(), re.IGNORECASE) for p in EXCLUDE_NAME_PATTERNS)

def is_excluded_url(url):
    return any(p in url for p in EXCLUDE_URL_PATTERNS)

def extract_links_from_soup(soup, base_url, min_count=1):
    """링크 추출"""
    links, seen = [], set()

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith('/'):
            href = f"{base_url}{href}"

        if not is_kt_domain(href):
            continue
        if is_excluded_url(href) or href in seen:
            continue
        
        text = ''
        
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
        
        # 유효하지 않은 텍스트 스킵 (빈값, 2자 미만, 제외 패턴)
        if not text or len(text) < 2 or is_excluded_name(text):
            continue

        # 너무 긴 텍스트는 150자로 자르기
        if len(text) > 150:
            text = text[:150] + '...'
        
        seen.add(href)
        links.append({'name': text, 'url': href})
    
    return links if len(links) >= min_count else []


def filter_by_dominant_pattern(links):
    """가장 많이 나온 URL 패턴의 링크만 유지"""
    def get_url_pattern(url):
        match = re.search(r'/([a-zA-Z]+\.do)', url)
        if match:
            return match.group(1)
        return 'other'

    patterns = [get_url_pattern(link['url']) for link in links]
    pattern_counts = Counter(patterns)

    logger.info(f"   📊 URL 패턴 분포: {dict(pattern_counts)}")

    if not pattern_counts:
        return links

    dominant_pattern = pattern_counts.most_common(1)[0][0]
    logger.info(f"   📊 주요 패턴: {dominant_pattern}")

    filtered = [link for link in links if get_url_pattern(link['url']) == dominant_pattern]
    return filtered


async def extract_with_pagination(frame):
    """페이지네이션이 있는 프레임에서 모든 페이지 추출"""
    all_links, seen = [], set()
    page_num = 1
    base_url = get_base_url(frame.url)

    while page_num <= 20:
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

    while page_num <= 20:
        logger.info(f"📄 {page_num}페이지 수집...")

        html = await frame.content()
        soup = BeautifulSoup(html, 'html.parser')

        # 헤더/푸터/탭 제거
        for sel in ['#cfmClHeader', '#cfmClFooter', '#cfmClSkip', '.header', '.footer',
                    '.navigation', '.sidebar', '.banner', '.popup', '.overlay', '.sns-area', '.location',
                    '.gnb', '.lnb', '.snb', '.util', '.quick', '.ui-tab-lst', '.ui-tab-top-lst']:
            for el in soup.select(sel):
                el.decompose()

        count_before = len(all_links)

        for a in soup.find_all('a', href=True):
            href = a.get('href', '').strip()

            if href.startswith('/'):
                href = f"{base_url}{href}"

            if not href.startswith('http') or is_excluded_url(href) or href in seen:
                continue

            if not is_kt_domain(href):
                continue

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


async def extract_from_titled_iframe(page, iframe_title):
    """특정 title의 iframe에서 모든 링크 추출 (페이지네이션 포함)"""
    logger.info(f"🔍 iframe[title=\"{iframe_title}\"] 찾는 중...")

    try:
        iframe_locator = page.locator(f'iframe[title="{iframe_title}"]')
        count = await iframe_locator.count()
        if count == 0:
            logger.info(f"   ❌ iframe[title=\"{iframe_title}\"] 없음")
            return []

        logger.info(f"   ✅ iframe[title=\"{iframe_title}\"] 발견!")
        frame = iframe_locator.content_frame
        await page.wait_for_timeout(2000)

        all_links = []
        seen = set()
        page_num = 1
        base_url = get_base_url(page.url)

        while page_num <= 20:
            logger.info(f"   📄 페이지 {page_num} 처리 중...")

            links = await frame.locator('a[href]').all()
            count_before = len(all_links)

            for link in links:
                try:
                    href = await link.get_attribute('href')

                    # href="#"인 경우 onclick에서 eventListView 파싱
                    if not href or href == '#':
                        onclick = await link.get_attribute('onclick')
                        if onclick and 'eventListView' in onclick:
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

                    text = ''
                    try:
                        text = await link.inner_text()
                        text = text.strip() if text else ''
                    except:
                        pass

                    if not text or len(text) < 2:
                        try:
                            img = link.locator('img').first
                            alt = await img.get_attribute('alt')
                            if alt:
                                text = alt.strip()
                        except:
                            pass

                    if text and len(text) >= 2 and not is_excluded_name(text):
                        seen.add(href)
                        all_links.append({'name': text, 'url': href})
                except:
                    continue

            added = len(all_links) - count_before
            logger.info(f"   → 페이지 {page_num}: +{added}개 (총 {len(all_links)}개)")

            next_btn = frame.locator(f'a[pageno="{page_num + 1}"]:not(.page)').first
            next_count = await next_btn.count()
            if next_count == 0:
                next_btn = frame.locator(f'a[pageno="{page_num + 1}"]').first
                next_count = await next_btn.count()

            if next_count == 0:
                break

            await next_btn.click()
            await page.wait_for_timeout(2000)
            page_num += 1

        logger.info(f"✅ iframe[title=\"{iframe_title}\"]에서 총 {len(all_links)}개 추출 완료")

        if all_links:
            all_links = filter_by_dominant_pattern(all_links)
            logger.info(f"🔍 필터링 후: {len(all_links)}개")

        return all_links

    except Exception as e:
        logger.error(f"❌ iframe[title=\"{iframe_title}\"] 처리 실패: {e}")
        return []


async def try_extract_from_iframes(page):
    """iframe에서 목록 추출 (HTML 내용 기반)"""

    # 1차: projectList + 페이지네이션 있는 iframe
    for i, frame in enumerate(page.frames):
        try:
            html = await frame.content()
            soup = BeautifulSoup(html, 'html.parser')

            project_links = soup.select('ul.projectList li a[href]')
            has_pagination = soup.select_one('a[pageno]')

            if len(project_links) >= 3 and has_pagination:
                logger.info(f"🔍 iframe {i}에서 목록+페이지네이션 발견")
                return await extract_with_pagination(frame)
        except:
            continue

    # 2차: pageno가 있는 프레임 우선 처리 (게시판 목록)
    for i, frame in enumerate(page.frames):
        try:
            html = await frame.content()
            soup = BeautifulSoup(html, 'html.parser')

            has_pageno = soup.select_one('a[pageno]')
            if has_pageno:
                all_li_with_a = soup.select('li a[href]')
                if len(all_li_with_a) >= 3:
                    logger.info(f"🔍 iframe {i}에서 pageno 목록 발견, 페이지네이션 처리 시작...")
                    links = await extract_with_pagination_generic(frame)
                    if links:
                        logger.info(f"✅ iframe {i}에서 {len(links)}개 추출 완료")
                        return links
        except:
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
                links = extract_links_from_soup(soup, base_url, min_count=3)
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

    while page_num <= 20:
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

        # 다음 페이지 버튼 찾기
        next_btn = page.locator(f'a[pageno="{page_num + 1}"]').first
        next_count = await next_btn.count()

        if next_count == 0:
            break

        await next_btn.click()
        await page.wait_for_timeout(2000)
        page_num += 1

    if products:
        logger.info(f"📦 상품 총 {len(products)}개 발견")
    return products


async def extract_board_links(page):
    """게시판 링크 추출"""
    html = await page.content()
    soup = BeautifulSoup(html, 'html.parser')
    base_url = get_base_url(page.url)

    for sel in ['#cfmClHeader', '#cfmClFooter', '#cfmClSkip', '.header', '.footer',
                '.navigation', '.sidebar', '.banner', '.popup', '.overlay', '.sns-area', '.location']:
        for el in soup.select(sel):
            el.decompose()
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()

    links = extract_links_from_soup(soup, base_url, min_count=3)
    if links:
        logger.info(f"📋 게시판 {len(links)}개 링크 추출")
    return links


async def crawl_page(url):
    """
    크롤링 우선순위:
    1. 스킵 체크
    2. iframe[title] 우선 처리
    3. 상품
    4. 게시판
    5. 탭
    6. iframe + 페이지네이션
    7. 없으면 빈 배열
    """
    if should_skip(url):
        logger.info(f"⏭️ 스킵: {url}")
        return {'success': True, 'links': [], 'skipped': True}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(5000)

            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await page.wait_for_timeout(2000)

            # 0. iframe[title] 우선 처리
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
            if any(kw in url.lower() for kw in ['webzine', 'board', 'notice', 'news']):
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


MENU_TREE = []


def save_progress(path):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(MENU_TREE, f, ensure_ascii=False, indent=2)


async def process_node(menu, output_path, hierarchy, delay=1.0):
    name = menu.get('name', '')
    url = menu.get('url', '')
    children = menu.get('children', [])
    current = hierarchy + [name]
    
    if isinstance(children, list) and not children and url.startswith('http'):
        logger.info(f"🔍 [{len(current)}depth] {' > '.join(current)}")
        logger.info(f"   URL: {url}")
        
        result = await crawl_page(url)
        
        if result['success']:
            if result.get('skipped'):
                logger.info("⏭️ 스킵됨")
                menu['children'] = []
            else:
                links = result.get('links', [])
                menu['children'] = [{'name': l['name'], 'url': l['url']} for l in links]
                if links:
                    logger.info(f"✅ 완료 ({len(links)}개)")
                else:
                    logger.info("⚠️ 추출 결과 없음")
        else:
            logger.error(f"❌ 실패: {result.get('error')}")
            menu['children'] = []
        
        save_progress(output_path)
        if not result.get('skipped'):
            await asyncio.sleep(delay)
    
    elif isinstance(children, list) and children:
        for child in children:
            await process_node(child, output_path, current, delay)


def count_nodes(tree):
    stats = {'total': 0, 'empty': 0, 'skip': 0, 'crawl': 0}
    
    def count(nodes):
        for n in nodes:
            stats['total'] += 1
            children = n.get('children', [])
            url = n.get('url', '')
            
            if isinstance(children, list) and not children and url.startswith('http'):
                stats['empty'] += 1
                if should_skip(url):
                    stats['skip'] += 1
                else:
                    stats['crawl'] += 1
            elif isinstance(children, list) and children:
                count(children)
    
    count(tree)
    return stats


async def main():
    global MENU_TREE
    
    INPUT = 'kt_menu_final_with_url.json'
    OUTPUT = 'kt_menu_crawled.json'
    DELAY = 1.0
    
    print('=' * 60)
    print('🚀 KT 메뉴 크롤러')
    print('=' * 60)
    
    with open(INPUT, 'r', encoding='utf-8') as f:
        MENU_TREE = json.load(f)
    
    stats = count_nodes(MENU_TREE)
    logger.info(f"📊 전체: {stats['total']}개, 크롤링: {stats['crawl']}개, 스킵: {stats['skip']}개")
    
    logger.info('\n🚀 크롤링 시작...\n')
    start = datetime.now()
    output_path = Path(OUTPUT)
    
    for i, menu in enumerate(MENU_TREE, 1):
        logger.info(f"\n{'='*60}")
        logger.info(f"[{i}/{len(MENU_TREE)}] {menu.get('name', '')}")
        logger.info('='*60)
        await process_node(menu, output_path, [], DELAY)
    
    logger.info(f"\n🎉 완료! 소요시간: {datetime.now() - start}")
    logger.info(f"💾 저장: {output_path.absolute()}")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('\n⚠️ 중단됨')
    except Exception as e:
        logger.error(f'❌ 오류: {e}', exc_info=True)