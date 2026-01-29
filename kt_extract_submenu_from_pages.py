import json
import asyncio
import logging
import re
from typing import List, Dict
from pathlib import Path
from datetime import datetime
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

def should_skip(url):
    return any(p in url for p in SKIP_CRAWL_PATTERNS)

def is_excluded_name(name):
    return any(re.match(p, name.strip(), re.IGNORECASE) for p in EXCLUDE_NAME_PATTERNS)

def is_excluded_url(url):
    return any(p in url for p in EXCLUDE_URL_PATTERNS)

def extract_links_from_soup(soup, min_count=1):
    """링크 추출"""
    links, seen = [], set()
    
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith('/'):
            href = f"https://shop.kt.com{href}"
        
        if not href.startswith('https://shop.kt.com'):
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
        
        # 2. img alt
        if not text:
            img = a.find('img')
            if img:
                text = img.get('alt', '').strip()
        
        # 3. 링크 텍스트
        if not text:
            text = a.get_text(strip=True)
        
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
                href = f"https://shop.kt.com{href}"
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
    
    # 2차: projectList만 있는 iframe
    for i, frame in enumerate(page.frames):
        try:
            html = await frame.content()
            soup = BeautifulSoup(html, 'html.parser')
            
            project_links = soup.select('ul.projectList li a[href]')
            if len(project_links) >= 3:
                links = extract_links_from_soup(soup, min_count=3)
                if links:
                    logger.info(f"🔍 iframe {i}에서 목록 발견: {len(links)}개")
                    return links
        except:
            continue
    
    return []


async def extract_tabs(page):
    """탭 링크 추출"""
    html = await page.content()
    soup = BeautifulSoup(html, 'html.parser')
    
    tabs, seen = [], set()
    for link in soup.find_all('a', href=True):
        img = link.find('img')
        if not img or 'tab' not in img.get('src', '').lower():
            continue
        
        href = link['href']
        if href.startswith('/'):
            href = f"https://shop.kt.com{href}"
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
    """상품 추출"""
    html = await page.content()
    soup = BeautifulSoup(html, 'html.parser')
    
    products = []
    for inp in soup.find_all('input', {'name': 'prodAttr'}):
        name, no = inp.get('prodnm', ''), inp.get('prodno', '')
        if name and no:
            products.append({
                'name': name,
                'url': f"https://shop.kt.com/display/olhsGoodsDtl.do?goodsCode={no}"
            })
    
    if products:
        logger.info(f"📦 상품 {len(products)}개 발견")
    return products


async def extract_board_links(page):
    """게시판 링크 추출"""
    html = await page.content()
    soup = BeautifulSoup(html, 'html.parser')
    
    for sel in ['#cfmClHeader', '#cfmClFooter', '#cfmClSkip', '.header', '.footer',
                '.navigation', '.sidebar', '.banner', '.popup', '.overlay', '.sns-area', '.location']:
        for el in soup.select(sel):
            el.decompose()
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    
    links = extract_links_from_soup(soup, min_count=3)
    if links:
        logger.info(f"📋 게시판 {len(links)}개 링크 추출")
    return links


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
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(5000)
            
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await page.wait_for_timeout(2000)
            
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
        if MENU_TREE:
            with open('kt_menu_crawled_interrupted.json', 'w', encoding='utf-8') as f:
                json.dump(MENU_TREE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f'❌ 오류: {e}', exc_info=True)