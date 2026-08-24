# 재작도 회귀 사례

## projection-001 — 정팔각뿔을 대칭화해 원본 투영을 잃음

### 증상

- 팔각뿔의 꼭짓점 수와 모서리 수는 맞았지만, 뒤쪽 밑면 꼭짓점 세 개가 원본보다 왼쪽·위쪽에 놓였다.
- 결과가 정팔각뿔답게 균형 잡혀 보였으나 사진의 눌린 사투상도와 달랐다.
- 작은 SOURCE / REDRAW / OVERLAY 비교판에서는 전체 실루엣이 비슷해 보여 수동 검수를 통과했다.

### 발생 원인

1. 원본 크롭의 픽셀 꼭짓점을 먼저 기록하지 않고 SVG 좌표를 규칙적·대칭적으로 설계했다.
2. 문제의 수학적 위상 검산이 맞자 투영 검산도 맞다고 과신했다.
3. 기존 비교 도구가 원본과 재작도본을 각각 독립적으로 패널에 맞춘 뒤 합성해 좌표 오차를 수치화하지 않았다.

### 검수 누락 원인

- `validate_geometry.py`는 `projection_match: true`라는 수동 불리언만 확인했다.
- 비교 이미지가 현재 원본과 SVG에서 생성됐는지 해시나 최신성 검사가 없었다.
- 꼭짓점별 원본 좌표 근거와 오차 상한이 없었다.

### 영구 개선

- 모든 `trace` manifest에 `trace_evidence`를 강제한다.
- 원본 픽셀 랜드마크와 SVG 좌표의 최대 정규화 오차 절대 상한을 `0.04`로 제한한다.
- `compare_geometry.py`가 좌표 오차 보고서와 현재 원본·SVG·비교 이미지의 SHA-256을 기록한다.
- `validate_geometry.py`가 누락된 좌표, 과도한 허용치, 실패한 좌표 오차, 실제 크롭 크기 불일치, 오래된 비교 보고서를 거부한다.
- `selftest.py`에 비대칭 투영 승인 사례와 대칭화된 음성 사례를 추가한다.

### 재현 결과

- 잘못된 초안: 최대 정규화 오차 `0.1437` → 거부
- 수정본: 최대 정규화 오차 `0.0016`, 허용치 `0.0200` → 통과

수학적 위상이 맞아도 투영 오차 검사가 실패하면 승인하지 않는다.

## multigraph-001 — 같은 판독으로 만든 기대값이 간선 누락을 통과시킴

### 증상

- 네 꼭짓점 사이의 다중간선을 눈으로 한 번 세어 전체 9개로 판독했다.
- 실제로는 세 간선 다발에서 가닥 하나씩을 놓쳐 전체 12개였다.
- 잘못 읽은 다중도로 경로 수를 계산하고 같은 계산값을 `expect`로 사용해 검산이 통과했다.

### 발생·검수 누락 원인

1. 서로 붙은 가닥을 확대 화면에서 한 번만 세었다.
2. 예상 답이 재구성 바깥의 근거가 아니라 같은 간선 판독에서 계산되었다.
3. 꼭짓점 차수, 간선 다발 절단 수, 그린 SVG 가닥 수를 서로 대조하지 않았다.
4. 결과가 깔끔한 정수라는 사실을 구조 정확성으로 오인했다.

### 영구 개선

- 모든 실제 원본 manifest에 `verification_evidence`의 근거 종류를 강제하고 자기참조 종류를 거부한다.
- `imgprobe.py --deg`가 명시된 여러 반지름에서 원형 가닥 수를 측정하고 하나의 연속 안정값만 허용한다.
- `imgprobe.py --cut`이 명시된 여러 절단 길이에서 간선 다발 교차 수를 측정하고 하나의 연속 안정값만 허용한다.
- 프로브 JSON에 원본 SHA-256, 이미지 크기, 측정 좌표와 모든 표본을 기록한다.
- `validate_geometry.py`가 모든 꼭짓점 차수의 완전성, 간선 다중도, 차수 합 `2|E|`, 실제 SVG 요소 수, 프로브 최신성을 함께 검사한다.

