# 문제지 제작 워크플로

## 1. 산출물 구조

한 작업 폴더 안에서 다음을 분리한다.

```text
source/                 원본 사진, 수정 금지
work/crops/             문제·도형 크롭
work/inspection/        그레이·대비·이진화 확대본
work/manifests/         문제별 판독 기록 JSON
work/figures/           SVG와 편집 가능한 원본
work/qa/                비교표, 검사 보고서, 렌더 페이지
output/                 사용자용 PDF
```

원본에서 알아낸 내용은 즉시 manifest에 적는다. 긴 작업이나 대화 압축 이후에도 같은 이미지를 다시 추론하지 않도록 한다.

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
    "confidence": 1.0
  },
  "trace_evidence": {
    "method": "manual pixel landmark trace",
    "inspection_artifact": "work/inspection/p03/inspection-sheet.png",
    "source_size": [2400, 1800],
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

## 3. 판독 순서

1. 사진 전체를 한 번 읽고 문제 순서와 대략적인 영역을 기록한다.
2. 문제 단위로 자르고, 도형이 있으면 도형만 별도로 자른다.
3. 도형 크롭의 그레이스케일, 대비 강화, 이진화 버전을 읽기 좋은 크기로 만든다. 확대 배율은 고정하지 않으며, 원본의 native pixel 간격과 획 굵기를 함께 기록한다. 확대 뒤에도 측정값이 매개변수 변화에 따라 안정되지 않으면 정보 부족으로 판정하고 수치 근거와 함께 재촬영을 요청한다.
4. 꼭짓점 좌표를 도형 크롭의 픽셀 좌표로 먼저 기록한다. 그 뒤 선분 존재 여부, 점선 주기, 글자 위치와 치수 보조선을 기록한다. SVG 좌표에서 원본 좌표를 역산하면 안 된다.
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

최종 PDF 전에 도형만 모은 비교표를 만든다. 각 항목에는 원본 크롭, 새 그림, 겹침 이미지, 문제 번호만 둔다. `compare_geometry.py --manifest ... --report ...`로 원본 좌표와 SVG 좌표의 정규화 오차 및 파일 해시를 먼저 기록한다. 수치 게이트가 통과한 항목만 작업자가 수동 검사하고 manifest의 `source_comparison`을 채운다. 최종 PDF를 렌더한 뒤에도 원본 문제 크롭과 문제별로 다시 대조한다. 원본 자체가 정말 판독 불가능한 항목만 사용자에게 확인을 요청한다.

최종 대조는 `work/qa/delivery.json`에 남기고 납품 검사기를 통과시킨다.

```json
{
  "pdf": "../../output/worksheet.pdf",
  "pdf_qa_report": "pdf/qa-report.json",
  "all_pages_inspected": true,
  "problems": [
    {
      "id": "p03",
      "source_crop": "../crops/p03.png",
      "comparison_artifact": "p03-source-final-overlay.png",
      "final_page_render": "pdf/page-02.png",
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

`python <skill>/scripts/validate_delivery.py work/qa/delivery.json`이 실패하면 PDF는 아직 완성품이 아니다.

## 6. 반복 개선

사용자가 고친 도형은 실패 사례가 아니라 다음 실행의 시험 사례다.

- 원본 크롭
- 판독 manifest
- 승인된 SVG
- 오류 분류: 누락선, 위상, 점선, 라벨, 비례, 잘림, 조판

투영 불일치가 사용자 검수에서 발견되면 `projection` 회귀 사례로 분류하고, 승인 전·후 비교 이미지와 원본 픽셀 랜드마크를 함께 보관한다. 수학적 위상 검사가 맞았다는 이유로 투영 오류를 닫지 않는다.

이 네 가지를 회귀 자료로 남긴다. 같은 오류가 다시 나오면 프롬프트를 길게 늘리지 말고 생성기나 검사기에 규칙을 추가한다.
