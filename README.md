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
`ELS_Curator.exe` 실행 → `downloads/청약중인상품_*.xlsx` 생성
→ `import_els` 배치가 감지·파싱·DB저장·알림

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
python manage.py import_els        # 새 엑셀 감지 → 임포트 + 알림
```
Windows 매일 09:00 자동화: `register_scheduler.ps1` (관리자 PowerShell)

## 환경변수 (.env)
`.env.example` 참고. `DJANGO_DEBUG=0`으로 두면 운영 모드(외부 노출 시).