### 재현 결과

- 과소계수 초안: 차수 `5/6/5/8`과 불일치 → 거부
- 수정 구조: 12간선, 계산 차수와 실측 차수 일치 → 통과
- 동일한 SVG 가닥 이름을 두 간선 다발에 재사용 → 거부
- 원본 해시가 바뀐 프로브 보고서 → 거부

## svg-bounds-001 — `<defs>` 패턴 타일을 실제 잉크로 계산함

### 증상

망점 면을 `<pattern>`으로 정의하자 패턴 타일의 원점 근처 rect·circle이 실제 그림 잉크로 계산되어 여백 부족 또는 잘림 오탐이 발생했다.

### 영구 개선

- `svg_fit()`은 잉크 좌표를 수집하기 전에 `<defs>...</defs>`를 제외한다.
- 실제로 그려진 `<rect>`의 경계는 별도로 계산한다.
- selftest에 pattern 정의는 무시하고 pattern을 사용하는 가시 rect만 계산하는 승인 사례를 둔다.

## trace-self-reference-001 — 같은 좌표를 관측값과 SVG 값에 복사함

### 증상

- 원본과 다른 도형인데 landmark 오차가 항상 0으로 보고되었다.
- manifest의 `points`를 배율만 바꾸어 `trace_evidence.points`에도 넣어 두 값이 서로를 검증했다.

### 영구 개선

- 현재 원본 해시·크기, 픽셀 점, 이름 있는 선분과 스타일을 담은 별도 observation JSON을 manifest 작성 전에 만든다.
- 검사기는 observation 해시와 원본 해시를 확인하고, 관측 점·선분 목록이 manifest로 변경 없이 전달됐는지 검증한다.

## trace-duplicate-edge-001 — 같은 모서리를 실선과 점선으로 중복 그림

### 증상

- 입체도형 한 모서리가 실선과 점선으로 동시에 선언되어 렌더 결과가 원본과 다른 굵은 선 또는 실선처럼 보였다.

### 영구 개선

- 모든 일반 선분의 무방향 끝점 쌍을 고유 키로 검사한다.
- 끝점이 같은 두 선분은 스타일이 같아도 중복으로 거부한다. 실제 평행 이중선은 별도 관측 꼭짓점을 사용해야 한다.

## trace-ink-001 — 꼭짓점만 맞고 중간 선과 실루엣이 원본에서 이탈함

### 증상

- 일부 꼭짓점 landmark는 맞았지만 연결선이 찌그러지거나 원본에 없는 선이 추가되었다.
- 여러 항목의 수동 판정을 하나의 `APPROVED = true` 값으로 채워 비교가 무력화되었다.

### 영구 개선

- 비교기는 재작도본을 원본 크기 좌표계로 정규화하고, 재작도 잉크 중 원본 잉크 허용 거리 안에 있는 비율을 보고·게이트한다.
- 수동 검사는 각 항목마다 별도 review JSON에 validator가 요구하는 모든 판정을 기록하고 원본·관측·SVG·comparison·diagnostic·보고서·landmark 확대판 해시를 고정한다.
- manifest의 불리언은 review JSON과 항목별로 같아야 하며 review가 없거나 오래되면 거부한다.

## trace-missing-edge-001 — 누락 잔차를 먼 꼭짓점까지 잘못 연장함

### 증상

- 실제 원본의 누락선은 아래 꼭짓점 `A(ㅂ)`에서 가운데 무라벨 꼭짓점 `I`로 가는 `A-I`였다.
- 잔차 검출기가 잡은 `A-I`의 아래쪽 조각을 방향만 보고 위 꼭짓점 `G(ㄱ)`까지 연장하여, 원본에 없는 `G-A(ㄱ-ㅂ)` 직선을 추가했다.
- 잘못된 `G-A`가 주변의 실제 꺾은선 `G-I-J-A`와 누락된 `A-I` 가까이에 놓여 전체 잉크 근접률과 넓은 잔차 허용 반경을 통과했다.

