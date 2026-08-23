# 도형 재작도 규칙

## 목표의 우선순위

1. 문제와 연결 구조가 정확하다.
2. 원본의 2차원 투영, 실선·점선, 내부선, 라벨과 치수의 역할이 같다.
3. 원본과 비례와 배치가 충분히 비슷하다.
4. 인쇄 품질이 깨끗하다.

정규 각기둥처럼 예쁘게 보이도록 바꿨더라도 원본과 선의 연결이 달라졌다면 실패다.

## 좌표 추적

- 도형 크롭의 왼쪽 위를 `(0, 0)`으로 두고 모든 중요한 꼭짓점을 픽셀 좌표로 찍는다.
- 이 원본 픽셀 좌표를 manifest의 `trace_evidence.source_size`와 `trace_evidence.points`에 먼저 기록한다. SVG 좌표를 먼저 만든 뒤 원본 좌표를 역으로 맞추는 것은 금지한다.
- SVG 좌표는 원본 픽셀 좌표를 일정 비율로 정규화해 만든다. 원본이 비대칭이거나 눌려 보이면 그 투영을 그대로 보존하며, 정다각형답게 보이도록 대칭화하거나 등간격화하지 않는다.
- 한 점을 고치면 연결된 선이 함께 움직이도록 점 이름을 공유한다.
- 곡선은 시작점·끝점과 중간 관측점 3개 이상을 기록한다. 자연스러운 곡선은 5개 안팎의 점을 `smooth path`로 잇거나 2차·3차 베지어로 근사한다.
- 선 하나가 가려져 보이지 않는지, 실제로 없는지 문제 문장과 인접 면을 이용해 확인한다.

### Trace evidence 하드 게이트

실제 원본을 사용하는 모든 `trace` manifest에는 다음을 둔다.

```json
{
  "trace_evidence": {
    "method": "manual pixel landmark trace",
    "inspection_artifact": "../inspection/p03/inspection-sheet.png",
    "source_size": [2400, 1800],
    "max_normalized_error": 0.025,
    "points": {"A": [810, 120], "B": [330, 1410]}
  }
}
```

- `source_size`는 비교에 실제 사용한 크롭 파일의 픽셀 크기와 같아야 한다.
- 모든 명시적 SVG 점에 원본 픽셀 좌표가 있어야 한다.
- 원본과 SVG 좌표를 각 프레임의 너비·높이로 정규화한 뒤 점별 거리를 계산한다.
- 최대 정규화 오차는 `0.04`를 넘을 수 없고, 일반적인 목표는 `0.02`–`0.03`이다.
- 임계값을 키워 실패를 통과시키지 않는다. 오차가 크면 원본 좌표 판독 또는 SVG 투영으로 돌아간다.

## 다중간선 그래프

붙어 있는 가닥을 확대 화면에서 한 번 세는 것으로 다중도를 확정하지 않는다. 꼭짓점 주변 원형 측정과 간선 다발 중점의 수직 절단 측정을 서로 다른 증거로 남긴다.

```powershell
python <skill>/scripts/imgprobe.py graph-crop.png --deg 120 85 `
  --radii 14 18 22 26 30 --stable-samples 3 --report K-degree.json
python <skill>/scripts/imgprobe.py graph-crop.png --cut 120 85 210 150 `
  --half-lengths 8 12 16 20 24 --stable-samples 3 --report K-N-cut.json
```

위 수치는 예시일 뿐이다. 실제 반지름과 절단 길이는 원본의 native pixel 크기와 다른 선·라벨이 끼어드는 위치를 보고 명시한다. 하나의 개수가 필요한 연속 표본 수만큼 유지되지 않거나 서로 다른 안정값이 둘 이상 나오면 프로브는 실패한다.

manifest에는 다음처럼 현재 원본과 실제 SVG 가닥을 연결한다.

```json
{
  "type": "graph",
  "verification_evidence": {
    "kind": "independent-measurement",
    "description": "Ring degrees and midpoint cuts independently constrain edge multiplicities."
  },
  "graph_evidence": {
    "method": "ring degree plus midpoint cut",
    "inspection_artifact": "../inspection/graph-sheet.png",
    "source_size": [1200, 900],
    "source_sha256": "...",
    "inspection_sha256": "...",
    "vertices": {
      "K": {"degree": 5, "probe_report": "../inspection/K-degree.json"},
      "N": {"degree": 6, "probe_report": "../inspection/N-degree.json"}
    },
    "edge_groups": [
      {
        "vertices": ["K", "N"],
        "multiplicity": 3,
        "drawn_elements": ["KN-1", "KN-2", "KN-3"],
        "probe_report": "../inspection/K-N-cut.json"
      }
    ]
  }
}
```

