"""사이트 모듈 목록. 새 사이트는 모듈을 만들고 여기에 한 줄 추가한다."""
from . import google, naver

SITES = {
    naver.SITE_ID: naver,
    google.SITE_ID: google,
}