### 발생·검수 누락 원인

1. Hough 선분은 보이는 잔차 조각만 알려 주는데, 이를 완전한 선의 의미적 양 끝점으로 오해했다.
2. 원본에서 `A-I` 전체를 직접 추적하지 않고 각도가 비슷한 `G-A`로 외삽했다.
3. 전체 그림 평균 검사는 한 선분이 틀려도 나머지 많은 선이 맞으면 통과하며, 넓은 허용 반경은 가까운 꺾은선을 같은 선처럼 취급했다.
4. 수동 검토도 새로 추가한 선 하나를 독립적으로 원본 위에 좁게 대조하지 않았다.

### 영구 개선

- 잔차 조각은 후보 발견에만 쓰고, 후보 양 끝점 사이 전체 중심선이 native crop에서 지속적인 지지를 받을 때만 관측 선분으로 확정한다.
- `compare_geometry.py`는 선언된 각 실선·강조선을 좁은 반경으로 개별 표본화해 지지율과 최장 연속 미지지 구간을 보고한다.
- `validate_geometry.py`는 한 선분이라도 이 개별 지지 검사를 통과하지 못하면 전체 잉크 점수와 수동 검토가 참이어도 거부한다.
- `selftest.py`는 두 실제 변과 끝점만 공유하는 가짜 현을 거부하고, 실제 두 변은 승인하는 회귀 사례를 고정한다.

## print-readability-001 — 축소 조판에서 선과 꼭짓점 기호가 흐려짐

### 증상

- 화면에서는 보이던 일반선·점선이 문제지에 축소 배치되자 가늘어졌다.
- `ㄱ`, `ㄴ`, `A`, `B`, `C` 같은 꼭짓점 기호가 선과 비슷한 농도로 보여 빠르게 구분하기 어려웠다.

### 영구 개선

- 기본 일반선·점선과 기호 크기·굵기를 인쇄 가독성 기준으로 올리고, 라벨 뒤에는 얇은 흰색 halo를 둔다.
- manifest의 `render_style`로 혼잡한 개별 그림만 좁게 재정의할 수 있게 하되 허용 범위를 벗어난 값은 거부한다.
- `selftest.py`가 2배 실제 래스터에서 선의 연속 두께와 한글·라틴 라벨의 실제 잉크를 검사한다.

## inspection-throughput-001 — 고정 7배 확대가 큰 크롭에서 픽셀을 폭증시킴

### 증상

- 1080×840 크롭을 7배로 만들면 판독 보조본 6장이 약 2.67억 픽셀, 비교 시트가 별도로 약 2.68억 픽셀이 되어 저장·메모리·렌더 시간이 불필요하게 늘었다.
- 비교기에서 원본 이진화와 재작도 정규화를 게이트마다 반복했다.

### 영구 개선

- 판독 보조본은 긴 변 `2200px`을 목표로 적응적으로 크기를 정하고 자동 확대는 6배로 제한한다. native crop은 원래 해상도로 별도 보존한다.
- 비교 게이트는 원본 마스크·거리장과 정규화 재작도 마스크를 한 실행 안에서 공유한다.

## trace-routing-001 — 복합 문제를 `diagram`으로 우회해 좌표 검증이 사라짐

### 증상

- 하나의 문제에 입체도형 변형 과정과 다섯 개 선택지 전개도가 함께 있었다.
- 113개 좌표와 132개 선분을 사용했지만 manifest를 `type: diagram`으로 기록해 trace landmark와 선분별 원본 지지 검사가 적용되지 않았다.
- 전체 잉크 근접도는 통과했지만 상단 입체도형과 보기 일부의 배치가 원본과 달랐다.
- 수동 요약은 이미 구조·투영 불일치를 적었지만 그 결과가 납품 중단 게이트와 연결되지 않았다.

