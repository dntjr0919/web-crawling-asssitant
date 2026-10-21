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

- 본문 추출 첫 사용자 검사: 10건 중 본문 후보 8건 추출, 1건 실패, 1건은 네이버 언론사 구독 페이지로 건너뜀(사용자 보고).
- 추출한 8건 중 사용자가 직접 대조한 1건은 실제 본문과 일치. 나머지 7건 정확성은 미검증.
- 본문 결과를 포함한 JSON 저장 정상(사용자 확인).

## 사용자 검증 대기
- 본문 추출 확장: 첫 사용자 실행 확인 완료. 전체 언론사 지원과 나머지 결과 정확성은 검증 대기.
  - GUI 체크박스는 기본 해제. 해제 시 기존 검색 결과만 수집.
  - 체크 시 확인된 원문 링크를 사이트별 전용 컨텍스트의 별도 탭으로 한 건씩 열어 처리.
  - 본문 요청은 사이트 동시 실행 설정과 무관하게 전체 작업에서 최대 1개.
  - 보이는 본문 영역(#dic_area, itemprop=articleBody, article의 문단)만 후보로 사용.
  - 추출 상태: not_requested / pending / processing / extracted / failed / blocked / skipped / cancelled.
  - extracted는 본문 후보 추출이며 기사 전문의 완전성·정확성 보장 아님.
  - 원문 링크 없으면 skipped. 유료벽·접근 제한·인증 화면은 blocked 또는 failed로 기록하고 다음 기사 처리.
  - 숨겨진 본문·JSON-LD·검색 설명을 본문으로 대체하지 않음. 기사 본문 인증 자동 해결 없음.
  - JSON schema_version=2. 본문·상태·오류·최종 URL·방식·수집 시각 포함. 기존 결과 파일은 변경하지 않음.
  - 본문 확장 검사(Computer 클라우드 작업 공간, 브라우저 실행 없음):
    Python 문법·pyflakes, 가짜 객체 단위 검사 10개, 가짜 DOM JavaScript 검사 6개 통과.
    기존 순수 실행부 로직 회귀 검사도 통과. 실제 GUI·기사 검증은 위 사용자 확인 범위에 한정.
- max_concurrency=2 실제 브라우저 동시 실행
- 사이트 화면 구조 변경 시 선택자 대응(현재 선택자는 이번 실행 기준으로만 동작 확인)
- 대기 시간 초과, 창 닫기 중 정리 동작의 실제 화면 확인

## Computer 실행 환경
- 가상환경 Python 실행: 권한 추가 후 해결
- Playwright: 시작 단계 파이프 생성에서 접근 거부(WinError 5). 원인 미확정, 진단 보류
- Computer에서는 Playwright를 실행하지 않음. 실제 실행은 사용자 VS Code에서 함

## 알려진 제약
- 네이버 이용약관은 사전 허락 없는 자동화 검색을 제한함(https://policy.naver.com/rules/service.html)
- Google 약관은 robots.txt 위반 자동화 접근을 금지하며, robots.txt가 /search를 Disallow 함
- 구글은 실제 실행에서 CAPTCHA가 표시됨. 사용자 직접 인증 후 재개 방식만 사용하며 자동 해결·회피는 구현하지 않음
- 정렬·기간 조건 미적용(사이트 기본 정렬). 최신순으로 표시하지 않음
- 언론사별 이용약관·접근 허용 범위는 별도 확인 필요. 모든 언론사 지원·유료벽/인증 자동 판별을 보장하지 않음.
- 본문 후보가 여러 개이거나 200자 미만이면 실패로 기록. 본문 준비 대기 8초, 페이지 이동은 기존 제한 시간 사용.
- 로그인 판정은 미지원(현재 두 사이트 공개 검색만 사용). 비밀번호 저장·자동 입력 없음
- 진행 중 Playwright 호출은 브라우저를 닫아서 중단함. 브라우저 시작 도중의 중지는 시작이 끝난 뒤 반영됨

## 확인된 미지원 사례
- 뉴스1 사진 기사 https://www.news1.kr/photos/8172144 : 사용자 실행에서 본문 영역 없음 또는 200자 미만으로 실패.
- 해당 페이지 HTML에서 짧은 사진 설명이 #articleBodyContent .m-auto div에 있음을 확인. 일반 기사 본문과 구분한 사진 설명 지원은 미구현.
- 검색 추출 방식 naver_link_heuristic에서 비기사 링크(언론사 구독 페이지)가 포함된 사용자 사례 있음. 검색 항목 필터 보강은 미구현.

## 향후 후보(미구현)
본문 추출의 언론사별 검증·선택자 개선, 핵심 키워드, 본문 요약, 원문 URL 기준 중복 제거, CSV 내보내기,
기간·건수 GUI 설정, 로그인 필요 사이트와 로그인 상태 판정

## 변경하면 안 되는 결정
- Playwright async API, 전용 스레드 asyncio 실행부, Qt 시그널로 GUI 전달
- 평소 Chrome 프로필 사용 금지, 같은 프로필 동시 사용 금지
- CAPTCHA·접근 제한·추가 인증은 사용자 확인 또는 중지. 탐지 회피·우회 구현 안 함
