# 검증 기록

검증일: 2026-11-11 (Asia/Seoul)

Windows 환경의 프로젝트 Python 가상환경으로 실행했습니다. 추가 패키지는 설치하지 않았습니다. 아래 명령은 저장소 루트에서 실행할 수 있습니다.

```bash
python -m unittest discover -s tests -v
```

| 자동 검증 | 확인 내용 | 결과 |
|---|---|---|
| DC → PT → FM | 새 임시 폴더에서 데모 자동 생성, netlist/SDC 생성, PT와 FM 성공 메시지 | 통과 |
| 코너 비교 | tt/ss/ff 리포트 생성, ss 음수 slack, tt 양수 slack, ff > tt | 통과 |
| RTL 변경 탐지 | DC 실행 후 counter RTL 연산자를 바꾸고 FM 재실행 시 FAILED | 통과 |

`docs/demo-session.txt`는 별도로 실행한 데모 세션의 출력입니다. 사용자 이름과 로컬 경로는 일반 예시로 치환했습니다. 출력의 툴 배너, 실행 시간, 메모리 사용량 및 리포트는 시뮬레이터가 표시한 값이며 실제 상용 도구의 측정값이 아닙니다.

이 검증은 학습 시나리오의 동작을 확인합니다. 실제 EDA 결과의 정확성, 전체 Linux/Tcl/Verilog 호환성, 모든 명령의 회귀, Linux 실행 및 대화형 키 조작은 검증 범위에 포함하지 않았습니다.