### 영구 개선

- 실제 원본을 참조하며 명시적 좌표·선분을 가진 `diagram`은 `validate_geometry.py`에서 거부한다.
- `compare_geometry.py`도 같은 우회를 독립적으로 거부해 비교기만 실행해도 자동 통과하지 못하게 한다.
- 단계·선택지·분리 도형이 함께 있는 문제는 구성 요소 목록을 먼저 고정하고, 각 요소와 전체 문제 비교를 모두 통과해야 완료로 본다.
- selftest에 실제 원본 좌표형 `diagram`의 manifest 거부와 비교 라우팅 거부를 독립 음성 사례로 추가한다.

## trace-oriented-strip-001 — 큰 국소 박스가 흐린 선과 주변 잡음을 함께 승인함

### 증상

- 흐린 실제 선을 살리려고 각 선분의 축정렬 bounding box에 국소 Otsu를 적용했다.
- 한 선분에서 완화된 임계값이 박스 전체와 다른 선분에 합쳐져, 평평한 음영·글자·가까운 다른 선이 후보 중심선을 대신할 수 있었다.
- 157개 선분을 가진 복합 문제에서 같은 원본의 팽창 필터와 SVG 래스터화를 반복하여 재검사 시간이 길어졌다.
- 확대본은 있었지만 native 좌표 눈금이 없어 표시 픽셀을 다시 원본 좌표로 환산하는 수작업이 필요했다.

### 영구 개선

- 후보 방향에 맞춘 좁은 native-pixel 띠만 읽고, 중심의 어두운 값이 좌우 평행 띠보다 지속적으로 어두운지 검사한다.
- 국소 Otsu는 후보 띠 안에서만 계산하고 다른 선분의 support mask에 합치지 않는다. selftest는 흐린 실제 선 승인과 평평한 음영 거부를 동시에 고정한다.
- 원본 유클리드 거리장은 실행당 한 번만 만들어 잉크 근접률과 선분 검사에서 공유한다.
- SVG·렌더러·잠금 파일·배율의 내용 해시로 래스터 캐시를 만들고, 보고서에 캐시 적중과 단계별 시간을 남긴다.
- `preprocess_geometry.py`가 crop-local native 좌표 격자를 자동 생성한다. 개별 판독본은 전체 해상도를 유지하고 overview sheet 패널만 긴 변 `1000px`로 제한한다.
- 21번 전체 비교에서 동일 조건 5회 중앙값이 `926.6ms`에서 `484.4ms`로 줄었고, 상단 4단계와 보기 5개 전체가 계속 통과했다.

## inventory-omission-001 — 연습문제만 만들고 개념탐구·예제를 빠뜨림

### 증상과 원인

남은 문제와 PDF 페이지는 내부적으로 정상이라 기존 페이지별 검사가 통과했지만 원본에 있던 개념탐구와 예제가 납품 집합에서 사라졌다. PDF를 만들기 전에 원본 전체 항목 집합을 고정하지 않았기 때문이다.

### 영구 개선

- `source-inventory.json`에 모든 쪽과 모든 항목의 ID·순서·종류·bbox·text/figure 유무를 고정한다.
- `validate_inventory.py`가 inventory/content/figure manifests/delivery의 exact ID set과 모든 원본 쪽 account를 검사한다.
- selftest는 2쪽 원본에서 example 하나를 제거하면 `SOURCE_PAGES_UNACCOUNTED`로 거부되는 사례를 둔다.

## crop-lineage-001 — 잘린 crop을 완전한 원본처럼 추적함

### 증상과 원인

모서리가 crop 경계에서 끊겼지만 이후 모든 좌표·SVG·비교가 그 잘린 crop에 대해 자기일치했다. crop이 어느 부모 이미지의 어느 픽셀 영역에서 왔는지와 경계를 통과하는 획을 기록하지 않았다.

