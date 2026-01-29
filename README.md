# KT 메뉴 크롤러

KT 홈페이지의 전체 메뉴 구조를 추출하고, 각 메뉴의 하위 링크까지 수집하는 크롤러입니다.

---

## 전체 파이프라인

```
[1단계]                    [2단계]                      [3단계]
kt_menu_parse.py  →      kt_menu_crawler.py          →  kt_extract_submenu_from_pages.py
       ↓                          ↓                              ↓
kt_menu_full_dom.html      kt_menu_final_with_url.json    kt_menu_crawled.json
(메뉴 DOM 저장)            (메뉴 트리 파싱)               (하위 링크 채움)
```

---

## 파일 설명

| 파일명 | 역할 |
|--------|------|
| `kt_menu_parse.py` | KT 홈페이지 접속 → 메뉴 hover/click → DOM 저장 |
| `kt_menu_crawler.py` | DOM에서 메뉴 트리 구조 파싱 → JSON 변환 |
| `kt_extract_submenu_from_pages.py` | 각 메뉴 URL 방문 → 하위 링크 수집 |

| 데이터 파일 | 설명 |
|-------------|------|
| `kt_menu_full_dom.html` | 메뉴가 펼쳐진 상태의 HTML |
| `kt_menu_final_with_url.json` | 메뉴 트리 (children 비어있음) |
| `kt_menu_crawled.json` | 메뉴 트리 (children 채워짐) |

---

## 실행 방법

### 0. 필요 패키지 설치

```bash
pip install playwright beautifulsoup4
playwright install chromium
```

### 1단계: DOM 추출

```bash
python kt_menu_parse.py
```

- KT 홈페이지 접속
- 모든 링크에 hover, 버튼 click하여 메뉴 펼침
- `kt_menu_full_dom.html` 저장

### 2단계: 메뉴 파싱

```bash
python kt_menu_crawler.py
```

- HTML에서 `#cfmClGnb` (GNB 메뉴) 찾기
- 재귀적으로 메뉴 트리 구조 추출
- `kt_menu_final_with_url.json` 저장

### 3단계: 하위 링크 수집

```bash
python kt_extract_submenu_from_pages.py
```

- `children`이 빈 배열인 메뉴만 크롤링
- 각 페이지에서 하위 링크 추출
- `kt_menu_crawled.json` 저장

### 중단하려면

`Ctrl+C` - 현재까지 진행 상황이 자동 저장됩니다.

---

## 작동 원리

### 1단계: DOM 추출 (`kt_menu_dom_extract.py`)

```python
# 1. KT 메인 페이지 접속
# 2. 모든 visible <a> 태그에 hover → 서브메뉴 펼침
# 3. 모든 visible <button> 클릭 → 숨겨진 메뉴 펼침
# 4. 펼쳐진 상태의 전체 DOM 저장
```

### 2단계: 메뉴 파싱 (`kt_menu_parse.py`)

```python
# 1. #cfmClGnb 영역에서 1depth ul 찾기
# 2. 각 li 태그를 재귀적으로 순회
# 3. name, url, children 구조로 변환
```

**출력 예시:**
```
뎁스별 메뉴 개수:
1 depth: 8
2 depth: 45
3 depth: 120
총 메뉴 개수: 173
```

### 3단계: 하위 링크 수집 (`kt_extract_submenu_from_pages.py`)

**처리 대상:** `children`이 빈 배열이고 `url`이 있는 노드

```json
{
  "name": "5G 요금제",
  "url": "https://shop.kt.com/...",
  "children": []  ← 이런 노드만 처리
}
```

**추출 우선순위:**

1. **상품 목록** - `<input name="prodAttr">` 태그
2. **게시판 링크** - URL에 webzine, board, notice 포함 시
3. **탭 메뉴** - 이미지에 'tab' 포함된 링크
4. **iframe 목록** - 페이지네이션 있는 목록

**결과 예시:**

```json
{
  "name": "5G 요금제",
  "url": "https://shop.kt.com/mobile/plan.do",
  "children": [
    {"name": "5G 심플", "url": "https://shop.kt.com/..."},
    {"name": "5G 슬림", "url": "https://shop.kt.com/..."},
    {"name": "5G 다이렉트", "url": "https://shop.kt.com/..."}
  ]
}
```

---

## 스킵되는 페이지

아래 URL 패턴은 자동으로 건너뜁니다:

| 패턴 | 이유 |
|------|------|
| `/direct/` | 다이렉트 페이지 |
| `/wire/` | 유선(인터넷/TV) |
| `/benefit/` | 혜택 랜딩 |
| `/deal/` | 이벤트 페이지 |
| `/rental/` | 가전구독 |
| `hotdeal.kt.com` | 핫딜 쇼핑몰 |

전체 목록은 코드의 `kt_menu_crawled.json` 참고.

> ⚠️ 추후 DB화 필요

---

## 제외되는 링크

추출 시 아래 링크는 무시됩니다:

- **UI 요소**: 닫기, 로그인, 장바구니, 검색 등
- **외부 링크**: shop.kt.com 외부 URL
- **상품 상세**: `/mobile/view.do`, `/accessory/accsProductView.do`

> ⚠️ 추후 DB화 필요

---

## 설정 변경

`kt_extract_submenu_from_pages.py`의 `main()` 함수에서 수정:

```python
INPUT = 'kt_menu_final_with_url.json'  # 입력 파일명
OUTPUT = 'kt_menu_crawled.json'        # 출력 파일명
DELAY = 1.0                            # 페이지 간 대기 시간(초)
```

---

## 로그 예시

```
🔍 [3depth] 모바일 > 요금제 > 5G 요금제
   URL: https://shop.kt.com/mobile/plan.do
📄 1페이지 수집...
   → +10개 (총 10개)
📄 2페이지 수집...
   → +10개 (총 20개)
✅ 완료 (20개)
```

---

## 문제 해결

### Q: 1단계에서 메뉴가 안 펼쳐져요
브라우저가 headless 모드라 안 보일 수 있어요. `headless=False`로 되어있는지 확인하세요.

### Q: 특정 페이지에서 크롤링을 안 해요
`SKIP_CRAWL_PATTERNS`에 해당 패턴이 있는지 확인하세요.

### Q: 중간에 중단했는데 다시 시작하면?
이미 `children`이 채워진 노드는 건너뛰므로, 출력 파일을 입력으로 사용하면 이어서 진행됩니다:

```python
INPUT = 'kt_menu_crawled.json'  # 출력 파일을 입력으로
```

---

## 파일 구조

```
├── kt_menu_dom_extract.py           # 1단계: DOM 추출
├── kt_menu_parse.py                 # 2단계: 메뉴 파싱
├── kt_extract_submenu_from_pages.py # 3단계: 하위 링크 수집
├── kt_menu_full_dom.html            # DOM 데이터
├── kt_menu_final_with_url.json      # 메뉴 트리 (빈 children)
├── kt_menu_crawled.json             # 메뉴 트리 (채워진 children)
└── README.md                        # 이 파일
```

---

## TODO

- [ ] 스킵 패턴 DB화
- [ ] 제외 링크 패턴 DB화
- [ ] 병렬식으로→ 대 메뉴 기준으로 Shop/상품/혜택/고객지원/마이
- [ ] 하나의 구조로 합치기