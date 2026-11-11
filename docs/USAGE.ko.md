# dc_shell_sim v4 — 가상 리눅스 + Design Compiler / PrimeTime / Formality 학습 환경 (+ 실제 SKY130 라이브러리)

프로그램을 실행하면 **가상 리눅스 터미널**(`[user@dc-sim-host ~]$`)이 열립니다. 실제 회사/학교 서버처럼 리눅스 명령으로 폴더를 다루고, `dc_shell`을 입력해야 Design Compiler(모의)가 실행됩니다. `exit`를 입력하면 다시 리눅스로 돌아옵니다.

- Synopsys 제품이 아니며 실제 합성을 하지 않습니다. 메시지와 리포트 수치는 RTL 구조·제약·라이브러리·옵션에 따라 일관되게 바뀌도록 **가상으로** 만듭니다.
- Python 3.8 이상, 표준 라이브러리만 사용합니다.
- **Tab 자동완성과 줄 편집**을 지원합니다(Windows와 Linux 모두, 설치할 것 없음).
  - Tab: 명령, 파일·폴더 경로, `$변수` 완성. 후보가 여러 개면 Tab을 두 번 눌러 목록을 볼 수 있습니다.
  - dc_shell / pt_shell / fm_shell 안에서도 Tab이 동작합니다. 명령, `-옵션`, app 변수, 포트·모듈·라이브러리 셀 이름을 완성합니다.
  - ↑↓ 기록, ←→ Home End, Ctrl+A/E/U/K/W/L, Ctrl+R(기록 검색), Ctrl+C(취소), Ctrl+D(종료)
  - 끄려면 Windows 환경변수 `SIM_LINEEDIT=0`

## 실행

`run_dc_shell.bat`을 더블클릭하세요. 직접 실행할 때는 `python dc_shell_sim.py`입니다.

```bash
[user@dc-sim-host ~]$ cd ~/projects/demo
[user@dc-sim-host demo]$ dc_shell -f scripts/run_dc.tcl | tee dc.log
[user@dc-sim-host demo]$ ls reports outputs
[user@dc-sim-host demo]$ less reports/timing.rpt
[user@dc-sim-host demo]$ dc_shell            # 대화형
dc_shell> report_qor
dc_shell> exit
```

## 가상 파일 시스템

| 가상 경로 | 실제 위치 | 권한 |
|---|---|---|
| `/home/<사용자>` (`~`) | `dc_shell_sim\home` | 읽기/쓰기 (연습 공간) |
| `/tmp` | `dc_shell_sim\home\.tmp` | 읽기/쓰기 |
| `/mnt/c`, `/mnt/d` … | 실제 PC 드라이브 (WSL처럼) | **읽기 전용** |
| `/mnt/<이름>` | `mount` 명령으로 연결한 PC 폴더 | **읽기 전용** |
| `/tools/synopsys/...`, `/tools/lib/edu_lib/...` | 가상 설치 폴더 / 가상 셀 라이브러리 | 읽기 전용 |

실제 PC 파일은 절대 수정되지 않습니다. `rm`, `mv`, `>`, `write` 같은 명령으로 PC 폴더에 쓰려고 하면 `Read-only file system` 오류가 납니다. PC의 RTL은 그 자리에서 바로 `analyze`해도 되고, `cp`로 홈에 복사해서 연습해도 됩니다.

`home` 폴더는 처음 실행할 때 예제와 함께 자동으로 만들어집니다. 예제를 지웠다면 `restore_demo`로 다시 만들 수 있습니다.

## 실제 공개 셀 라이브러리 (SkyWater SKY130)

`libs/sky130_fd_sc_hd/`에 **SkyWater SKY130 `sky130_fd_sc_hd`**(Apache-2.0)의 tt / ss / ff 세 코너 liberty 파일이 들어 있습니다. 셀 428개이며, 가상 리눅스에서는 `/tools/lib/sky130_fd_sc_hd/{db,lib}`로 보입니다. 시뮬레이터가 `.lib`를 직접 읽어서 다음 값을 **실제 라이브러리 데이터**로 씁니다.

- 셀 이름과 핀 이름: `sky130_fd_sc_hd__nand2_1/Y`, `dfrtp_1/CLK`, `fa_1/SUM` 등이 리포트와 netlist에 그대로 나옵니다.
- 셀 면적: report_area, report_reference
- 지연·트랜지션: NLDM 테이블을 보간합니다 (clk→Q, setup/hold 포함).
- 누설 전력(`cell_leakage_power`)과 핀 용량 기반의 동적 전력
- 동작 조건: 전압, 온도, 코너 이름

```bash
cd ~/projects/sky130_demo
dc_shell -f scripts/run_dc.tcl | tee dc.log          # 10ns(100MHz) 합성
dc_shell -f scripts/run_corners.tcl | tee corners.log # tt/ss/ff 비교 (ss 에서 위반 발생)
grep -H "Critical Path Slack" reports/qor_*.rpt
```