### 영구 개선

- 전처리는 부모 해시·크기, crop box, crop 해시·pixel hash, crop context와 edge-stroke continuation을 기록한다.
- 새 lineage가 있으면 validator가 부모에서 다시 crop해 픽셀 일치를 확인한다.
- 경계로 이어지는 실제 획은 더 넓게 다시 자르는 것이 기본이며, 의도적인 페이지 경계만 별도 검토 사유로 허용한다.

## trace-source-roi-001 — 선언한 점들의 hull 밖 구성요소가 통째로 사라짐

### 증상과 원인

원본에는 분리된 두 번째 도형이 있었지만 observation·manifest·SVG에서 모두 빠졌다. 누락선 검사가 이미 선언한 trace point hull 안에서만 실행되어 빠진 구성요소를 보지 못했다.

### 영구 개선

- 원본을 먼저 본 관측자가 모든 구성요소를 포함하는 `source_geometry_roi`를 정한다.
- ROI를 trace point hull이나 SVG bbox에서 만들면 거부한다.
- 실제 원본 trace의 비교 ROI는 `trace_evidence.source_geometry_roi` 하나만 사용하고, 관측 JSON의 같은 필드와 정확히 같아야 한다. manifest 루트나 `source.figure_roi` 같은 별칭으로 더 작은 ROI를 넣어 비교기를 우회하면 거부한다.
- 비교기는 gate ROI와 별개로 full native crop residual 후보도 diagnostic에 `R#`로 표시한다.
- selftest는 hull 밖 두 번째 사각형을 빠뜨린 redraw가 full-crop 후보에 나타나고 source-first ROI에서는 실패하는 사례를 둔다.

## trace-topology-signature-001 — observation과 manifest가 같은 잘못을 공유함

### 증상과 원인

관측 파일과 manifest의 좌표를 그대로 복사해 landmark 오차는 0이었지만 짧은 내부선 두 개가 빠지거나 원본의 흰 틈을 가짜 선으로 연결했다. `required_segments`도 같은 부분집합이라 이름 존재 검사만 통과했다.

### 영구 개선

- 관측 overlay에서 원본의 각 면을 훑어 점·선·곡선·라벨·치수를 이름과 함께 실제로 본다.
- `required_segments == observation segments == manifest segments`와 곡선 complete set을 강제한다.
- `V/E/components/isolated/degree multiset/cycle rank/per-style counts` 위상 서명을 원본 관측과 manifest에서 각각 계산해 비교한다.

## vision-loop-001 — 비교 PNG는 만들었지만 실제로 열어 보지 않음

### 증상과 원인

전체 ink score는 좋았지만 원본과 다른 연결 관계와 비례가 남았다. 수치 보고서와 축소 contact sheet만 읽고 최신 native overlay의 접합점을 보지 않았다.

### 영구 개선

- `build_observation_overlay.py`가 원본/관측과 ROI 확대판을 만든다.
- `compare_geometry.py --diagnostic-out`이 등록된 색상 차이와 `R#` 누락선 후보를 만든다.
- `build_visual_review.py`가 전체 검수판과 꼭짓점 native 확대판, 해시된 pending review를 만든다.
- Geometry validator는 review의 observation overlay를 현재 source+observation으로 다시 렌더한 RGB 픽셀과 대조한다. 무관한 PNG와 그 자체 SHA·복제 report를 함께 제출해도 거부한다.
- vision-capable agent가 두 PNG를 실제로 열어 본 뒤에만 review 값을 기록한다.
- 합성 forward test에서 native 좌표 평균오차가 `5.978px → 1.546px`, 2px 선 지지율이 `0.600 → 0.962`로 개선되고, 일부러 빠뜨린 76px 내부선이 `R1`으로 검출된 사례를 고정한다.

