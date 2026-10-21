# PROJECT_STATUS

## 목표와 확정 기술
Windows 로컬 데스크톱 브라우저 자동화·동적 크롤링 오케스트레이터. 뉴스 검색은 첫 사용 사례.
Python 3.12.10 + PySide6 6.12.0 + Playwright 1.64.0 async API + asyncio. 설정·결과 JSON, 표준 logging.
API 수집기로 교체하지 않음. DB·서버·LLM·Docker 미사용.

## 실행 방법
- 설치: `& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt`
- 시작: `& '.\.venv\Scripts\python.exe' main.py`

## 파일 역할
- main.py: 앱 시작, 로그 설정(logs/app.log)
- config.json: 사이트별 최대 건수(10), 동시 실행 수(기본 1, 최대 2), 시간 제한, 저장 위치
- crawler_app/gui.py: PySide6 화면(검색어·사이트 선택·실행/중지/계속·상태·로그·결과·상세·JSON 저장)
- crawler_app/runner.py: 전용 스레드의 asyncio 실행부, 오케스트레이터, 취소·사용자 대기
- crawler_app/browser.py: 사이트별 전용 프로필(profiles/), 프로필 중복 사용 잠금
- crawler_app/models.py: 결과 형식, JSON 저장(기존 파일 보호)
- crawler_app/article.py: 공개 원문 본문 후보 추출, 기사별 실패·접근 제한 기록
- crawler_app/sites/common.py: 사이트 공통 동작(검색 입력, 탭 이동, 결과 대기, 제한 화면 판정)
- crawler_app/sites/naver.py, google.py: 사이트 구조 의존 선택자와 추출
- open_naver.py, verify_browser.py: 이전 단계 테스트용 스크립트(보존)

## 사용자 검증 완료
- 사용자 VS Code: .venv Python 3.12.10, Playwright 1.64.0, Chromium 설치
- open_naver.py: 네이버 홈 접속(HTTP 200, 제목 NAVER), 검색 실행과 정상 검색 결과 표시
- verify_browser.py: 15초 유지 및 정상 종료
- MVP 앱(main.py): PySide6 6.12.0 설치, 앱 시작, 첫 테스트 순서 1~5 사용자 확인
  - 네이버: 검색 → 뉴스 탭 → 결과 수집·표시, 상세 보기, JSON 저장
  - 구글: CAPTCHA 표시 → '사용자 확인 필요'로 대기 → 사용자가 직접 인증 후 '계속' → 재개·수집
  - 실행 중 중지: 정상 동작
