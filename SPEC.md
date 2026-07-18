# ELS 자동투자 플랫폼 — 구현 스펙 v1.0

> 작성: 페이블 (기획) / 구현: 오푸스
> 사용자: Taehoon — Django 경험자 (절세해 운영), UI는 토스 스타일 선호, 이모지 금지(FontAwesome CDN 사용), 라운딩·애니메이션 절제

## 0. 한 줄 요약
KOFIA ELS 청약 데이터를 매일 자동 수집·파싱해 **주간 단위 웹 대시보드**로 보여주고,
조건 프리셋에 매칭되는 상품을 텔레그램으로 알리는 로컬 웹 플랫폼.
**청약 실행 자체는 자동화하지 않는다** (숙려제도·API 부재 — 최종 결정은 사람).

## 1. 기존 자산 (재활용 필수)
| 자산 | 경로 | 용도 |
|------|------|------|
| **데이터 수집기** | `Desktop\ELS투자\ELS_Curator_v1.3.exe` (서드파티 툴) | 주 1회 수동 실행 → `Desktop\ELS투자\downloads\청약중인상품_YYYYMMDD_HHMM.xlsx` 생성. **이것이 유일하게 검증된 수집원** |
| 파싱 함수 | `Desktop\휴지통\els_cleaner_app.py` | extract_ki / extract_barriers / extract_period / classify_asset — **검증 완료, 그대로 이식** |
| 텔레그램 봇 | 토큰: `.env` (els_collector_ready), chat_id 8023647003 | 알림 재활용 |
| 분류 로직 | `ELS_Process.py` | KI 구간별 카테고리 참고 |

> ⚠ 업데이트 (2026-07-18): `ELS_DN.py`의 옛 엔드포인트(`proworks/callServletService.jsp`,
> form 인코딩)는 여전히 WAF에 막힘 — 이식 금지. 그러나 **다른 정식 엔드포인트
> (`proframeWeb/XMLSERVICES/`, XML 바디 + Referer 헤더)는 WAF를 통과하며 순수 `requests`로
> 동작 확인됨.** `core/kofia_scraper.py` + `scrape_kofia` 커맨드로 구현 완료 —
> 이제 이것이 1순위 자동 수집 경로이고, exe+엑셀 업로드는 백업/보완 경로로 격하됨.

## 2. 기술 스택
- **백엔드**: Django 5.x + SQLite (단일 사용자, 로컬 실행)
- **프론트**: Django 템플릿 + 바닐라 JS (SPA 불필요). FontAwesome CDN, 이모지 금지
- **스케줄러**: Windows 작업 스케줄러 등록 스크립트 제공 (매일 09:00 수집 management command)
- **알림**: 텔레그램 sendMessage/sendDocument
- 프로젝트 위치: `C:\Users\Taehoon\Desktop\ELS투자\platform\`

## 3. 데이터 모델
```
Product        # 수집된 ELS 상품 (이력 축적 — 삭제하지 않음)
  issuer, issuer_short, product_no, name, product_type(ELS/DLS/ELB)
  yield_rate(float), ki(int|null, NoKI=null+is_no_ki=True)
  barrier_first, barrier_last, barriers_raw(json), period_months
  asset_type(지수형/종목형), assets_raw, assets_short
  issue_date, expiry_date, sub_start, sub_end
  currency, description(원문), collected_at
  unique_together: (issuer, product_no, sub_end)

Preset         # 조건 프리셋 (기본 3종 시드 + 사용자 수정/추가/삭제 가능)
  name, is_default(bool)
  ki_max(int|null), ki_min(int|null), include_no_ki(bool)
  asset_type(지수형/종목형/전체), yield_min(float|null)
  period_max(int|null), currency(KRW/USD/전체)
  notify(bool)  # 매칭 시 텔레그램 알림 여부

WatchItem      # 관심 목록 (아직 투자 전)
  product(FK), memo, created_at

Investment     # 실제 투자 기록 (user FK — Django 기본 auth, 단일 계정)
  user(FK), product(FK)
  amount(int, 원 단위 투자금액), invested_at(청약일)
  broker_account(str, 선택 — 증권사/계좌 메모)
  status(보유중/조기상환/만기상환/낙인후상환)
  redeemed_at(date|null), redeemed_amount(int|null)  # 실제 상환 시 기록
  memo

