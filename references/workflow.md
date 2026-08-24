# 문제지 제작 워크플로

## 1. 산출물 구조

한 작업 폴더 안에서 다음을 분리한다.

```text
source/                 원본 사진, 수정 금지
work/crops/             문제·도형 크롭
work/inspection/        그레이·대비·이진화·native 좌표 격자 판독본
work/manifests/         문제별 판독 기록 JSON
work/figures/           SVG와 편집 가능한 원본
work/qa/                비교표, 검사 보고서, 렌더 페이지
output/                 사용자용 PDF
```

먼저 `source-inventory.json`에 원본 해시·페이지 수와 모든 개념탐구·예제·연습문제의 ID, 순서, 종류, 원본 쪽, 문제 bbox, text/figure 유무를 기록한다. `validate_inventory.py`가 원본의 모든 쪽과 최종 항목 집합을 확인하기 전에는 조판하지 않는다. 원본에서 알아낸 내용은 source observation 또는 manifest에 즉시 적어 긴 작업이나 대화 압축 이후에도 같은 이미지를 다시 추론하지 않도록 한다.

```json
{
  "schema_version": "1.0",
  "source": {"file": "source/workbook.pdf", "sha256": "...", "page_count": 1},
  "items": [
    {
      "id": "c1", "order": 1, "type": "concept_exploration",
      "source_page": 1, "problem_box": [40, 80, 1180, 930],
      "has_text": true, "has_figure": true
    }
  ],
  "page_accounts": []
}
```

PDF의 `problem_box`는 회전이 적용된 보이는 페이지의 왼쪽 위를 `(0, 0)`으로 하는 point 단위 좌표다. 선언한 `page_count`와 각 bbox는 `validate_inventory.py`가 실제 PDF에서 다시 읽어 대조한다. 항목이 없는 쪽은 `page_accounts`에 명시적으로 설명한다.

조판 전에는 delivery 없이 원본·전사·도형 집합을 먼저 고정한다.

```powershell
python <skill>/run.py validate_inventory.py source-inventory.json `
  --content content.json --manifest work/manifests
