# ELS 플랫폼

주간 ELS 청약 상품을 한눈에 보고, 프리셋 매칭 알림과 투자 포트폴리오·상환 캘린더를 관리하는 개인/가족용 웹 앱.

## 화면
- **주간 청약** — 이번 주 청약 마감 상품을 마감일별로 정리, 프리셋·필터
- **프리셋** — 조건 저장, 매칭 시 텔레그램 알림
- **상품 상세** — 배리어 계단 차트 + 원문
- **포트폴리오** — 투자 등록, 예상 상환금 자동 계산
- **상환 캘린더** — 조기상환 평가일 월간 뷰
- **관심 목록**

## 데이터 흐름

**방법 A — 자동 수집 (권장)**
`scrape_kofia` 배치가 KOFIA 전자공시(dis.kofia.or.kr)에서 청약중인 ELS/DLS/ELB/DLB를
직접 가져와 파싱·저장·알림까지 자동 처리. 수동 조작 불필요.

**방법 B — 엑셀 업로드 (백업/보완용)**
`ELS_Curator.exe` 실행 → `downloads/청약중인상품_*.xlsx` 생성
→ `import_els` 배치(또는 웹 업로드 화면)가 감지·파싱·DB저장·알림

두 방식은 같은 Product 테이블을 공유하며 `product_code`(KOFIA 고유코드가 있으면 그것으로,
없으면 발행사+상품번호+청약마감일로) upsert되어 중복 없이 병행 가능.

## 설치
```bash
pip install -r requirements.txt
cp .env.example .env      # 값 채우기 (SECRET_KEY, 텔레그램 등)
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## 배치
```bash
python manage.py scrape_kofia      # KOFIA 자동 수집 (권장, exe 불필요)
python manage.py import_els        # 새 엑셀 감지 → 임포트 + 알림 (백업 경로)
python manage.py update_prices     # 보유 상품 낙인 시세 갱신
```
Windows 매일 09:00 자동화: `register_scheduler.ps1` (관리자 PowerShell)

### scrape_kofia 참고사항
- KOFIA의 비공식 내부 API(`DISDlsOfferSO.selectSubscribing`)를 그대로 호출 — 문서화된
  공개 API가 아니므로 **KOFIA 페이지 개편 시 깨질 수 있음**. 실패 시 콘솔 로그 및
  텔레그램 알림으로 확인 가능.
- 상품설명(원문) 문구가 다양해 KI/배리어/주기 자동 추출률이 exe 소스보다 다소 낮음
  (2026-07-18 기준 KI 71%, 배리어 63%, 주기 82% — `core/parsers.py`에 패턴 추가로
  지속 개선 중). 놓친 상품은 상품 상세 화면에서 원문 설명을 직접 확인 가능.
- 브라우저/Playwright 불필요 — 순수 `requests`로 동작 (가볍고 어떤 호스팅에서도 실행 가능).

## 환경변수 (.env)
`.env.example` 참고. `DJANGO_DEBUG=0`으로 두면 운영 모드(외부 노출 시).