# 상환 스케줄은 별도 테이블 없이 property로 계산:
#   평가일 목록 = issue_date + period_months × n (n=1..만기까지)
#   각 회차 예상상환금 = amount × (1 + yield_rate/100 × (period_months×n)/12)
#   각 회차 배리어 = barriers_raw[n-1]
```

**기본 프리셋 시드 (migration으로 생성, 수정·삭제 가능)**:
1. "저낙인 종목형" — 종목형, KI ≤ 25, 수익률 ≥ 15
2. "안정 지수형" — 지수형, KI ≤ 40 (NoKI 포함), 수익률 ≥ 10
3. "고수익 헌터" — 전체, 수익률 ≥ 20

## 4. 화면 (6개)
1. **주간 청약** (메인 `/`)
   - 이번 주(월~일) 청약마감 상품을 마감일별 그룹핑. 주 이동 네비(◀ 이번주 ▶)
   - 필터바: KI 범위 / 자산유형 / 수익률 / 통화. 프리셋 원클릭 적용
   - 컬럼: 발행사, 상품번호, 기초자산, 수익률, KI, 1차/막차 배리어, 주기, 마감일(D-day 강조)
   - 마감 D-1 이하는 행 강조
2. **프리셋 관리** (`/presets/`)
   - 목록 + 인라인 수정 + 추가/삭제. 기본 프리셋도 수정 가능(is_default는 배지만)
   - 각 프리셋의 "현재 매칭 상품 수" 표시
3. **상품 상세** (`/product/<id>/`)
   - 파싱 결과 + 원문 설명 대조 표시
   - 배리어 계단 차트 (barriers_raw 시각화 — 순수 SVG, 라이브러리 금지)
   - 관심 등록 / 청약함 기록 버튼
4. **내 포트폴리오** (`/portfolio/`)
   - 상단 요약 카드: 총 투자금액 / 보유 건수 / 이번 달 평가 예정 건수 / 누적 상환수익
   - 보유 목록: 상품, 투자금액, 다음 평가일(D-day), 다음 회차 배리어, 예상 상환금액
   - 투자 등록 폼: 상품 검색 → 금액·청약일·계좌 입력
   - 상환 처리: 상태 변경 + 실제 상환금액 입력 → 수익률 자동 계산 표시
5. **상환 캘린더** (`/calendar/`)
   - 월간 캘린더 뷰 (순수 HTML/CSS — 라이브러리 금지)
   - 보유 상품의 조기상환 평가일을 날짜 칸에 표시: 상품명, 회차, 배리어, 예상상환금
   - 평가일 D-7 / D-1에 텔레그램 알림 (collect_els 배치에서 함께 검사)
6. **관심 목록** (`/watchlist/`) — 투자 전 후보 관리, 원클릭으로 투자 등록 전환

## 5. 데이터 입수 및 배치 (management commands)

**수집 흐름 (반자동)**:
사용자가 주 1회 `ELS_Curator_v1.3.exe` 실행(유일한 수동 단계, ~1분)
→ `Desktop\ELS투자\downloads\`에 `청약중인상품_*.xlsx` 생성
→ 이후 전부 자동 (아래 배치가 감지·임포트·알림)

- `import_els` — downloads 폴더 스캔 → 미처리 xlsx 감지(처리 이력은 ImportLog 테이블로 관리)
  → ALL 시트 파싱(다른 시트는 ALL의 부분집합이므로 무시) → extract_* 적용 → Product upsert
  → notify=True 프리셋 매칭 검사 → **신규 매칭 상품만** 텔레그램 발송 (중복 알림 금지)
  → 보유 Investment의 평가일 D-7/D-1 검사 → 상환 예정 알림 (같은 회차 중복 발송 금지)
  → 임포트 완료 요약 텔레그램 발송
- 매일 09:00 실행하되, **월~수 새 파일이 없으면 목요일 09:00에 리마인더 발송**:
  "이번 주 ELS 데이터가 아직 없습니다. ELS_Curator를 실행해주세요."
- `register_scheduler.ps1` — Windows 작업 스케줄러에 매일 09:00 등록
- 대시보드에도 "데이터 신선도" 배지 표시 (마지막 임포트 N일 전, 7일 초과 시 경고색)
- 임포트 실패 시 텔레그램으로 오류 알림

**완료 (2026-07-18)**: `scrape_kofia` 커맨드가 exe 의존 없이 KOFIA를 직접 자동 수집.
Playwright/브라우저 불필요 — 순수 requests. 다만 KI/배리어/주기 추출률이 exe 소스보다
낮음(KI 71%, 배리어 63%) — 원문 문구 다양성 때문. `core/parsers.py` 패턴 확장으로
지속 개선 여지 있음(2차 로드맵으로 남김).

## 6. 텔레그램 메시지 형식
```
[주간 ELS 브리핑] 06/29 09:00
신규 수집 34건 / 프리셋 매칭 5건

<저낙인 종목형> 3건
- 키움 1950 (19.08%) KI25 Broadcom/Qualcomm ~07.03
...
대시보드: http://localhost:8000
```

## 7. 인증
- Django 기본 auth 사용. createsuperuser로 본인 계정 1개 생성 (회원가입 화면 없음)
- 전 화면 login_required. 로그인 화면만 비인증 접근 가능
- Investment 등 개인 데이터는 user FK로 연결 (향후 계정 추가 대비)

## 8. 명시적 비범위 (하지 말 것)
- 증권사 로그인/청약 자동 실행 (법적·기술적 불가 — 기획 단계에서 확정된 결정)
- 회원가입/이메일 인증 등 공개 서비스용 기능 (createsuperuser로 충분)
- 실시간 수집 (일 1회 배치로 충분)
- 실제 지수/주가 연동한 낙인 모니터링 (2차 로드맵 — 지금은 하지 않음)

## 9. 검증 기준
- 기존 엑셀 `Desktop\ELS투자\downloads\청약중인상품_20260314_1144.xlsx` 105건 기준
  파싱 누락이 els_cleaner_app 최신 버전과 동일 수준(구조적 무배리어 상품 제외 전부 추출)
- import_els 2회 연속 실행 시 중복 Product / 중복 알림 없음 (동일 파일 재처리 금지 포함)
- 대시보드 주간 그룹핑·프리셋 필터 실동작 확인
- 투자 등록 → 캘린더에 평가일 표시 → 예상상환금 계산 단위 테스트:
  1,000만원 / 연 20% / 6개월 주기 → 1회차 10,000,000×(1+0.20×6/12)=11,000,000원, 2회차 12,000,000원
- 상환 처리 시 실현수익률 = (redeemed_amount - amount) / amount 연환산 표시
