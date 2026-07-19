# 포트폴리오/스케줄 데이터 정합성 종합 수정 스펙

> 기획·진단: 페이블 / 구현: 오푸스
> 사용자 지적: 다음평가일·캘린더가 틀림, 163→161 누락, 이번달 건수 오류.
> 전수 조사로 근본원인 6종(A~F) 확정. 아래 순서대로 구현·검증.

프로젝트: C:\Users\Taehoon\Desktop\ELS투자\platform (Django5/SQLite/Windows)
쉘: `cd "/c/Users/Taehoon/Desktop/ELS투자/platform" && python manage.py check`

---

## A. 비균등 조기상환 스케줄 지원 + 개선된 주기 판정 ★가장 중요

**문제**: `Investment.schedule`(core/models.py)가 "주기 × 회차"로 **균등**하다고 가정.
그러나 일부 상품(예: NH 320)은 **첫 조기상환 3개월, 이후 1개월 간격**인 비균등 구조.
또 주기 텍스트가 없는 상품은 `extract_period` 폴백(만기÷배리어)이 소수→반올림으로 틀림.

**사용자 확정 판정 규칙**:
1. `총개월 ÷ 배리어수`가 정수 → 균등 주기 확정 (first=interval=몫)
2. 정수 아니면, **첫 3개월 가정**: `(총개월−3) ÷ (배리어수−1)`이 정수면
   → first_eval=3, interval=그 몫  (예: 320 → (12−3)/(10−1)=1 → 3개월+1개월)
3. 둘 다 안 되면 → 판정 불가 (추정 배지, 아래 F 참고)
- **day 아티팩트 보정**: 총개월 계산 시 발행일/만기일의 '일(day)'이 달라 23 vs 24 등으로
  어긋날 수 있음. span, span±1 세 값을 시도해 정수로 떨어지는 것을 채택.
- 설명에 명시적 주기 텍스트가 있으면(기존 extract_period 텍스트 패턴 매칭) 그게 최우선(확정).

**모델 변경 (core/models.py Product)**:
- `first_eval_months = IntegerField(null=True, blank=True)` 추가. null이면 period_months와 동일(균등).
- `schedule_estimated = BooleanField(default=False)` 추가. 주기를 판정 못해 임의 채운 경우 True.
- `period_months`는 "이후 조기상환 간격"으로 의미 유지.
- 마이그레이션 생성.

**Investment.schedule property 수정**:
```
base = product.issue_date or invested_at
n_barriers = len(barriers)
first = product.first_eval_months if product.first_eval_months else product.period_months
interval = product.period_months
for n in 1..n_barriers:
    months = first if n==1 else first + (n-1)*interval
    eval_date = _add_months(base, months)
    ...
```
- 균등(first==interval==period)이면 결과가 기존과 동일해야 함(회귀 없음). 검증할 것.
- `expected`(예상상환금) 계산의 elapsed months도 위 months를 그대로 사용(기존도 그러함).

**판정 로직 위치**: `core/parsers.py`에 새 함수
`infer_schedule(n_barriers, issue_date, expiry_date, desc) -> (first_eval, interval, estimated) | None`
- 텍스트 주기 우선 → 규칙1 → 규칙2 → None.
- `reparse_products` 커맨드가 이 함수를 써서 first_eval_months/period_months/schedule_estimated 세팅.
  (scrape_kofia/import_els도 동일 적용하면 좋으나, 이번엔 reparse_products 일괄 갱신으로 충분)

**단위 검증(필수)**:
- NH 320: issue 2026-06-18, 배리어10, expiry 2027-06-18 → first=3, interval=1.
  schedule[0].date == 2026-09-18, schedule[1].date == 2026-10-18, schedule[-1].date == 2027-06-18.
- 균등 예: issue 2026-06-05, 배리어9, expiry 2029-06-05 → 36/9=4 → first=interval=4.
  schedule[0]==2026-10-05, schedule[-1]==2029-06-05.
- 24888: issue 2026-07-02, 배리어8, expiry 2028-06-30 → span=23, +1=24, 24/8=3 → 균등3.

## B. 그룹B 자동복구 3건 (엑셀에 온전한 설명 있음)
downloads 엑셀에서 아래 상품의 설명을 찾아 해당 Product.description을 갱신 후 재파싱:
- 미래에셋 37858 → `조기상환형, 75-75-75-75-70-70, KI25, 3년만기 6개월 평가, 쿠폰 연32.6%`
- 삼성 30924 → `[스텝다운] 3년/3개월,45KI(90,90,90,90,90,90,85,85,85,80,80,75)%,세전 연 22%`
- 삼성 30994 → `[월지급식] 3년/3개월,25KI(85,85,85,85,80,80,80,80,75,75,75,70)%,월수익행사율 65%`
방법: downloads/청약중인상품_*.xlsx(ALL 시트, 가공본 _수정/테스트 제외)에서 (발행사,상품번호)로
가장 긴 설명을 찾아 그 상품의 **투자가 연결된 Product 행**의 description을 채우고 reparse.
(주의: 중복 행 존재 가능 — 투자가 연결된 행을 갱신해야 화면에 반영됨. D와 함께 처리 권장.)

