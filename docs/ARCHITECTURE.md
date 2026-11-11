# 구현 구조

`dc_shell_sim.main()`은 `vterm.run_terminal()`을 호출합니다. `Terminal`은 홈과 데모 템플릿을 준비하고 가상 Linux 명령을 처리합니다. `dc_shell`, `pt_shell`, `fm_shell` 실행 시 같은 가상 파일 시스템을 해당 모의 도구에 전달합니다.

## 모듈 간 역할

`dc_shell_sim.py`의 `ShellCore`와 `DCExtra`를 결합한 `Shell`이 명령 실행·Tcl 스타일 처리·RTL 구조 분석·리포트 생성을 담당합니다. `eda_tools.make_classes()`는 이 셸을 기반으로 PT/FM 클래스를 만듭니다. DC netlist의 `SIMMETA` 정보를 PT가 읽어 모델을 복원하고, FM은 RTL 모듈 본문의 해시 등으로 변경 여부를 비교합니다. SAT/SMT 기반 등가성 증명은 수행하지 않습니다.

`libparse.py`는 Liberty를 그룹 구조로 읽고 셀·핀·타이밍 아크와 테이블을 구성합니다. 셀의 Boolean function을 진리표로 평가해 내부 셀 역할에 대응시키고, NLDM 조회 시 테이블 축에 맞춰 보간합니다. 파싱 결과는 실행 시 캐시할 수 있습니다.

`lineedit.py`는 터미널 입력과 완성을 담당합니다. UI 입력, 가상 파일 시스템, EDA 계산은 파일로 구분되어 있지만 EDA 엔진과 셸 내부에는 여러 책임이 함께 남아 있습니다.

## 파일과 상태

- `home/`: 최초 실행 때 자동 생성되는 쓰기 가능한 실습 공간. Git에서는 제외합니다.
- `/mnt/c` 등: 호스트 드라이브에 대한 가상 읽기 전용 경로.
- `/tools/lib`: 라이브러리의 가상 경로. `.db`는 모의 경로이며 실제 입력 데이터는 `.lib`입니다.
- `sim_mounts.json`: 사용자 로컬 마운트 설정. Git에서는 제외합니다.

이 구조는 실습 중 외부 파일에 쓰는 실수를 줄이기 위한 것입니다. 운영체제 수준의 보안 격리나 임의의 외부 입력에 대한 안전성을 보장하지 않습니다.