dc_shell에서 쓸 수 있는 라이브러리 관련 명령:
- `list_libs`, `check_library`
- `report_lib sky130_fd_sc_hd__tt_025C_1v80 *dfrtp*`
- `get_lib_cells */*nand2*`, `get_attribute [get_lib_cells */sky130_fd_sc_hd__fa_1] function`
- `set_dont_use [get_lib_cells */sky130_fd_sc_hd__fa_*]`: FA가 다른 셀 조합으로 분해되면서 면적이 늘어나는 것을 확인할 수 있습니다.
- `set_driving_cell -lib_cell sky130_fd_sc_hd__buf_4`

다른 `.lib`(Nangate45, 학교 SAED 등)도 `libs/<이름>/lib/`에 넣거나 `target_library`에 `.lib` 경로를 직접 지정하면 됩니다(`libs/README.md` 참고). `newproj`는 기본으로 sky130을 쓰고, `-lib edu`를 붙이면 가상 edu_lib를 씁니다.

※ 셀 배치와 연결(netlist 구조)은 여전히 RTL 통계로 추정하므로, 경로 위의 셀 순서는 실제 합성 결과와 다릅니다. 셀 자체의 값(면적, 지연, 핀, 전력)은 라이브러리의 실제 데이터입니다.

## PrimeTime(`pt_shell`) / Formality(`fm_shell`) 모의 실행

실습 흐름을 **DC → PT → FM** 순서로 따라 할 수 있습니다. 출력 형식은 실제 실습 화면(`S-2021.06-SP5-2`)을 기준으로 맞췄습니다.

```bash
cd ~/projects/sky130_demo
dc_shell -f scripts/run_dc.tcl | tee dc.log     # netlist, sdc, svf 생성 (outputs/)
pt_shell -f scripts/run_pt.tcl | tee pt.log     # STA: check_timing, report_analysis_coverage ...
fm_shell -f scripts/run_fm.tcl | tee fm.log     # 등가성: match, verify
```

- **pt_shell**
  - 지원 명령: `set link_path`, `read_verilog <netlist>`, `link_design`, `read_sdc`, `check_timing`, `update_timing`, `report_analysis_coverage`, `report_global_timing`, `report_timing`, `report_constraint`
  - DC가 netlist에 넣어 둔 정보를 읽어서 디자인을 복원합니다.
  - 클럭이 너무 빠르면 setup·min_pulse_width 위반이 생기고, `set_false_path -from rst_n`이 있으면 recovery/removal 검사가 Untested로 나옵니다.
- **fm_shell**
  - 프롬프트가 `fm_shell (setup)>` → `(match)>` → `(verify)>`로 바뀝니다.
  - 지원 명령: `set synopsys_auto_setup true`, `set_svf`, `read_db -technology_library`, `read_verilog -r/-i`, `set_top r:/WORK/top`, `match`, `verify`, `report_failing_points`
  - 합성한 뒤 RTL을 수정하면 `Verification FAILED`가 나오고, 바뀐 모듈의 DFF가 failing point로 표시됩니다.
- 화면 출력에는 MobaXterm처럼 키워드에 색이 들어갑니다(Warning 노랑, Error/false 빨강, true/succeeded 초록, Information 하늘색, For/If 보라). 끄려면 `export SIM_HIGHLIGHT=0`.
- 리포트에 표시되는 툴 버전은 `S-2021.06-SP5-2`입니다. 바꾸려면 Windows 환경변수 `SIM_TOOL_VERSION`을 설정하세요.
- VCS(실제 Verilog 시뮬레이션)는 아직 지원하지 않습니다.

## 내 PC의 RTL로 연습하기

```bash
modfind /mnt/c/Users/user/PycharmProjects        # .v/.sv 모듈·포트·top 후보 찾기
newproj myproj                                    # ~/projects/myproj 생성
cp /mnt/c/.../rtl/*.v ~/projects/myproj/rtl/
cd ~/projects/myproj
modfind rtl                                       # top 후보 확인
vi scripts/run_dc.tcl                             # set TOP 수정 (메모장이 열림)
dc_shell -f scripts/run_dc.tcl | tee dc.log
```

자주 쓰는 폴더는 `mount C:\Users\user\Documents\rtl myrtl`처럼 연결하면 `/mnt/myrtl`로 바로 갈 수 있습니다(읽기 전용, 다음 실행 때도 유지). 실제 위치는 `winpath`, 탐색기로 열기는 `explorer`입니다.

## 리눅스 명령

| 분류 | 명령 |
|---|---|
| 파일·폴더 | `ls -lahRt`, `cd`, `pwd`, `mkdir -p`, `rmdir`, `rm -rf`, `cp -r`, `mv`, `touch`, `tree`, `du`, `df`, `file`, `realpath` |
| 파일 내용 | `cat -n`, `less`/`more`, `head`/`tail -n`, `grep -rnilvcwE`, `find -name -type -maxdepth`, `wc`, `sort`, `uniq`, `cut`, `diff`, `tee` |
| 편집 | `vi`/`vim`/`nano`/`gedit`/`code` → 메모장이나 VS Code가 열림 (`export EDITOR=code`로 변경 가능) |
| 셸·환경 | `echo`, `printf`, `export`, `setenv`, `env`, `alias`, `history`, `!!`, `!n`, `source`, `bash script.sh`, `which`, `type`, `man`, `module avail/load/list/unload`, `uname -a`, `whoami`, `id`, `ps`, `date`, `clear`, `exit` |
| 문법 | 파이프 `|`, 리다이렉트 `> >> < 2> 2>&1 &>`, `;` `&&` `||`, 와일드카드 `* ? [ ]`, `$변수` `$?`, `~`, 따옴표 |