## C. 그룹B 8건 — 사용자 정보 대기 (이번 구현 범위 아님)
미래에셋 37750/37751, 키움 1833/1837/1839/1840/3928/3933.
설명이 원본 엑셀에도 손상("종목형"/"지수형"/숫자). 사용자가 배리어+주기 제공 예정.
지금은 손대지 말고, F의 "확인 필요" 배지만 뜨게 둘 것.

## D. 중복 Product 행 정리 + 투자 재연결
(issuer, product_no)가 같은데 sub_end가 달라(하나는 None) 행이 2개 이상인 상품 다수.
업로드 매칭(_match_product_for_investment: order_by('-sub_end').first())이 배리어 없는 빈 행을 고르는 경우 발생(예: 미래에셋 37858 — 투자가 빈 행에 연결).
**수정**:
1. `_match_product_for_investment`(core/views.py) 개선: 후보 중 **배리어가 있는 행 우선**, 그다음 최신 sub_end.
2. 기존 잘못 연결된 투자 교정 커맨드(또는 데이터 마이그레이션): 각 (issuer,product_no)에서
   배리어 있는 "정상 행"을 찾아, 빈 행에 연결된 보유 투자를 정상 행으로 relink. 그 후 어디에도
   안 쓰이는 빈 중복 행은 삭제(Investment FK PROTECT라 relink 먼저).
   - 안전장치: 정상 행이 없으면 건드리지 말 것.

## E. 누락 2건 (삼성30868, 키움1806)
DB에 상품 자체가 없어 업로드 시 스킵됨. 원본 엑셀에도 없음. 이번엔 **손대지 말 것**
(사용자 정보 받으면 별도 처리). 단, F의 업로드 실패사유 표시로 이런 케이스가 다음부턴 보이게 함.

## F. 추정 배지 + 다운로드 버튼 + 업로드 실패사유
1. **추정 배지**: schedule_estimated=True 이거나 배리어/주기 없어 스케줄 못 만드는 상품의
   다음평가일·예상상환금·상환캘린더 항목 옆에 작은 "추정" 또는 "확인필요" 배지.
   - 포트폴리오 보유테이블 다음평가일 셀, 상환캘린더 이벤트에 표기.
   - 확정(텍스트 주기 or 규칙1/2 정수판정)은 배지 없음.
2. **보유상품 엑셀 다운로드 버튼**: 포트폴리오 화면(투자등록 카드의 엑셀 영역 근처)에
   "보유 내역 다운로드" 버튼. 현재 보유 투자를 xlsx로: 발행사/상품번호/기초자산/투자금액/
   수익률/KI/주기/다음평가일/예상상환금/손실확률/발행일/만기. 뷰 `portfolio_export`,
   URL `portfolio/export/`. (기존 portfolio_template 다운로드 방식 참고: openpyxl, HttpResponse)
3. **업로드 실패사유 표시**: portfolio_upload(core/views.py)가 이미 errors 리스트를 messages로
   보여주는데, 매칭 실패 행의 발행사·상품번호가 명확히 보이도록 메시지 문구 점검/개선.

---

## 검증 (필수 — 실행하고 수치 보고)
1. `python manage.py check` + `makemigrations --check` 없이 마이그레이션 정상 적용.
2. A 단위검증(위 3케이스) 손계산 일치.
3. reparse_products 재실행 후:
   - NH 320 보유투자의 next_evaluation.date == 2026-09-18 확인.
   - 이번달(7월) 평가예정 건수 재계산(사용자 기대 8건 근처인지 — 정확 일치 아니어도 근거 제시).
4. D 실행 후: 미래에셋 37858 보유투자가 배리어 있는 행에 연결되고 next_evaluation 정상.
5. 균등상품 회귀 없음: 기존 정상 상품 몇 개의 schedule이 변하지 않았는지 확인.
6. 다운로드: test client로 portfolio/export/ 200 + xlsx 헤더 확인.

## 하지 말 것
- git 커밋/푸시 금지 (페이블이 리뷰 후 커밋)
- 그룹B 8건(C)·누락2건(E)은 손대지 말 것 (사용자 데이터 대기)
- simulate_products.py는 최소 변경(스케줄 바뀌면 손실확률 재계산이 필요할 수 있으나, 그건 별도 배치 재실행으로 처리 — 이 스펙에선 simulate 코드 변경 불필요)

## 보고: 파일별 변경요약, 마이그레이션명, 검증 출력 전부, 회귀 확인 결과.
