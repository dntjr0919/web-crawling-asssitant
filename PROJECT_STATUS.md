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

## 사용자 검증 대기
- 본문 추출 전체 언론사 지원과 추출 결과의 텍스트 완전성 검증
- max_concurrency=2 실제 브라우저 동시 실행
- 사이트 화면 구조 변경 시 선택자 대응
- 대기 시간 초과, 창 닫기 중 정리 동작의 실제 화면 확인

## Computer 실행 환경
- 가상환경 Python 실행: 권한 추가 후 해결
- Playwright: 시작 단계 파이프 생성에서 접근 거부(WinError 5). Computer에서는 Playwright를 실행하지 않으며, 실제 실행은 사용자 VS Code에서 수행함

## 알려진 제약
- 네이버 이용약관은 사전 허락 없는 자동화 검색을 제한함
- Google 약관은 robots.txt 위반 자동화 접근을 금지하며, robots.txt가 /search를 Disallow 함
- 구글 CAPTCHA는 사용자 직접 인증 후 재개 방식만 사용하며 자동 해결·회피는 구현하지 않음
- 정렬·기간 조건 미적용(사이트 기본 정렬)
- 언론사별 이용약관·접근 허용 범위는 별도 확인 필요. 모든 언론사 지원·유료벽/인증 자동 판별을 보장하지 않음
- 본문 후보가 여러 개이거나 200자 미만이면 실패로 기록. 본문 준비 대기 8초, 페이지 이동은 기존 제한 시간 사용
- 로그인 판정은 미지원(현재 두 사이트 공개 검색만 사용). 비밀번호 저장·자동 입력 없음
- 진행 중 Playwright 호출은 브라우저를 닫아서 중단함

## 확인된 미지원 사례
- 뉴스1 사진 기사 https://www.news1.kr/photos/8172144 : 사용자 실행에서 본문 영역 없음 또는 200자 미만으로 실패.
  - 해당 페이지 HTML에서 짧은 사진 설명이 #articleBodyContent .m-auto div에 있음을 확인. 일반 기사 본문과 구분한 사진 설명 지원은 미구현.

## 향후 후보(미구현)
1. 사진 기사 설명 추출 지원(200자 미만 예외 또는 사진 전용 선택자)
2. 원문 URL 기준 수집 결과 중복 제거
3. 결과 CSV 내보내기 기능
4. 검색 기간·건수 GUI 설정 연동
5. 핵심 키워드 추출 및 본문 요약
6. 로그인 필요 사이트 및 로그인 상태 판정

## 변경하면 안 되는 결정
- Playwright async API, 전용 스레드 asyncio 실행부, Qt 시그널로 GUI 전달
- 평소 Chrome 프로필 사용 금지, 같은 프로필 동시 사용 금지
- CAPTCHA·접근 제한·추가 인증은 사용자 확인 또는 중지. 탐지 회피·우회 구현 안 함
