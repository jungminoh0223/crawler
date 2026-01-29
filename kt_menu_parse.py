from bs4 import BeautifulSoup
from urllib.parse import urljoin
import json
import re

PAGE_URL = "https://www.kt.com"

def normalize_url(href, page_url):
    if not href:
        return None

    href = href.strip()

    if href.startswith("javascript:"):
        m = re.search(r"'(https?://[^']+)'", href)
        return m.group(1) if m else None

    if href.startswith("http"):
        return href

    return urljoin(page_url, href)

def extract_menu(li, page_url):
    tag_a = li.find("a", recursive=False)
    if not tag_a:
        return None

    name = tag_a.get_text(strip=True)
    href = tag_a.get("href")

    if not name:
        return None

    url = normalize_url(href, page_url)

    sub_ul = li.find(
        "ul",
        class_=lambda x: x and "depth" in x
    )

    children = []
    if sub_ul:
        for sub_li in sub_ul.find_all("li", recursive=False):
            child = extract_menu(sub_li, page_url)
            if child:
                children.append(child)

    return {
        "name": name,
        "url": url,
        "children": children
    }

def count_menu(menu, depth_counts, depth=1):
    depth_counts[depth] = depth_counts.get(depth, 0) + 1
    for child in menu.get("children", []):
        count_menu(child, depth_counts, depth + 1)

def main():
    with open("kt_menu_full_dom.html", "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    gnb = soup.find("div", id="cfmClGnb")
    if not gnb:
        raise Exception("cfmClGnb 없음")

    top_ul = gnb.find("ul")
    if not top_ul:
        raise Exception("1뎁스 ul 없음")

    menu_tree = []

    for li in top_ul.find_all("li", recursive=False):
        menu = extract_menu(li, PAGE_URL)
        if menu:
            menu_tree.append(menu)

    with open("kt_menu_final_with_url.json", "w", encoding="utf-8") as f:
        json.dump(menu_tree, f, ensure_ascii=False, indent=2)

    depth_counts = {}
    for menu in menu_tree:
        count_menu(menu, depth_counts)

    print("뎁스별 메뉴 개수:")
    for d in sorted(depth_counts):
        print(f"{d} depth: {depth_counts[d]}")

    print(f"총 메뉴 개수: {sum(depth_counts.values())}")

if __name__ == "__main__":
    main()