`~/.bashrc`는 시작할 때 실행됩니다. 여기에는 `module load syn/SIM-2026.11`과 alias가 들어 있습니다. `module unload syn`을 하면 `dc_shell: command not found`가 나오는 것까지 실제 서버처럼 흉내 냈습니다.

## dc_shell 기능

- **시작**: `$SYNOPSYS/admin/setup` → `~` → 현재 폴더 순서로 `.synopsys_dc.setup`을 읽습니다. 현재 폴더에는 `command.log`를 만들고, compile을 하면 `default.svf`가 생깁니다.
- **실행 옵션**: `-f`, `-x`, `-topographical_mode`, `-no_home_init`, `-no_local_init`, `-output_log_file`, `-version`
- **라이브러리**: `/tools/lib/edu_lib/db/edu_lib_{tt,ss,ff}.db`를 `search_path`에서 실제로 찾습니다. 경로를 빼먹으면 `UID-3` 오류가 납니다. `cat /tools/lib/edu_lib/lib/edu_lib_tt.lib`로 liberty 형식도 볼 수 있습니다.
- **읽기**: `analyze`(`-autoread -recursive` 지원), `elaborate -parameters`, `read_verilog`, `read_file -autoread -recursive -top`, `define_design_lib WORK -path ./WORK`(WORK에 `.pvl/.syn/.mr` 생성), `report_design_lib`, `read_ddc`, `read_sdc`, `link`, `check_design`, `check_library`
- **제약**: `create_clock`, `create_generated_clock`, `set_clock_groups`, `set_clock_uncertainty/latency/transition`, `set_input/output_delay`, `remove_*`, `set_load`, `set_driving_cell`, `set_input_transition`, `set_max_delay/min_delay`, `set_false_path`, `set_multicycle_path`, `set_case_analysis`, `set_disable_timing`, `group_path`, `set_max_area/fanout/transition/capacitance`, `check_timing`, `reset_design`
- **합성**: `compile`(`-map_effort`), `compile_ultra`(`-gate_clock`, `-retime`, `-no_autoungroup`), `ungroup`, `set_ungroup`, `set_flatten`, `set_optimize_registers`, `set_dont_use`, `remove_attribute`, `insert_clock_gating`, `optimize_netlist -area`, `set_host_options`, `set_svf`
- **리포트**: `report_timing`(`-max_paths -from -to -delay_type min -nets -transition_time`), `report_area -hierarchy`, `report_power -hierarchy`, `report_qor`, `report_constraint -all_violators`, `report_resources`, `report_clock_gating`, `report_clock`, `report_port`, `report_net`, `report_design`, `report_hierarchy`, `report_reference`, `report_cell`, `report_case_analysis`, `report_path_group`, `report_timing_requirements`, `report_compile_options`, `report_app_var`
- **출력**: `change_names -rules verilog -hierarchy`, `write -format verilog|ddc`, `write_sdc`, `write_sdf`, `> 파일`, `redirect`
- **Tcl·collection**: `glob`, `sh <리눅스 명령>`, `ls`, `cd`, `pwd`, `getenv`/`setenv`, `$env(HOME)`, `get_attribute`/`set_attribute`, `filter_collection`, `sort_collection`, `index_collection`, `append_to_collection`, `get_pins`, `all_fanin`/`all_fanout`, `get_ports -filter`, `get_cells -filter`, `foreach_in_collection`, `proc`, `if`, `for`, `while`, `expr`
- **보호**: 기존 RTL(.v/.sv) 파일은 `write`로 덮어쓸 수 없습니다(`SIM-1`).

## 실습 아이디어

1. `create_clock -period 1.0`으로 줄여서 compile → VIOLATED 확인 → `compile_ultra`, `-retime`으로 비교하기
2. `set_dont_use [get_lib_cells edu_lib_tt/FADDX1]` 후 면적이 얼마나 늘어나는지 `report_reference`로 확인하기
3. `compile_ultra -gate_clock` 전후의 `report_power`, `report_clock_gating` 비교하기
4. `edu_lib_ss.db`(slow corner)로 바꿔서 setup slack 보기, `report_timing -delay_type min`으로 hold 보기
5. `write -format ddc` → `exit` → 다시 `dc_shell` → `read_ddc outputs/top.ddc`로 복원하기
6. `grep -n VIOLATED reports/*.rpt`, `grep -c Warning dc.log`처럼 로그를 분석하는 리눅스 습관 들이기

※ 예제 RTL과 Tcl은 최초 실행 시 `home/projects/`에 자동 생성됩니다.