```

최종 delivery를 만든 뒤 같은 명령에 `--delivery work/qa/delivery.json`을 추가해 네 집합을 다시 정확히 대조한다.

## 2. 문제 manifest

문제마다 최소한 다음을 기록한다.

```json
{
  "id": "p03",
  "source": {"file": "source/page2.jpg", "problem_box": [0, 0, 100, 100], "figure_box": [0, 0, 50, 50]},
  "order": 3,
  "text": "검토가 끝난 정확한 문제 문장",
  "text_confidence": 1.0,
  "figure": {
    "kind": "prism",
    "renderer": "geometry_svg",
    "manifest": "work/manifests/p03-figure.json",
    "required_labels": ["ㄱ", "ㄴ"],
    "required_segments": ["ㄱㄴ"],
    "required_curves": [],
    "confidence": 1.0
  },
  "trace_evidence": {
    "method": "manual pixel landmark trace",
    "inspection_artifact": "work/inspection/p03/inspection-sheet.png",
    "observation_artifact": "work/inspection/p03/source-observation.json",
    "observation_sha256": "...",
    "source_size": [2400, 1800],
    "source_geometry_roi": [120, 80, 2280, 1710],
    "max_normalized_error": 0.025,
    "points": {"ㄱ": [810, 120], "ㄴ": [330, 1410]}
  },
  "private_evidence": {"handwritten_answer": null},
  "verification_evidence": {
    "kind": "independent-invariant",
    "description": "The observed face and fold counts are checked against the n-gonal prism identities.",
    "invariant": {"faces": "n+2", "folds": "n+1", "boundary": "4n-2"},
    "observed": {"faces": 8, "folds": 7, "boundary": 22}
  },
  "source_comparison": {
    "artifact": "work/qa/p03-source-redraw-overlay.png",
    "report": "work/qa/p03-source-redraw-overlay.json",
    "review": "work/qa/p03-source-redraw-review.json",
    "review_sha256": "...",
    "text_match": true,
    "topology_match": true,
    "line_styles_match": true,
    "labels_match": true,
    "dimensions_match": true,
    "projection_match": true,
    "inspected_latest_render": true
  },
  "qa": {"text_checked": false, "geometry_checked": false, "render_checked": false}
}
```

`private_evidence`는 검증에만 사용하고 학생용 PDF에 출력하지 않는다. `verification_evidence`에는 재구성한 값과 같은 판독에서 계산한 답을 넣지 않는다. 허용되는 `kind`는 `external-answer`, `independent-invariant`, `independent-measurement`, `user-confirmation`이다. 외부 답이나 사용자 확인을 근거로 쓰면 근거 산출물과 SHA-256도 기록한다.

`trace_evidence.source_geometry_roi`는 아래처럼 manifest보다 먼저 만든 observation JSON의 ROI를 값 변경 없이 복사한 것이다. ROI를 정한 방법은 trace가 아니라 observation에 기록해야 검사기가 독립 판독 출처를 확인할 수 있다.

```json
{
  "source_sha256": "...",
  "source_size": [2400, 1800],
  "source_geometry_roi": [120, 80, 2280, 1710],
  "source_geometry_roi_method": "manual source ink census before SVG construction",
  "points": {"ㄱ": [810, 120], "ㄴ": [330, 1410]},
  "segments": [
    {"name": "ㄱㄴ", "from": "ㄱ", "to": "ㄴ", "style": "solid"}
  ],
  "curves": [],
  "markers": [],
  "labels": [],
  "dimensions": []
}
```

## 3. 판독 순서

1. 사진 전체를 한 번 읽고 문제 순서와 대략적인 영역을 기록한다.
2. 문제 단위로 자르고, 도형이 있으면 도형만 별도로 자른다.
3. 도형 크롭의 그레이스케일, 대비 강화, 이진화, native 좌표 격자 버전을 읽기 좋은 크기로 만든다. `coordinate-grid.png`의 숫자는 크롭 내부 원본 좌표이며 확대 화면 좌표가 아니다. 확대 배율은 고정하지 않으며, 원본의 native pixel 간격과 획 굵기를 함께 기록한다. 개별 판독본은 충분한 해상도를 유지하되 전체 inspection sheet만 축소한다. 확대 뒤에도 측정값이 매개변수 변화에 따라 안정되지 않으면 정보 부족으로 판정하고 수치 근거와 함께 재촬영을 요청한다.
4. 꼭짓점 좌표와 모든 구성요소를 포함하는 `source_geometry_roi`를 별도 관측 JSON에 먼저 기록한다. 그 뒤 모든 선분·곡선·직각/눈금 marker·라벨·치수에 고유 이름과 끝점/anchor·실선/점선 역할을 기록하고 원본과 관측 파일의 해시를 고정한다. `build_observation_overlay.py`의 원본/중첩본과 ROI 확대본을 이미지 뷰어로 실제 확인한 다음에만 manifest/SVG를 만든다. SVG 좌표나 trace point hull에서 원본 좌표·ROI를 역산하면 안 된다.
5. 인쇄선과 손글씨가 섞였으면 획의 직선성·농도·균일성을 비교한다. 판정 근거가 약하면 최종 그림에서 임의로 삭제하지 않는다.
6. 문제 문장을 읽어 도형이 수행해야 하는 수학적 역할과 대조한다.

## 4. 도형별 라우팅

- 점·선·원·원호·교점·수선·영역: Geometry DSL로 의미 기반 작성.
- 각기둥·입체 사투상도: 원본 좌표 추적을 우선하고, 규칙적인 경우에만 파라미터 각기둥을 사용.
- 전개도: 면 목록으로 작성한 뒤 공유 변을 자동 계산하여 접는선과 외곽선을 결정.
- 쌓기나무·막대 격자: 격자 인덱스와 노드·막대 목록으로 작성.
- 다중간선 길찾기 그래프: 각 꼭짓점의 원형 차수 측정과 각 간선 다발의 중점 수직 절단 측정을 별도 JSON으로 남긴 뒤 `graph_evidence`로 연결.
- 특수 삽화: 원본 크롭 정제를 허용하되 손글씨 제거와 잘림 검사를 수행.

## 5. 사용자 확인 지점

최종 PDF 전에 도형만 모은 비교표를 만든다. 각 항목에는 원본 크롭, 새 그림, 겹침 이미지, 문제 번호만 둔다. `compare_geometry.py --manifest ... --report ...`로 원본 좌표 오차, 재작도 잉크의 원본 잉크 근접도, 방향 정렬 선분 지지, 파일 해시와 단계별 시간을 먼저 기록한다. 비교기는 내용 주소 SVG 래스터와 한 번 계산한 원본 거리장을 자동 재사용하지만 어떤 품질 게이트도 생략하지 않는다. 수치 게이트가 통과한 항목만 작업자가 수동 검사하고 항목별 review JSON을 만든 뒤 manifest의 `source_comparison`과 같은 값을 기록한다. 여러 항목에 하나의 승인 변수를 복사하지 않는다. 최종 PDF를 렌더한 뒤에도 원본 문제 크롭과 문제별로 다시 대조한다. 원본 자체가 정말 판독 불가능한 항목만 사용자에게 확인을 요청한다.

최종 대조는 content-addressed schema v2 `work/qa/delivery.json`에 남기고 납품 검사기를 통과시킨다.

```json
{
  "schema_version": 2,
  "status": "verified",
  "pdf": "../../output/worksheet.pdf",
  "pdf_sha256": "...",
  "pdf_qa_report": "pdf/qa-report.json",
  "source_inventory": {"path": "../../source-inventory.json", "sha256": "..."},
  "all_pages_inspected": true,
  "problems": [
    {
      "id": "p03",
      "status": "verified",
      "pdf_sha256": "...",
      "source_crop": "../crops/p03.png",
      "source_crop_sha256": "...",
      "comparison_artifact": "p03-source-final-overlay.png",
      "comparison_artifact_sha256": "...",
      "final_page_render": "pdf/page-02.png",
      "final_page_render_sha256": "...",
      "final_problem_crop": "p03-final-problem-crop.png",
      "final_problem_crop_sha256": "...",
      "final_problem_box": [160, 220, 2310, 3180],
      "visual_review": "p03-visual-review-final.json",
      "visual_review_sha256": "...",
      "final_page": 2,
      "text_match": true,
      "figure_match": true,
      "line_styles_match": true,
      "labels_dimensions_match": true,
      "no_handwriting": true,
      "no_clipping": true,
      "inspected_latest_pdf": true
    }
  ]
}
```

`python <skill>/run.py validate_delivery.py work/qa/delivery.json`이 실패하면 PDF는 아직 완성품이 아니다.

검증된 SVG/PNG를 A4 복습지로 조판할 때는 [worksheet-renderer.md](worksheet-renderer.md)의 JSON 형식과 `build_worksheet.py`를 사용할 수 있다. 렌더러는 도형을 새로 해석하지 않고 이미 승인된 그림만 배치하며, 번들 한글 글꼴·입력·그림·쪽별 픽셀의 SHA-256을 보고서에 남긴다. PNG를 실제로 열어 본 뒤 PDF를 요청했다면 `qa_pdf.py`의 페이지 렌더와 최종 문제 crop도 다시 연다.

`final_problem_box`는 현재 `qa_pdf.py`의 `final_page_render` RGB 픽셀 좌표에서 half-open `[x0, y0, x1, y1]`로 기록한다. `final_problem_crop`은 그 정확한 bbox를 무배율로 자른 픽셀과 같아야 하며, 1×1 흰 픽셀 같은 비실질적 crop은 거부된다. `visual_review`는 이 source crop·comparison·final crop을 해시로 묶고 `validate_visual_review.py --require-final-crop`을 통과한 JSON이어야 한다. Delivery 검사기는 이를 다시 계산하므로 예전 PDF의 검수판을 새 PDF에 재사용할 수 없다.

수정 작업은 baseline PDF와 허용 쪽/도형 ROI를 `compare_pdf_scope.py`에 주어 비대상 영역이 바뀌지 않았는지 확인한다. PDF QA와 범위 비교의 좌표 계약은 회전 `0`, `CropBox=MediaBox`, media origin `(0,0)`인 canonical page다. 다른 PDF geometry는 먼저 canonical PDF로 정규화하지 않으면 fail-closed로 거부된다.

`validate_workbook.py --resume`은 각 제작 단계의 입력·출력·실행 코드 해시를 기록하고 변경된 항목부터 다시 실행한다. 각 stage의 `inputs`/`evidence`가 실제 선행 output을 가리키게 하고, 파일 경로로 표현되지 않는 의존성만 `depends_on`에 선행 gate/name을 명시한다. 실행기 자체는 stage를 순서대로 진행한다. 서로 독립인 문제 제작은 에이전트나 상위 작업 관리자가 병렬화하고, 캐시 재사용 단위를 작게 유지하려면 한 workbook 전체를 한 command로 묶지 말고 문제별 preprocess/observe/render/compare/review stage로 선언한다. PDF·delivery·revision gate는 자동으로 workbook 전체 선행단계에 의존한다.

## 6. 반복 개선

사용자가 고친 도형은 실패 사례가 아니라 다음 실행의 시험 사례다.

- 원본 크롭
- 판독 manifest
- 승인된 SVG
- 오류 분류: 누락선, 위상, 점선, 라벨, 비례, 잘림, 조판

투영 불일치가 사용자 검수에서 발견되면 `projection` 회귀 사례로 분류하고, 승인 전·후 비교 이미지와 원본 픽셀 랜드마크를 함께 보관한다. 수학적 위상 검사가 맞았다는 이유로 투영 오류를 닫지 않는다.

이 네 가지를 회귀 자료로 남긴다. 같은 오류가 다시 나오면 프롬프트를 길게 늘리지 말고 생성기나 검사기에 규칙을 추가한다.