- 본문 추출 확장: 첫 사용자 실행 확인 완료
  - 보이는 본문 영역(#dic_area, itemprop=articleBody, article의 문단)만 후보로 사용.
  - 본문 결과를 포함한 JSON 저장 정상.
- 네이버 비기사 링크 필터링 보강 (사용자 확인 완료):
  - `crawler_app/sites/naver.py`의 `EXTRACT_JS` 내 `naver_link_heuristic` 수정.
  - `*.naver.com`, `*.naver.net` 및 도메인 루트(`/`) 링크를 검색 결과 후보에서 제외.
  - 네이버 언론사 구독 페이지(`media.naver.com`) 유입으로 인한 본문 추출 건너뜀(`skipped`) 항목 제거 확인.
- 사진 기사 설명 추출 지원 (뉴스1 검증 완료):
  - `crawler_app/article.py`의 `PHOTO_CAPTION_SELECTORS`를 `#articleBodyContent .m-auto`로 수정.
  - 뉴스1 사진 기사(https://www.news1.kr/photos/8172144)에서 정상 추출(`status: extracted`, `method: photo:#articleBodyContent .m-auto`) 검증 완료.
- 원문 URL 기준 수집 결과 중복 제거 구현 및 검증 완료:
  - `crawler_app/dedup.py` 신규 생성: URL 정규화(프로토콜/호스트 정리, 트래킹 파라미터 정제) 및 고유 식별 키 추출(`original_link` 우선).
  - `crawler_app/runner.py`: 사이트별 수집 후 및 사이트 간 수집 시 중복 기사 자동 제거, 중복된 기사의 본문 추출 낭비 방지.
  - `crawler_app/gui.py`: '원문 중복 제거' 체크박스(기본 True) 추가 연동.
  - 단위 테스트 및 러너 통합 테스트 통과 확인.
- 결과 CSV 내보내기 기능 구현 및 검증 완료:
  - `crawler_app/models.py`: `save_job_csv`, `default_csv_path` 구현 (Excel 한글 깨짐 방지 UTF-8 BOM `utf-8-sig` 적용).
  - `crawler_app/gui.py`: 'CSV 저장' 버튼 추가 연동.
  - 단위 테스트(BOM 헤더 및 데이터 컬럼 일치 검증) 통과 확인.
- 검색 건수 및 기간 GUI 설정 연동 구현 및 검증 완료:
  - `crawler_app/gui.py`: 최대 건수(1~50건 SpinBox), 기간(전체/1일/1주/1개월/1년 ComboBox) 컨트롤 추가.
  - `crawler_app/runner.py`: `_Job` 및 `SiteRunContext`에 `max_results`와 `period` 전달 연동.
  - `crawler_app/sites/naver.py`, `google.py`: 기간 파라미터(네이버 `pd`/`nso`, 구글 `tbs`) 자동 적용.
  - 네이버 대상 실제 1일(1d), 3건 지정 검색 및 수집 검증 통과 확인.
- 구글 본문 추출 복구 및 수집 대상(뉴스/웹/블로그) 선택 기능 구현 완료:
  - `crawler_app/sites/google.py`: 구글 뉴스의 게이트웨이 링크(`/goto?url=...`)를 원문 링크로 정상 판별하여 본문 추출 실패 문제 해결.
  - `crawler_app/gui.py`: '수집 대상'(뉴스/웹 전체/블로그) 선택 콤보박스 및 '동시 실행'(1~2개) 스핀박스 추가.
  - `crawler_app/sites/naver.py`, `google.py`: 뉴스/웹/블로그 대상별 탭 및 선택자 연동, 블로그 글 링크(`blog.naver.com`) 원문 링크 지원.
  - 블로그 수집 실제 통합 테스트 통과 확인.

## 사용자 검증 대기
- 본문 추출 전체 언론사 지원과 추출 결과의 텍스트 완전성 검증
- max_concurrency=2 실제 브라우저 동시 실행
- 사이트 화면 구조 변경 시 선택자 대응
- 대기 시간 초과, 창 닫기 중 정리 동작의 실제 화면 확인

## Perplexity Computer 실행 환경(Perplexity Computer로 실행한 경우에 해당)
- 가상환경 Python 및 Playwright 실행: 정상 동작 확인 완료.
- 테스트 정책: 단위 테스트나 로직 검증은 환경 내에서 직접 Headless 모드로 실행·검증하며, GUI 조작이나 실제 사용자 확인이 꼭 필요한 항목만 사용자에게 요청함.

## 알려진 제약
- 네이버 이용약관은 사전 허락 없는 자동화 검색을 제한함
- Google 약관은 robots.txt 위반 자동화 접근을 금지하며, robots.txt가 /search를 Disallow 함
- 구글 CAPTCHA는 사용자 직접 인증 후 재개 방식만 사용하며 자동 해결·회피는 구현하지 않음
- 정렬 조건 미적용(사이트 기본 정렬 사용, 기간 필터는 전체/1일/1주/1개월/1년 지원)
- 페이지네이션 미적용(현재는 검색 결과 1페이지에 노출된 항목 중에서 최대 건수만큼 수집, 네이버는 약 20~30건, 구글은 10건 내외)
- 언론사별 이용약관·접근 허용 범위는 별도 확인 필요. 모든 언론사 지원·유료벽/인증 자동 판별을 보장하지 않음
- 본문 후보가 여러 개이거나 200자 미만이면 실패로 기록. 본문 준비 대기 8초, 페이지 이동은 기존 제한 시간 사용
- 로그인 판정은 미지원(현재 두 사이트 공개 검색만 사용). 비밀번호 저장·자동 입력 없음
- 진행 중 Playwright 호출은 브라우저를 닫아서 중단함

## 확인된 미지원 사례
- 없음 (추가 미지원 언론사 또는 레이아웃 발견 시 추가)

## 향후 후보(미구현)
1. 검색 결과 1페이지 초과 시 페이지네이션(Next Page) 추가 수집
2. 사진 기사 설명 추출 지원 대상 언론사 확대 (현재 뉴스1 지원)
3. 핵심 키워드 추출 및 본문 요약
4. 로그인 필요 사이트 및 로그인 상태 판정

## 변경하면 안 되는 결정
- Playwright async API, 전용 스레드 asyncio 실행부, Qt 시그널로 GUI 전달
- 평소 Chrome 프로필 사용 금지, 같은 프로필 동시 사용 금지
- CAPTCHA·접근 제한·추가 인증은 사용자 확인 또는 중지. 탐지 회피·우회 구현 안 함