## delivery-stale-pdf-001 — 마지막 도형 수정 뒤 예전 PDF 검수 해시를 재사용함

### 증상과 원인

도형 비교는 최신이었지만 delivery가 이전 PDF와 페이지 render를 가리켰다. 파일 존재와 수동 `true`만 확인해 서로 다른 build의 증거가 섞였다.

### 영구 개선

- delivery schema v2가 현재 PDF SHA, QA 보고서의 같은 PDF SHA, 페이지 render SHA·pixel SHA, 문항별 source/comparison/final render SHA를 재계산한다.
- 각 문항의 최종 visual review, QA page render의 half-open RGB pixel bbox, 그 bbox에서 무배율로 자른 실질적인 final problem crop을 필수로 묶고 `validate_visual_review(..., require_final_crop=True)`를 다시 실행한다. 페이지 어디엔가 존재하는 1×1 픽셀은 인정하지 않는다.
- `validate_workbook.py`는 입력 해시로 build ID를 만들고 변경된 단계 이후의 stale evidence를 재실행한다.
- 수정 작업은 `compare_pdf_scope.py`가 동일 렌더 환경에서 허용된 쪽/ROI 밖의 변경 픽셀을 거부한다.

## resume-import-cache-001 — 실행 스크립트가 import한 helper 변경을 캐시가 놓침

### 증상과 원인

stage command의 `worker.py` 해시는 같았지만 import된 `helper.py`의 값이 바뀌었다. 직접 명령행에 나온 파일만 execution identity에 포함되어 `--resume`이 예전 출력을 재사용했다.

### 영구 개선

- workbook job 시작 시 스킬 `scripts/`의 실행 코드 파일을 한 번만 내용 주소화하고 집계 digest를 모든 stage identity에 넣는다.
- 같은 job 안에서는 이 digest를 재사용해 반복 I/O를 피하되, 다음 job 실행에서는 다시 계산한다.
- 회귀 시험은 helper의 출력 `A→B` 변경 뒤 같은 command가 cache miss로 재실행되어 `B`를 만드는지 확인한다.

## resume-independent-items-001 — p1 수정이 독립 p2 캐시까지 무효화함

### 증상과 원인

모든 stage fingerprint에 전체 선행 stage chain을 넣어 p1 입력 하나가 바뀌면 p2가 아무 파일도 공유하지 않아도 다시 실행됐다. 정확성에는 문제가 없지만 “변경되지 않은 문항 재사용”과 대량 문제지 속도 목표를 잃었다.

### 영구 개선

- stage가 선언한 input/evidence 경로와 prior output의 교집합, 그리고 명시적 `depends_on`만 항목 의존성으로 넣는다.
- PDF·delivery·revision 같은 workbook-wide gate는 모든 문항 선행단계에 의존한다.
- 의존성을 전혀 선언하지 않은 stage는 안전하게 전체 선행 chain에 의존하는 기존 fail-closed 동작을 유지한다.
- 회귀 시험은 p1 변경 뒤 p1만 cache miss, 독립 p2는 hit, PDF·delivery는 miss인지 확인한다.

## portable-install-001 — GitHub 스킬 설치 뒤 첫 SVG 렌더가 실패함

### 증상과 원인

공식 skill-installer는 저장소 파일만 복사한다. 새 설치 폴더에는 `.venv`와 `node_modules`가 없었지만 예전 `run.py`는 전역 Python으로 조용히 실행되어, 환경에 따라 import 오류가 나거나 첫 resvg 렌더에서 중단됐다.

### 영구 개선

- 표준 라이브러리만 사용하는 `bootstrap.py`가 설치 폴더 안에 고정 버전 Python 환경과 로컬 resvg를 준비한다.
- `doctor.py`가 잠금 버전, 로컬 Node 모듈, 번들 한글 글꼴의 release hash를 검사한다.
- 런처는 스킬 로컬 환경이 없으면 production script를 전역 패키지로 실행하지 않고 bootstrap 명령을 안내한다.
- 릴리스는 저장소 밖의 새 설치 경로에서 bootstrap·doctor·전체 회귀 시험을 다시 실행한다.

