# Codex 시각 판독·좌표 추적 루프

## 목적

이미지 분석 수치만 통과시키는 것이 아니라 Codex가 원본과 결과 이미지를 실제로 열어 보고, 원본의 2차원 투영을 native 좌표로 옮기고, 어긋난 지점을 다시 고치는 제작 루프다. 자동 검사는 눈을 대신하지 않는다. 눈이 어디를 다시 봐야 하는지 표시하고 반복 시간을 줄인다.

## 네 번 실제로 보기

실원본 도형마다 다음 이미지를 이미지 뷰어로 직접 연다. 파일 존재나 JSON 보고서만 확인해서는 안 된다.

1. 원본 전체 문제와 native 도형 crop
2. 원본 위에 점·선·곡선 이름을 표시한 source-observation overlay
3. 원본·재작도·등록 overlay·색상 차이·누락선 후보를 담은 geometry diagnostic
4. 최종 PDF에서 다시 자른 문제 영역과 원본 문제 영역

가능하면 `detail=original`로 열고, 전체 도형과 꼭짓점 확대판을 둘 다 본다. 낮은 해상도 대화 첨부 화면이 아니라 현재 해시의 native crop을 좌표 근거로 사용한다.

## 빠른 제작 순서

### 1. 원본을 먼저 본다

- 페이지 전체에서 문항 경계, 도형 구성요소 수, 라벨·치수 위치를 확인한다.
- crop 밖으로 이어지는 획이 없는지 본다.
- 입체를 새 3D 모델로 해석하지 말고 인쇄된 평면 선화로 읽는다.
- 외곽을 한 바퀴 돈 뒤 각 면을 같은 방향으로 훑어 내부선·대각선·숨은선을 센다.

### 2. source observation을 만든다

- 꼭짓점은 native crop 좌표로 공유 이름 하나만 둔다.
- 선은 각각 고유 이름, 두 끝점, `solid`/`dashed`/`guide`/`emphasis` 역할을 둔다.
- 곡선은 시작·끝과 중간 관측점 3개 이상, 보통 총 5–8점을 둔다.
- 직각표시·눈금·짧은 ㄴ자 표시는 구조 선분에 섞지 않고 이름 있는 `right-angle`/`tick`/`short-polyline` marker로 관측한다.
- 라벨과 치수도 원본의 anchor와 bbox를 기록한다.
- 관측자가 원본에서 정한 `source_geometry_roi`는 모든 분리 구성요소를 포함해야 하며 manifest 점들의 hull에서 역산하지 않는다.

```powershell
python <skill>/run.py build_observation_overlay.py source.png observation.json `
  --manifest figure.json --out observation-review
```

원본과 overlay를 실제로 열고 다음을 확인한다.

- 모든 꼭짓점 표식이 획의 실제 접합 중심에 있는가.
- 원본 선 하나당 이름 있는 선 하나가 있는가.
- 가까이 지나갈 뿐인 선을 잘못 연결하지 않았는가.
- 짧은 선, 점선, 분리된 선택지나 변형 단계가 빠지지 않았는가.

`observation-overlay.png`와 같은 폴더의 `observation-overlay.json`을 함께 보존한다. Geometry validation은 그 report의 source/observation 해시를 재검산하고 현재 두 파일로 overlay를 다시 렌더해 RGB 픽셀이 같은지 확인하므로, 다른 그림이나 오래된 overlay를 자체 SHA만 바꾸어 재사용할 수 없다.

### 3. SVG를 만들고 source에 등록한다

관측 좌표를 canvas에 정규화해 SVG를 만들되 대칭화·정다각형화하지 않는다. 비교판은 source와 redraw를 각자 보기 좋게 맞추지 않고 동일한 정규화 프레임에 등록해야 한다.

```powershell
python <skill>/run.py geometry_svg.py figure.json figure.svg
python <skill>/run.py compare_geometry.py source.png figure.svg `
  --manifest figure.json --out comparison.png --diagnostic-out diagnostic.png `
  --report comparison.json
```

`diagnostic.png`의 색상 뜻:

- 초록: 원본과 재작도가 겹친 잉크
- 빨강: 원본에만 남은 잉크. 빠진 선·곡선·구성요소 후보
- 파랑: 재작도에만 있는 잉크. 잘못 추가했거나 좌표가 이탈한 후보
- 빨간 `R#`: 원본 전체 ROI에서 찾은 설명되지 않은 긴 선 후보