- 모든 그래프 꼭짓점에 실측 차수와 통과한 degree 프로브 보고서가 있어야 한다.
- 모든 간선 다발에 실측 다중도, 같은 수의 `drawn_elements`, 통과한 cut 프로브 보고서가 있어야 한다.
- 무방향 꼭짓점 쌍은 한 번만 선언하고 루프는 허용하지 않는다.
- 간선에서 계산한 차수와 실측 차수, 차수 합 `2|E|`, 실제 SVG 요소 이름이 모두 맞아야 한다.
- 프로브 원본 해시, 이미지 크기, 검사 이미지 해시가 현재 파일과 다르면 오래된 증거로 거부한다.
- 프로브가 적어 둔 `ok`나 `stable_count`를 신뢰하지 않고 원시 `counts`와 `minimum_samples`에서 안정값을 다시 계산한다.

## 파라미터 각기둥

규칙적인 정n각기둥에는 `geometry_svg.prism()`을 쓸 수 있다.

- 윗면의 n개 꼭짓점을 만들고 같은 x 좌표에서 높이만큼 이동해 아랫면을 만든다.
- 면 방향을 이용해 보이는 옆면과 숨은 옆면을 계산한다.
- 보이는 모서리는 실선, 숨은 모서리는 점선으로 만든다.
- 원본이 숨은선을 생략했다면 `hidden=false`로 맞춘다.
- 문제에만 있는 내선, 경로, 대각선은 생성된 꼭짓점이나 선분의 보간점에 고정한다.

파라미터 투영이 원본의 찌그러짐이나 특수 시점을 재현하지 못하면, 원본의 각 꼭짓점을 직접 좌표로 적은 `trace` 모드를 사용한다.

## 전개도

전개도는 선을 먼저 그리지 말고 면을 먼저 정의한다.

1. 각 면을 꼭짓점 순서가 일정한 다각형으로 기록한다.
2. 반올림 허용오차 안에서 같은 두 끝점을 가진 변을 공유 변으로 센다.
3. 정확히 두 면이 공유하는 변은 접는선 점선이다.
4. 한 면만 가진 변은 외곽 실선이다.
5. 세 면 이상이 같은 변을 공유하면 겹침 오류로 중단한다.

n각기둥 전개도의 기본 불변식:

- 면 `n + 2`
- 접는선 `n + 1` — 통상적인 트리형 전개도
- 외곽 변 `4n - 2`
- 모든 면이 접는선 그래프로 한 덩어리

문제에서 일부 선을 학생이 그려야 하는 경우, 인쇄된 선과 학생 답을 먼저 구분하고 인쇄된 구조만 재현한다.

## 선과 라벨

- `solid`: 외곽, 보이는 모서리, 인쇄된 일반 선
- `dashed`: 숨은 모서리와 접는선
- `guide`: 치수 보조선과 안내 호
- `emphasis`: 문제에서 지정한 경로·대각선

라벨은 자유 좌표가 아니라 `vertex`, `edge midpoint`, `face centroid`, `route point` 중 하나에 오프셋을 둬 배치한다. 치수 텍스트도 측정 대상 선분과 연결된 보조선을 가져야 한다.

원본 면이 망점이면 인쇄·축소 후에도 보이는 SVG `<pattern>`으로 재현한다. 원본이 단색이면 망점을 추가하지 않는다. `<defs>` 안의 pattern·clipPath·marker 좌표는 선언일 뿐 실제 잉크 위치가 아니므로 잘림 계산에서 제외한다.

## 해상도 한계

화면 확대는 기존 픽셀을 크게 보일 뿐 새 구조 정보를 만들지 않는다. 선 간격, 격자 간격, 획 굵기, 측정값이 안정되지 않은 매개변수 범위를 native pixel 단위로 기록한다. 서로 다른 대비·임계값·반지름·절단 길이에서 구조가 확정되지 않으면 임의로 고르지 말고 숫자 근거와 함께 더 높은 해상도의 원본을 요청한다.

## 의미 검산

완성 그림을 이용해 문제를 직접 풀어 보되, 그 결과는 독립 근거가 있을 때만 기대값 게이트가 된다.

- 답이 흔한 교재 수치로 깔끔하게 떨어지는지는 개연성 신호로만 사용한다.
- 채점·해설·독립 계산으로 신뢰성이 확인된 외부 답과 일치하는가.
- 문제에서 언급한 모든 기호가 그림에 존재하는가.
- 경로 길이, 면 개수, 모서리 수를 그림의 실제 구조에서 계산할 수 있는가.

검산이 실패하면 숫자나 원본이 이상하다고 단정하지 말고 도형 판독으로 돌아간다.