## portable-layout-001 — 깨끗한 문제지가 작업 폴더의 임시 코드와 Windows 글꼴에 의존함

### 증상과 원인

한 문제의 결과는 보기 좋았지만 A4 배치 코드, 고정 해상도, 맑은 고딕 경로가 비공개 작업 폴더에만 있었다. 다른 컴퓨터의 공개 스킬만으로는 같은 조판을 만들 수 없었다.

### 영구 개선

- `build_worksheet.py`가 내용과 분리된 JSON으로 A4 PNG/PDF, 자동 페이지 분할, 풀이 공간을 재현한다.
- unmodified NanumGothic Regular/Bold와 OFL을 배포하고 실제 선택 글꼴·입력·그림·쪽 픽셀 해시를 보고한다.
- SVG 배치는 PyMuPDF가 아니라 bundled resvg를 사용해 짧은 점선이 실선으로 바뀌지 않게 한다.
- 자동 시험은 검수 이미지를 만들되 “눈으로 열었다”고 승인하지 않는다. 릴리스 담당 vision agent가 원본, overlay, diagnostic, landmark, 최종 문제지를 실제로 연다.

## portable-cache-001 — 글꼴을 바꿔도 예전 SVG 래스터가 재사용됨

### 증상과 원인

SVG 캐시 키가 SVG·렌더러·배율만 묶고 실제 픽셀 입력인 `MWR_FONT_FILES`와 글꼴 바이트를 묶지 않았다. 같은 SVG에서 글꼴 파일을 바꾼 뒤에도 이전 PNG가 cache hit로 반환될 수 있었다.

### 영구 개선

- 캐시 키에 구성 글꼴의 순서·정규화 경로·파일 바이트와 번들 글꼴 바이트를 포함한다.
- resvg는 시스템 글꼴을 암묵적으로 읽지 않고, 명시한 글꼴과 배포본 글꼴만 사용한다.
- 같은 경로의 글꼴 바이트 변경과 환경변수 제거가 모두 cache miss인지 회귀 시험한다.

## portable-layout-002 — 학생란 높이 누락과 출력 경로 충돌

### 증상과 원인

제목 없이 이름·날짜란만 있을 때 pagination 높이에는 학생란이 빠졌지만 실제 렌더에는 그려졌다. 또한 PNG/PDF/report가 spec·figure·font와 같은 경로여도 허용되어 입력과 lineage를 덮어쓸 수 있었다.

### 영구 개선

- 학생란을 독립적인 header 요소로 측정·검증하고 실제 그리기 높이와 일치시킨다.
- 모든 입력 경로와 모든 예정 출력 경로를 쓰기 전에 비교해 동일·중복 경로를 거부한다.
- 페이지 하단 경계와 figure/spec 불변 해시를 회귀 시험한다.

## portable-install-002 — 기존 설치 폴더의 junction이 외부 쓰기를 유도함

### 증상과 원인

기존 `scripts`, `.venv/Scripts`, `node_modules`가 외부 폴더를 가리키는 junction이면 복사·pip·npm이 스킬 밖을 수정할 수 있었다. 설치 폴더 안에서 설치기를 다시 실행하면 자기 파일을 지우는 경로도 있었다.

### 영구 개선

- 설치 대상 트리의 reparse point를 복사·삭제 전에 거부한다.
- bootstrap은 `.venv`와 `node_modules`에서 skill runtime 밖으로 나가는 링크를 설치 전후 모두 거부한다.
- in-place 실행은 파일 복사를 생략하고 선택한 dependency bootstrap만 수행한다.
- 외부 junction 쓰기 0건과 in-place 파일 보존을 Windows 회귀 시험으로 고정한다.
