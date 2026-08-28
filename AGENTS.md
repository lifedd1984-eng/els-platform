# AGENTS.md — ELS 레이더 코드 작업 가이드

이 저장소에서 코드를 만드는 AI 에이전트(코덱스 등)를 위한 지침입니다.
아래의 **주의사항은 전부 실제로 겪은 사고에서 나온 것**이니 반드시 지켜 주세요.

## 작업 방식

- 기획·검증과 구현은 분리한다. 구현자는 **주어진 스펙대로** 작성하고, 애매하면 임의로 정하지 말고 질문한다.
- **배지 상수·선별 규칙·백테스트 판정 로직의 수치는 임의로 바꾸지 않는다.** 이 값들은 10년 데이터 검증으로 확정된 것이라, 변경은 검증을 거쳐야 한다.
- 구현 중 발견한 버그·개선점은 고치기 전에 먼저 보고한다.
- 무엇을 바꿨고 어디에 영향이 갈 수 있는지(특히 배지·판정·시세 경로)를 커밋·PR 설명에 분명히 남긴다.

## 스택 · 구조

- Django 6.x, 단일 앱 `core`, 프로젝트 패키지 `els_platform`. DB는 SQLite(`db.sqlite3`).
- 모듈 지도 (`core/`):
  | 파일 | 역할 |
  |---|---|
  | `models.py` | 모델 + 배지 튜닝 상수(상단 ⚙️ 마커 블록) |
  | `views.py` / `els_platform/urls.py` | 화면·라우트 |
  | `hist_radar.py` | 과거 재현 배지(`AdaptiveGate`, 트레일링 퍼센타일) |
  | `backtest.py` | 백테스트 |
  | `market.py` | 시세·티커 정규화(`resolve_ticker`, `split_assets`) |
  | `kofia_scraper.py` | KOFIA 자동 수집 |
  | `compare.py` | 유사상품 비교 |
  | `asset_pages.py` | 기초자산별 공개 페이지(상위 10개, 영문 슬러그) |
  | `push.py` | 웹 푸시 |
  | `ask_*.py` | AI 분석 질문 |
  | `threads_*.py` | 스레드 자동화 |
  | `sitemaps.py` | SEO |
- 스펙 문서: 루트 `SPEC.md`, `SIM_SPEC.md`, `FIX_SPEC.md`.

## 개발 · 테스트

```bash
pip install -r requirements.txt
cp .env.example .env        # 값 채우기
python manage.py migrate
python manage.py runserver
python manage.py test        # 테스트는 core/tests_*.py
```

주요 관리 명령: `scrape_kofia`(수집) · `update_prices`(시세) · `verify_historical`(검증) · `simulate_*`(시뮬).

운영은 EC2 + Cloudflare 터널 + gunicorn(systemd `els-web`). 배포는 `git pull` 후 서비스 재시작. **서버 패키지는 pip이 아니라 uv로 관리**한다.

## 반드시 지킬 함정

1. **Django 템플릿 주석 `{# … #}`은 한 줄만 된다.** 여러 줄로 쓰면 안 닫혀 화면에 그대로 찍힌다.
2. **시간대: 크론은 UTC, 코드는 KST.** 시스템 TZ가 UTC라 crontab 시각은 UTC로 읽어야 하고(`30 0`=09:30 KST), Django는 `TIME_ZONE='Asia/Seoul'`이라 `date.today()`가 서울 날짜다. 배치 스크립트도 `export TZ=Asia/Seoul`.
3. **손실 판정 시 조기상환 여부를 먼저 제외한다.** 안 하면 조기상환 상품을 만기 시세로 재판정해 손실을 과대계상한다.
4. **시세는 있는 그대로 쓴다.** 급등을 이상치로 보고 필터 넣지 말 것. `update_prices`는 `fetch_current_price`, 백테스트는 `fetch_history` — 두 경로는 별개다.
5. **`radar_tracks()`는 '오늘 이후 마감'만 반환** → 주말엔 0건이 정상. 전체 주간 검증은 명시적 `monday~sunday`로.
6. **Cloudflare가 `Java/` User-Agent를 403 차단**(Browser Integrity Check). 외부 봇 검증을 서버로 받을 때 실패할 수 있다(파일 방식 등 우회, `views.naver_verify` 참고).
7. **KOFIA 스크래퍼는 날짜 무관 정적 요청.** 발행 0건 주는 실제 시장 데이터지 버그가 아니다.
8. **퍼센타일 경계는 '미만(`<`)' 필수.** '이하(`≤`)'면 경계 상품이 새어 과거 손실이 발생한다.
9. **배지/선별 수치(`RADAR_KI_EXCL`, `RADAR_LAST_MAX`, `RADAR_V7_RELAX_RET1Y` 등)는 검증으로 확정된 상수.** 리팩터링 중에도 값 자체를 바꾸지 말 것.
10. **시세 캐시**: `fetch_history`가 워커별 프로세스 캐시라, 배치 끝에 `/weekly/`를 호출해 워밍한다. 관련 코드 수정 시 유의.

## 건드리지 말 것

- **비밀값**(`.env`의 `SECRET_KEY`·`VAPID_*`·토큰·DB 접속정보)을 코드/커밋/로그에 노출하지 말 것. **VAPID 키를 재생성하면 기존 웹푸시 구독이 전부 무효화**된다.
- 이 저장소 밖의 **다른 서비스·외부 계정 설정**은 건드리지 않는다(동일 서버에 별도 서비스가 함께 있을 수 있음).
- 라이브 DB/사이트 파괴적 조작 금지. 마이그레이션은 되돌릴 수 있게, 백업 확인 후.

## 컨벤션

- **이모지 금지 → FontAwesome 사용.** 라운딩·애니메이션 절제. 모바일 반응형 필수.
- 한국어 텍스트는 순수 한글(한자·일본어 혼용 금지).
- 주변 코드의 스타일·명명·주석 밀도를 따른다.

## 도메인 · 규제 (콘텐츠·로직 공통)

- 사용자 노출 콘텐츠에 **발행사명·상품번호·개별 쿠폰을 넣지 말 것**(금융소비자보호법 제22조①). 익명처리 "A증권사"만 허용.
- **개별 투자판단·조언 금지.** 안내·자동응답은 개별 사정을 반영하지 않는 정형 문구. 금액 배분 기능은 '사용자 입력 계산 도구' 형태로 설계한다.
- **ELS 만기 설명 정확성**: 낙인은 손실 확정 스위치가 아니다.
  - 낙인 미발생 → 원금 + 이자
  - 낙인 발생해도 만기 평가일에 상환 조건 이상 회복 → 원금 + 이자(전액)
  - 낙인 발생 + 만기에도 그 아래 → 하락분만큼 원금 손실