수치는 후보를 좁히는 도구다. 빨간 부분이 라벨인지 구조선인지, 파란 부분이 halo인지 잘못된 모서리인지는 원본을 보고 판정한다.

### 4. 공유 꼭짓점을 native ink로 미세보정한다

위상과 선 역할을 먼저 확정한 뒤에만 작은 반경 안에서 좌표 후보를 만든다. 자동 결과를 최종 관측으로 바로 승격하지 않는다.

```powershell
python <skill>/run.py refine_trace.py source.png figure.json `
  --observation source-observation.json `
  --out-observation source-observation-candidate.json `
  --overlay refinement-overlay.png --report refinement.json `
  --radius 6 --passes 3
```

- 빨강은 보정 전, 청록·초록은 보정 후다.
- 한 꼭짓점이 움직이면 연결된 모든 선이 함께 움직여야 한다.
- 라벨, 망점 경계, 교차하는 다른 선에 끌린 점은 되돌린다.
- 곡선은 관측점 확대판에서 각 점이 실제 곡선 중심을 따르는지 별도로 본다.

### 5. Codex용 고해상도 검수판을 만든다

```powershell
python <skill>/run.py build_visual_review.py --id p03 `
  --source source.png --observation source-observation.json `
  --observation-overlay observation-review/observation-overlay.png --redraw figure.svg `
  --comparison comparison.png --diagnostic diagnostic.png `
  --comparison-report comparison.json `
  --out visual-review.png --landmarks-out landmark-zooms.png `
  --review-template visual-review.json
```

Codex는 `visual-review.png`와 `landmark-zooms.png`를 실제로 연다. `visual-review.json`의 `null` 값을 수치 보고서에서 복사하지 않는다. 이미지에서 확인한 결과만 `true`/`false`로 기록하고, `false`이면 점·선·곡선 이름과 수정 내용을 적는다.

이미지를 연 뒤 reviewer·status와 모든 필수 check를 채우고 다음 순서로 검사한다. `build_visual_review.py`가 처음 만드는 pending JSON을 그대로 `validate_geometry.py`에 넣으면 의도적으로 실패한다.

```powershell
python <skill>/run.py validate_visual_review.py visual-review.json `
  --report visual-review-gate.json
# visual-review.json의 현재 SHA-256을 manifest source_comparison.review_sha256에 기록한다.
python <skill>/run.py validate_geometry.py --manifest figure.json --svg figure.svg
```

PDF 조판 뒤에는 `--final-crop final-p03.png --overwrite`로 별도의 최종 review 산출물을 다시 만들고, 원본과 PDF crop을 실제로 연다. `final_pdf_scale_and_spacing_are_suitable`까지 판정한 뒤 `validate_visual_review.py --require-final-crop`을 통과시키고, 갱신된 review 해시로 geometry validation도 다시 실행한다. 이렇게 해야 SVG 단계의 올바른 그림이 PDF에서 축소·잘림·간격 오류로 바뀐 경우를 잡을 수 있다.

## 반복 속도

- 원본 전처리, distance field, SVG raster는 내용 해시가 같을 때만 재사용한다.
- 전체를 다시 추론하지 말고 diagnostic에서 표시한 점·선만 native crop에서 다시 읽는다.
- 수정 뒤에는 해당 구성요소 비교를 먼저 실행하고, 통과하면 whole-problem 비교와 최종 PDF 문제 crop을 다시 본다.
- 좌표가 바뀌면 comparison과 visual review는 자동으로 오래된 증거가 된다. 다른 문항의 해시가 같으면 다시 만들지 않는다.

## 멈춰야 하는 경우

확대·대비·이진화와 원본 보기를 반복해도 한 선을 두 방식으로 읽을 수 있거나 crop 밖으로 이어지는 획이 있으면 추정하지 않는다. 더 넓고 선명한 원본을 요청한다. 멈추는 목적은 실패 선언이 아니라, 근거 없는 선을 그려 나중에 전체 위상을 다시 만드는 시간을 피하는 것이다.
