# kakaopage_dl

카카오페이지 기다무 대여권 자동 사용 + 회차 이미지 다운로드 SJVA 플러그인.

## 동작

스케줄러가 돌 때마다 설정에 적힌 작품들을 순회:

1. 작품 검색 (`bff-page.kakao.com/.../v1/search/series`) → series_id 매칭
2. 기다무 충전 상태 확인 (`v1/ticket/my`)
3. 회차 목록 + 마지막 본 회차 추출 (`v2/content/product/list`의 `last_view` 마커)
4. 다음 화 하나 골라서:
   - `v1/ticket/ready_to_use` → 사용 가능한 ticket_rental_type 확인
   - `v1/ticket/use` (`ticket_type=RT05`로 기다무) → ticket_uid 발급
   - `v5/inven/open_page` → 열람 등록
   - `v1/viewer/data` → secureUrl 목록 받아 jpg 다운로드
5. DB(`kakaopage_dl_item`)에 회차별 이력 기록 — 같은 회차 재다운로드 안 함

## 설정 (`설정` 메뉴)

| 항목 | 설명 |
|---|---|
| 체크할 작품 제목 | `A|B|C` 형태. 카카오페이지 표기 제목 그대로 |
| 카카오 쿠키 JSON | Cookie-Editor 확장으로 export한 JSON 그대로 |
| 다운로드 경로 | `{경로}/{작품}/{NNNN_회차}/{001.jpg ...}` |
| 1회 실행 최대 다운로드 화수 | 보통 1 (기다무 1장 단위) |
| 기다무만 사용 | On 권장. Off면 보유 이용권/캐시도 소비 가능 |

### 쿠키 주입

1. Chrome에 [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) 설치
2. `page.kakao.com` 카카오 로그인
3. Cookie-Editor 클릭 → Export → JSON → 복사
4. 설정 화면의 "카카오 쿠키" 텍스트박스에 붙여넣고 저장
5. "쿠키 검증" 버튼으로 유효 확인

쿠키는 약 30일 유지. 만료되면 다시 export.

## 카카오 BFF API 메모

| 엔드포인트 | 용도 |
|---|---|
| `GET v1/search/series?keyword=...` | 작품 검색 |
| `GET v1/content/series/simple?series_id=...` | 시리즈 간단 메타 |
| `GET v2/content/product/list?series_id=...&cursor_index=...` | 회차 목록 (last_view/purchase_info 마커 포함) |
| `GET v1/ticket/my?series_id=...&include_waitfree=true` | 보유 이용권 + 기다무 충전 |
| `GET v1/ticket/ready_to_use?product_id=...` | 회차별 사용 가능한 ticket_rental_type |
| `POST v1/ticket/use` `product_id=...&ticket_type=RT05` | 기다무 대여권 차감 → ticket_uid |
| `POST v5/inven/open_page` `seriesId,productId,transactionId,ticket_uid` | 열람 등록 (transactionId = _kpdid 쿠키) |
| `GET v1/viewer/data?series_id=...&product_id=...` | 이미지 secureUrl 목록 |
| `POST v1/viewer/last_page` | 진행상황 기록 (선택) |

응답의 `android_drm_type=WIDEVINE`은 모바일 앱 전용 메타. **웹 뷰어는 평문 JPEG**로 내려옴.
