from bs4 import BeautifulSoup
import json

def extract_menu(li):
    # li 바로 아래 a 태그에서 메뉴명 추출
    tag_a = li.find("a", recursive=False)
    if not tag_a:
        return None

    name = tag_a.get_text(strip=True)
    if not name:
        return None

    # 하위 메뉴 ul 찾기 (depth 클래스 기준)
    sub_ul = li.find(
        "ul",
        class_=lambda x: x and "depth" in x
    )

    if not sub_ul:
        return name

    children = []
    for sub_li in sub_ul.find_all("li", recursive=False):
        child = extract_menu(sub_li)
        if child:
            children.append(child)

    if not children:
        return name

    return {name: children}

def count_menu(menu, depth_counts, depth=1):
    if depth not in depth_counts:
        depth_counts[depth] = 0

    if isinstance(menu, dict):
        for v in menu.values():
            depth_counts[depth] += 1
            for child in v:
                count_menu(child, depth_counts, depth+1)
    else:
        depth_counts[depth] += 1

def main():
    with open("kt_menu_full_dom.html", "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    gnb = soup.find("div", id="cfmClGnb")
    if not gnb:
        raise Exception("cfmClGnb 없음")

    top_ul = gnb.find("ul")
    if not top_ul:
        raise Exception("1뎁스 ul 없음")

    menu_tree = {}
    for li in top_ul.find_all("li", recursive=False):
        result = extract_menu(li)
        if isinstance(result, dict):
            menu_tree.update(result)
        elif result:
            menu_tree[result] = []

    # JSON 저장
    with open("kt_menu_final.json", "w", encoding="utf-8") as f:
        json.dump(menu_tree, f, ensure_ascii=False, indent=2)

    # 뎁스별 메뉴 개수 계산
    depth_counts = {}
    for li in top_ul.find_all("li", recursive=False):
        result = extract_menu(li)
        if result:
            count_menu(result, depth_counts)

    total_count = sum(depth_counts.values())

    for d in sorted(depth_counts):
        print(f"{d} depth 메뉴 개수: {depth_counts[d]}")

    print(f"총 메뉴 개수: {total_count}")
    print("KT 메뉴 파싱 완료")

if __name__ == "__main__":
    main()
