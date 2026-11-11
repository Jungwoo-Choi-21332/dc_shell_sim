# -*- coding: utf-8 -*-
"""
vterm.py  -  dc_shell_sim 의 가상 리눅스 터미널 (bash 흉내)

  * /home/<user>  -> dc_shell_sim/home 폴더 (읽기/쓰기 가능한 연습 공간)
  * /tmp          -> dc_shell_sim/home/.tmp (읽기/쓰기)
  * /mnt/c, /mnt/d ... -> 실제 PC 드라이브 (읽기 전용!  실제 파일은 절대 수정되지 않음)
  * /tools/...    -> 가상 Synopsys 설치 폴더 / 가상 셀 라이브러리 (읽기 전용)

  지원: 파이프(|), 리다이렉트(> >> < 2> 2>&1), ;  &&  ||, 와일드카드(* ? []),
        $변수, ~, alias, history(!!, !n), 탭 자동완성(readline 있을 때)
"""
import os
import re
import sys
import json
import time
import shutil
import fnmatch
import difflib
import datetime
import subprocess
import getpass
from collections import OrderedDict

import lineedit

HOSTNAME = "dc-sim-host"
SYN_VER = "SIM-2026.11"
SYN_ROOT = "/tools/synopsys/syn/" + SYN_VER
SYN_BIN = SYN_ROOT + "/bin"
PT_BIN = "/tools/synopsys/prime/" + SYN_VER + "/bin"
FM_BIN = "/tools/synopsys/fm/" + SYN_VER + "/bin"
LIB_ROOT = "/tools/lib/edu_lib"
SIM_BIN = "/usr/local/bin"
MARKER = "dc_shell_sim"
IS_WIN = os.name == "nt"
HDL_EXT = (".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl", ".vg")
USE_COLOR = True


class FSError(Exception):
    pass


class TermExit(Exception):
    pass


class ParseError(Exception):
    pass


# --------------------------------------------------------------------------
# 출력 대상
# --------------------------------------------------------------------------
HIGHLIGHT = True
_HL_RE = re.compile(
    r"(?P<r>does not exist|Not Allowed|\b(?:Error|ERROR|error|errors|Errors|false|False|FALSE|"
    r"failed|Failed|FAILED|FAIL|VIOLATED|Fatal|FATAL|Not)\b)"
    r"|(?P<y>\$\w+(?:\(\))?|not found|\b(?:Warning|WARNING|warning|warnings|Warnings|ATTENTION|disabled|Disabled)\b)"
    r"|(?P<g>\b(?:true|True|TRUE|succeeded|SUCCEEDED|successfully|enabled|Enabled|passed|PASSED|MET)\b)"
    r"|(?P<c>\b(?:Information|Info|INFO)\b|(?<![\w.\]\-])-\d+(?:\.\d+)?(?![\w.]))"
    r"|(?P<m>\b(?:For|If)\b)")
_HL_COL = {"r": "1;31", "y": "1;33", "g": "1;32", "c": "1;36", "m": "1;35"}


def highlight(text):
    """MobaXterm 처럼 키워드 강조 (화면 출력 전용)"""
    if not HIGHLIGHT or not USE_COLOR or "\033[" in text:
        return text
    out = []
    for line in text.split("\n"):
        st = line.lstrip()
        if st.startswith("!!!") or st.startswith("### "):
            out.append("\033[1;32m%s\033[0m" % line if line else line)
            continue
        out.append(_HL_RE.sub(lambda m: "\033[%sm%s\033[0m" % (_HL_COL[m.lastgroup], m.group(0)), line))
    return "\n".join(out)


class TermOut:
    def __init__(self):
        self.tty = sys.stdout.isatty()
        self.screen = True

    def write(self, s):
        sys.stdout.write(highlight(s) if self.tty else s)
        sys.stdout.flush()


class BufOut:
    tty = False
    screen = False

    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    def getvalue(self):
        return "".join(self.parts)


class FileOut:
    tty = False
    screen = False

    def __init__(self, f):
        self.f = f

    def write(self, s):
        self.f.write(s)

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


class TeeOut:
    tty = False

    def __init__(self, a, files):
        self.a, self.files = a, files
        self.screen = getattr(a, "screen", False)

    def write(self, s):
        self.a.write(s)
        for f in self.files:
            f.write(s)


class NullFile:
    tty = False
    screen = False

    def write(self, s):
        return len(s)

    def tell(self):
        return 0

    def close(self):
        pass

    def flush(self):
        pass


def C(out, code, s):
    return "\033[%sm%s\033[0m" % (code, s) if (USE_COLOR and getattr(out, "tty", False)) else s


def decode_text(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("cp949")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# 가상 파일 시스템
# --------------------------------------------------------------------------
class VFS:
    def __init__(self, user, home_real, tmp_real):
        self.user = user
        self.home = "/home/" + user
        self.mounts = OrderedDict()      # vpath -> dict(real, rw, kind)
        self.vfiles = {}                 # vpath -> (content|callable, mode)
        self.extra_vdirs = set()
        self.t0 = time.time()
        self._vd = None
        self.add_mount(self.home, home_real, True, "home")
        self.add_mount("/tmp", tmp_real, True, "tmp")

    # ---- 구성 ----
    def add_mount(self, vp, real, rw, kind):
        self.mounts[vp] = dict(real=real, rw=rw, kind=kind)
        self._vd = None

    def remove_mount(self, vp):
        self.mounts.pop(vp, None)
        self._vd = None

    def add_vfile(self, vp, content, mode="-r--r--r--"):
        self.vfiles[vp] = (content, mode)
        self._vd = None

    def add_vdir(self, vp):
        self.extra_vdirs.add(vp)
        self._vd = None

    def vdirs(self):
        if self._vd is None:
            s = {"/"} | set(self.extra_vdirs)
            for p in list(self.mounts) + list(self.vfiles) + list(self.extra_vdirs):
                parts = p.strip("/").split("/")
                for i in range(len(parts)):
                    s.add("/" + "/".join(parts[:i]) if i else "/")
            self._vd = s
        return self._vd

    # ---- 경로 ----
    def abspath(self, p, cwd):
        p = str(p)
        dm = re.match(r"^([A-Za-z]):(?:[\\/](.*))?$", p)
        if dm:                                   # C:\Users\..  ->  /mnt/c/Users/..
            p = "/mnt/%s/%s" % (dm.group(1).lower(), dm.group(2) or "")
        p = p.replace("\\", "/")
        if p == "~" or p.startswith("~/"):
            p = self.home + p[1:]
        if not p.startswith("/"):
            p = cwd.rstrip("/") + "/" + p
        parts = []
        for seg in p.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(seg)
        return "/" + "/".join(parts)

    def mount_of(self, ap):
        best = None
        for vp in self.mounts:
            if ap == vp or ap.startswith(vp.rstrip("/") + "/"):
                if best is None or len(vp) > len(best):
                    best = vp
        if best is None:
            return None
        m = self.mounts[best]
        rest = ap[len(best):].strip("/")
        real = os.path.join(m["real"], *rest.split("/")) if rest else m["real"]
        return best, real, m["rw"]

    def real(self, ap):
        m = self.mount_of(ap)
        return m[1] if m else None

    def to_virtual(self, real):
        ra = os.path.abspath(real)
        best = None
        for vp, m in self.mounts.items():
            mr = os.path.abspath(m["real"])
            x, y = os.path.normcase(ra), os.path.normcase(mr)
            if x == y or x.startswith(y.rstrip("\\/") + os.sep):
                if best is None or len(mr) > len(best[1]):
                    best = (vp, mr)
        if not best:
            return None
        rest = ra[len(best[1]):].strip("\\/")
        return best[0].rstrip("/") + ("/" + rest.replace("\\", "/") if rest else "")

    # ---- 조회 ----
    def stat(self, ap):
        if ap in self.vfiles:
            c, mode = self.vfiles[ap]
            data = c() if callable(c) else c
            return dict(kind="file", size=len(data.encode("utf-8")), mtime=self.t0, rw=False, mode=mode,
                        virtual=True, owner="root")
        m = self.mount_of(ap)
        if m and ap not in self.vdirs() or (m and ap == m[0]):
            real = m[1]
            try:
                st = os.stat(real)
            except (OSError, ValueError):
                return None
            isdir = os.path.isdir(real)
            rw = m[2]
            ex = isdir or real.lower().endswith((".sh", ".csh", ".py", ".exe", ".bat"))
            if isdir:
                mode = "drwxr-xr-x" if rw else "dr-xr-xr-x"
            else:
                mode = ("-rwxr-xr-x" if ex else "-rw-r--r--") if rw else ("-r-xr-xr-x" if ex else "-r--r--r--")
            return dict(kind="dir" if isdir else "file", size=(4096 if isdir else st.st_size), mtime=st.st_mtime,
                        rw=rw, mode=mode, virtual=False, real=real,
                        owner=(self.user if self.mounts[m[0]]["kind"] in ("home", "tmp") else "root"))
        if ap in self.vdirs():
            return dict(kind="dir", size=4096, mtime=self.t0, rw=False, mode="dr-xr-xr-x", virtual=True, owner="root")
        return None

    def exists(self, ap):
        return self.stat(ap) is not None

    def isdir(self, ap):
        st = self.stat(ap)
        return bool(st and st["kind"] == "dir")

    def isfile(self, ap):
        st = self.stat(ap)
        return bool(st and st["kind"] == "file")

    def listdir(self, ap):
        st = self.stat(ap)
        if not st:
            raise FSError("No such file or directory")
        if st["kind"] != "dir":
            raise FSError("Not a directory")
        names = set()
        m = self.mount_of(ap)
        if m and (ap not in self.vdirs() or ap == m[0]):
            try:
                names.update(os.listdir(m[1]))
            except PermissionError:
                raise FSError("Permission denied")
            except OSError as e:
                raise FSError(e.strerror or str(e))
        pref = ap.rstrip("/") + "/"
        for p in list(self.mounts) + list(self.vfiles) + list(self.vdirs()):
            if p != ap and p.startswith(pref):
                names.add(p[len(pref):].split("/")[0])
        return sorted(names)

    def read_bytes(self, ap):
        if ap in self.vfiles:
            c, _ = self.vfiles[ap]
            return (c() if callable(c) else c).encode("utf-8")
        st = self.stat(ap)
        if not st:
            raise FSError("No such file or directory")
        if st["kind"] == "dir":
            raise FSError("Is a directory")
        try:
            with open(st["real"], "rb") as f:
                return f.read()
        except PermissionError:
            raise FSError("Permission denied")
        except OSError as e:
            raise FSError(e.strerror or str(e))

    def read_text(self, ap):
        return decode_text(self.read_bytes(ap))

    # ---- 쓰기 (쓰기 가능한 마운트 안에서만) ----
    def _wreal(self, ap):
        m = self.mount_of(ap)
        if not m:
            if ap in self.vfiles or ap in self.vdirs():
                raise FSError("Read-only file system")
            raise FSError("Permission denied")
        if not m[2]:
            raise FSError("Read-only file system")
        return m[0], m[1]

    def open_write(self, ap, append=False, protect_hdl=False, binary=False):
        if ap == "/dev/null":
            return NullFile()
        root, real = self._wreal(ap)
        if ap == root or os.path.isdir(real):
            raise FSError("Is a directory")
        if not os.path.isdir(os.path.dirname(real)):
            raise FSError("No such file or directory")
        if protect_hdl and os.path.exists(real) and real.lower().endswith(HDL_EXT):
            try:
                with open(real, "r", encoding="utf-8", errors="replace") as f:
                    head = f.read(400)
            except OSError:
                head = ""
            if MARKER not in head:
                raise FSError("HDL source file exists (protected)")
        if binary:
            return open(real, "ab" if append else "wb")
        return open(real, "a" if append else "w", encoding="utf-8", newline="\n")

    def mkdir(self, ap, parents=False):
        root, real = self._wreal(ap)
        if os.path.exists(real):
            if parents and os.path.isdir(real):
                return
            raise FSError("File exists")
        if parents:
            os.makedirs(real)
        else:
            if not os.path.isdir(os.path.dirname(real)):
                raise FSError("No such file or directory")
            os.mkdir(real)

    def rmdir(self, ap):
        root, real = self._wreal(ap)
        if ap == root:
            raise FSError("Device or resource busy")
        if not os.path.isdir(real):
            raise FSError("Not a directory" if os.path.exists(real) else "No such file or directory")
        try:
            os.rmdir(real)
        except OSError:
            raise FSError("Directory not empty")

    def remove(self, ap, recursive=False):
        root, real = self._wreal(ap)
        if ap == root:
            raise FSError("Device or resource busy")
        if not os.path.lexists(real):
            raise FSError("No such file or directory")
        if os.path.isdir(real):
            if not recursive:
                raise FSError("Is a directory")
            shutil.rmtree(real)
        else:
            os.remove(real)

    def rename(self, src, dst):
        r1, rs = self._wreal(src)
        r2, rd = self._wreal(dst)
        if src == r1:
            raise FSError("Device or resource busy")
        if not os.path.exists(rs):
            raise FSError("No such file or directory")
        os.replace(rs, rd) if not os.path.isdir(rd) else shutil.move(rs, rd)

    def touch(self, ap):
        root, real = self._wreal(ap)
        if os.path.exists(real):
            os.utime(real, None)
        else:
            if not os.path.isdir(os.path.dirname(real)):
                raise FSError("No such file or directory")
            open(real, "a").close()


# --------------------------------------------------------------------------
# 템플릿 (첫 실행 때 home 에 생성)
# --------------------------------------------------------------------------
T_COUNTER = """// 학습용 예제: 파라미터 카운터 (비동기 리셋, enable)
module counter #(
    parameter WIDTH = 8
) (
    input                  clk,
    input                  rst_n,
    input                  en,
    output reg [WIDTH-1:0] count,
    output                 wrap
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            count <= {WIDTH{1'b0}};
        else if (en)
            count <= count + 1'b1;
    end

    assign wrap = en & (&count);
endmodule
"""
T_ALU = """// 학습용 예제: 간단한 ALU (조합 논리)
module alu #(
    parameter W = 16
) (
    input      [W-1:0] a,
    input      [W-1:0] b,
    input      [2:0]   op,
    output reg [W-1:0] y,
    output             zero,
    output             a_gt_b
);
    always @(*) begin
        case (op)
            3'd0: y = a + b;
            3'd1: y = a - b;
            3'd2: y = a & b;
            3'd3: y = a | b;
            3'd4: y = a ^ b;
            3'd5: y = a << 1;
            3'd6: y = a >> 1;
            default: y = {W{1'b0}};
        endcase
    end

    assign zero   = (y == {W{1'b0}});
    assign a_gt_b = (a > b);
endmodule
"""
T_MAC = """// 학습용 예제: 곱셈-누산기 (타이밍이 빡빡한 경로 연습용)
module mac #(
    parameter W = 8
) (
    input                clk,
    input                rst_n,
    input                clr,
    input  [W-1:0]       x,
    input  [W-1:0]       c,
    output reg [2*W+3:0] acc
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            acc <= 0;
        else if (clr)
            acc <= 0;
        else
            acc <= acc + x * c;
    end
endmodule
"""
T_TOP = """// 학습용 예제: 최상위 모듈 (counter + alu + mac 계층 구조)
module top (
    input         clk,
    input         rst_n,
    input         en,
    input  [15:0] a,
    input  [15:0] b,
    input  [2:0]  op,
    input  [7:0]  x,
    input  [7:0]  c,
    output [7:0]  cnt,
    output        cnt_wrap,
    output reg [15:0] result,
    output        zero,
    output [19:0] mac_out
);
    wire [15:0] alu_y;
    wire        alu_zero;
    wire        alu_gt;

    counter #(.WIDTH(8)) u_cnt (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (en),
        .count (cnt),
        .wrap  (cnt_wrap)
    );

    alu #(.W(16)) u_alu (
        .a      (a),
        .b      (b),
        .op     (op),
        .y      (alu_y),
        .zero   (alu_zero),
        .a_gt_b (alu_gt)
    );

    mac #(.W(8)) u_mac (
        .clk   (clk),
        .rst_n (rst_n),
        .clr   (cnt_wrap),
        .x     (x),
        .c     (c),
        .acc   (mac_out)
    );

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            result <= 16'd0;
        else
            result <= alu_y;
    end

    assign zero = alu_zero;
endmodule
"""
T_SETUP = """# ------------------------------------------------------------------
#  프로젝트 로컬 설정 : 이 폴더에서 dc_shell 을 실행하면 자동으로 읽힘
# ------------------------------------------------------------------
set_app_var search_path    "$search_path ./rtl ./scripts /tools/lib/edu_lib/db"
set_app_var target_library "edu_lib_tt.db"
set_app_var link_library   "* $target_library"
define_design_lib WORK -path ./WORK
"""
T_SETUP_SKY = """# ------------------------------------------------------------------
#  SkyWater SKY130 (sky130_fd_sc_hd) 실제 공개 라이브러리 사용 설정
#  코너: tt_025C_1v80 (typical) / ss_n40C_1v40 (slow) / ff_n40C_1v95 (fast)
# ------------------------------------------------------------------
set_app_var search_path    "$search_path ./rtl ./scripts /tools/lib/sky130_fd_sc_hd/db"
set_app_var target_library "sky130_fd_sc_hd__tt_025C_1v80.db"
set_app_var link_library   "* $target_library"
define_design_lib WORK -path ./WORK
"""
T_RUN_SKY = """# ==========================================================
#  SKY130 실제 공개 라이브러리(sky130_fd_sc_hd)로 합성하는 예제
#  실행:  cd ~/projects/sky130_demo
#         dc_shell -f scripts/run_dc.tcl | tee dc.log
#  코너 비교: dc_shell -f scripts/run_corners.tcl | tee corners.log
# ==========================================================
set TOP        top
set CLK_PERIOD 10.0   ;# 100 MHz (130nm 공정)

sh mkdir -p reports outputs
set_svf outputs/${TOP}.svf

analyze -format verilog [glob ./rtl/*.v]
elaborate $TOP
current_design $TOP
link
check_design > reports/check_design.rpt

create_clock -period $CLK_PERIOD -name clk [get_ports clk]
set_clock_uncertainty 0.25 [get_clocks clk]
set_clock_transition  0.15 [get_clocks clk]
set DATA_IN [remove_from_collection [all_inputs] [get_ports clk]]
set_input_delay  [expr {$CLK_PERIOD * 0.2}] -clock clk $DATA_IN
set_output_delay [expr {$CLK_PERIOD * 0.2}] -clock clk [all_outputs]
set_driving_cell -lib_cell sky130_fd_sc_hd__buf_2 $DATA_IN
set_load 0.005 [all_outputs]
set_false_path -from [get_ports rst_n]
check_timing

compile_ultra -gate_clock

report_qor
report_timing -max_paths 1 -transition_time
report_timing -delay_type min -max_paths 1
report_area -hierarchy       > reports/area.rpt
report_reference             > reports/reference.rpt
report_timing -max_paths 10  > reports/timing.rpt
report_power -hierarchy      > reports/power.rpt
report_constraint -all_violators > reports/violators.rpt

change_names -rules verilog -hierarchy
write -format verilog -hierarchy -output outputs/${TOP}_netlist.v
write -format ddc     -hierarchy -output outputs/${TOP}.ddc
write_sdc outputs/${TOP}.sdc
echo "==== SKY130 합성 완료 (reports/, outputs/) ===="
exit
"""
T_RUN_CORNERS = """# ==========================================================
#  같은 RTL 을 SKY130 tt / ss / ff 코너로 각각 합성해서 비교
#  dc_shell -f scripts/run_corners.tcl | tee corners.log
# ==========================================================
set TOP top
set CLK_PERIOD 10.0
sh mkdir -p reports
analyze -format verilog [glob ./rtl/*.v]

foreach corner {tt_025C_1v80 ss_n40C_1v40 ff_n40C_1v95} {
    echo ""
    echo "################  corner : $corner  ################"
    set_app_var target_library "sky130_fd_sc_hd__${corner}.db"
    set_app_var link_library   "* $target_library"
    remove_design -all
    elaborate $TOP
    link
    create_clock -period $CLK_PERIOD -name clk [get_ports clk]
    set_clock_uncertainty 0.25 [get_clocks clk]
    set_input_delay  2.0 -clock clk [remove_from_collection [all_inputs] [get_ports clk]]
    set_output_delay 2.0 -clock clk [all_outputs]
    compile_ultra
    report_qor > reports/qor_${corner}.rpt
    report_timing > reports/timing_${corner}.rpt
    echo "  area  = [get_attribute [current_design] area]"
}
echo ""
echo "비교:  grep -H \"Critical Path Slack\" reports/qor_*.rpt"
exit
"""
T_RUN_PT = """# ==========================================================
#  PrimeTime (pt_shell) STA : DC 가 만든 netlist + SDC 로 타이밍 검증
#  실행: pt_shell -f scripts/run_pt.tcl | tee pt.log
#  (먼저 dc_shell -f scripts/run_dc.tcl 로 outputs/ 를 만들어야 합니다)
# ==========================================================
set TOP top
set search_path ". %(dbdir)s"
set link_path   "* %(db)s"

read_verilog outputs/${TOP}_netlist.v
current_design $TOP
link_design
read_sdc outputs/${TOP}.sdc

check_timing
update_timing
report_analysis_coverage
report_global_timing
report_timing -max_paths 1
report_timing -delay_type min -max_paths 1
report_constraint -all_violators
sh mkdir -p reports
report_analysis_coverage > reports/pt_coverage.rpt
report_timing -max_paths 10 > reports/pt_timing.rpt
exit
"""
T_RUN_FM = """# ==========================================================
#  Formality (fm_shell) 등가성 검증 : RTL(reference) vs netlist(implementation)
#  실행: fm_shell -f scripts/run_fm.tcl | tee fm.log
# ==========================================================
set TOP top
set synopsys_auto_setup true
if {[file exists outputs/${TOP}.svf]} { set_svf outputs/${TOP}.svf } else { set_svf default.svf }

read_db -technology_library %(dbdir)s/%(db)s

read_verilog -r [glob ./rtl/*.v] -libname WORK
set_top r:/WORK/$TOP

read_verilog -i outputs/${TOP}_netlist.v -libname WORK
set_top i:/WORK/$TOP

match
verify
report_failing_points
exit
"""
T_RUN_DEMO = """# ==========================================================
#  기본 합성 플로우 예제
#  실행:  cd ~/projects/demo
#         dc_shell -f scripts/run_dc.tcl | tee dc.log
# ==========================================================
set TOP        top
set CLK_PERIOD 2.5

sh mkdir -p reports outputs

# 1) RTL 읽기
analyze -format verilog [glob ./rtl/*.v]
elaborate $TOP
current_design $TOP
link
check_design > reports/check_design.rpt

# 2) 제약
create_clock -period $CLK_PERIOD -name clk [get_ports clk]
set_clock_uncertainty 0.1 [get_clocks clk]
set_input_delay  0.3 -clock clk [remove_from_collection [all_inputs] [get_ports clk]]
set_output_delay 0.3 -clock clk [all_outputs]
set_load 0.02 [all_outputs]
set_driving_cell -lib_cell BUFX2 [remove_from_collection [all_inputs] [get_ports clk]]
set_false_path -from [get_ports rst_n]
check_timing

# 3) 합성
compile_ultra -gate_clock

# 4) 리포트
report_qor
report_timing -max_paths 1
report_area -hierarchy       > reports/area.rpt
report_timing -max_paths 5   > reports/timing.rpt
report_power -hierarchy      > reports/power.rpt
report_constraint -all_violators > reports/violators.rpt
report_resources             > reports/resources.rpt
report_clock_gating          > reports/clock_gating.rpt

# 5) 결과 저장
change_names -rules verilog -hierarchy
write -format verilog -hierarchy -output outputs/${TOP}_netlist.v
write -format ddc     -hierarchy -output outputs/${TOP}.ddc
write_sdc outputs/${TOP}.sdc

echo "==== 합성 완료: reports/ outputs/ 폴더를 확인하세요 ===="
exit
"""
T_RUN_NEW = """# ==========================================================
#  %(name)s 프로젝트 합성 스크립트 (newproj 로 생성됨)
#  1) RTL(.v/.sv) 을 rtl/ 폴더에 넣기   예) cp /mnt/c/.../*.v rtl/
#  2) 아래 TOP 을 최상위 모듈 이름으로 수정   (modfind rtl 로 확인)
#  3) dc_shell -f scripts/run_dc.tcl | tee dc.log
# ==========================================================
set TOP        %(top)s
set CLK_PERIOD 5.0

sh mkdir -p reports outputs

analyze -format sverilog [glob -nocomplain ./rtl/*.v ./rtl/*.sv]
elaborate $TOP
current_design $TOP
link
check_design > reports/check_design.rpt

# 클럭 포트 자동 탐색 (없으면 가상 클럭)
set CLK_PORT [lindex [get_object_name [get_ports -quiet {clk clock *clk* *CLK*}]] 0]
if {"$CLK_PORT" != ""} {
    create_clock -period $CLK_PERIOD -name clk [get_ports $CLK_PORT]
    set DATA_IN [remove_from_collection [all_inputs] [get_ports $CLK_PORT]]
} else {
    echo "Info: no clock port found -> virtual clock"
    create_clock -period $CLK_PERIOD -name clk
    set DATA_IN [all_inputs]
}
set_clock_uncertainty 0.1 [get_clocks clk]
set_input_delay  [expr {$CLK_PERIOD * 0.2}] -clock clk $DATA_IN
set_output_delay [expr {$CLK_PERIOD * 0.2}] -clock clk [all_outputs]
check_timing

compile_ultra

report_qor
report_timing -max_paths 1
report_area -hierarchy  > reports/area.rpt
report_timing -max_paths 10 > reports/timing.rpt
report_power  > reports/power.rpt

change_names -rules verilog -hierarchy
write -format verilog -hierarchy -output outputs/${TOP}_netlist.v
write_sdc outputs/${TOP}.sdc
exit
"""
T_BASHRC = """# ~/.bashrc  (dc_shell_sim 가상 리눅스)  - 자유롭게 수정해도 됩니다 (vi ~/.bashrc)
export SYNOPSYS=/tools/synopsys/syn/SIM-2026.11
module load syn/SIM-2026.11
alias ll='ls -l'
alias la='ls -la'
alias dc='dc_shell'
alias demo='cd ~/projects/demo'
"""
T_GUIDE = """dc_shell_sim 가상 리눅스 실습 안내
==================================

[1] 예제 프로젝트 합성
    cd ~/projects/demo
    ls -la
    cat scripts/run_dc.tcl
    dc_shell -f scripts/run_dc.tcl | tee dc.log
    ls reports outputs
    less reports/timing.rpt

[2] 대화형으로 하나씩
    cd ~/projects/demo
    dc_shell
    dc_shell> analyze -format verilog [glob ./rtl/*.v]
    dc_shell> elaborate top
    dc_shell> create_clock -period 2 [get_ports clk]
    dc_shell> compile_ultra
    dc_shell> report_timing
    dc_shell> exit

[3] 내 PC 에 있는 RTL 로 연습 (PC 폴더는 읽기 전용, 복사해서 사용)
    modfind /mnt/c/Users/<이름>/PycharmProjects        # .v 모듈 찾기
    newproj myproj                                    # 새 프로젝트 생성
    cp /mnt/c/.../rtl/*.v ~/projects/myproj/rtl/
    cd ~/projects/myproj
    modfind rtl                                       # top 후보 확인
    vi scripts/run_dc.tcl                             # TOP 수정 (메모장이 열림)
    dc_shell -f scripts/run_dc.tcl | tee dc.log

    ※ 복사하지 않고 바로 읽어도 됩니다:
       dc_shell> analyze -format verilog /mnt/c/.../my.v

[4] 자주 쓰는 리눅스 명령
    ls -l, cd, pwd, mkdir -p, cp -r, mv, rm -r, cat, less, head, tail,
    grep -rn "module" rtl, find . -name "*.v", wc -l, tree, diff, history
"""

TEMPLATES = OrderedDict([
    (".bashrc", T_BASHRC),
    ("README_실습.txt", T_GUIDE),
    ("projects/demo/.synopsys_dc.setup", T_SETUP),
    ("projects/demo/rtl/counter.v", T_COUNTER),
    ("projects/demo/rtl/alu.v", T_ALU),
    ("projects/demo/rtl/mac.v", T_MAC),
    ("projects/demo/rtl/top.v", T_TOP),
    ("projects/demo/scripts/run_dc.tcl", T_RUN_DEMO),
    ("projects/sky130_demo/.synopsys_dc.setup", T_SETUP_SKY),
    ("projects/sky130_demo/rtl/counter.v", T_COUNTER),
    ("projects/sky130_demo/rtl/alu.v", T_ALU),
    ("projects/sky130_demo/rtl/mac.v", T_MAC),
    ("projects/sky130_demo/rtl/top.v", T_TOP),
    ("projects/sky130_demo/scripts/run_dc.tcl", T_RUN_SKY),
    ("projects/sky130_demo/scripts/run_corners.tcl", T_RUN_CORNERS),
    ("projects/sky130_demo/scripts/run_pt.tcl",
     T_RUN_PT % {"dbdir": "/tools/lib/sky130_fd_sc_hd/db", "db": "sky130_fd_sc_hd__tt_025C_1v80.db"}),
    ("projects/sky130_demo/scripts/run_fm.tcl",
     T_RUN_FM % {"dbdir": "/tools/lib/sky130_fd_sc_hd/db", "db": "sky130_fd_sc_hd__tt_025C_1v80.db"}),
    ("projects/demo/scripts/run_pt.tcl", T_RUN_PT % {"dbdir": "/tools/lib/edu_lib/db", "db": "edu_lib_tt.db"}),
    ("projects/demo/scripts/run_fm.tcl", T_RUN_FM % {"dbdir": "/tools/lib/edu_lib/db", "db": "edu_lib_tt.db"}),
])
TEMPLATE_VERSION = 4
TEMPLATE_NEW = {3: ("projects/sky130_demo/",),
                4: ("projects/sky130_demo/scripts/run_pt.tcl", "projects/sky130_demo/scripts/run_fm.tcl",
                    "projects/demo/scripts/run_pt.tcl", "projects/demo/scripts/run_fm.tcl")}

# --------------------------------------------------------------------------
# 도움말
# --------------------------------------------------------------------------
LINUX_HELP = OrderedDict([
    ("파일/폴더", [("ls [-lahRtr1d]", "목록"), ("cd [dir|-|~]", "이동"), ("pwd", "현재 위치"),
                ("mkdir [-p]", "폴더 만들기"), ("rmdir", "빈 폴더 삭제"), ("rm [-rf]", "삭제"),
                ("cp [-r]", "복사"), ("mv", "이동/이름변경"), ("touch", "빈 파일"), ("tree [-L n] [-d]", "트리"),
                ("du [-sh]", "용량"), ("df -h", "디스크"), ("file", "파일 종류"), ("basename / dirname", "경로 자르기"),
                ("realpath", "절대 경로")]),
    ("파일 내용", [("cat [-n]", "출력"), ("less / more", "페이지 보기"), ("head / tail [-n N]", "앞/뒤"),
                ("grep [-rnilvcwE]", "검색"), ("find [path] -name -type -maxdepth", "찾기"), ("wc [-lwc]", "세기"),
                ("sort [-rnu] / uniq [-c]", "정렬/중복"), ("cut -d -f", "열 자르기"), ("diff", "비교"),
                ("tee [-a]", "화면+파일"), ("vi / vim / nano / gedit / code", "편집 (메모장/VSCode 실행)")]),
    ("셸/환경", [("echo / printf", "출력"), ("export / setenv / unset / env", "환경변수"),
               ("alias / unalias", "별칭"), ("history / !! / !n", "기록"), ("source / . / bash", "스크립트 실행"),
               ("which / type", "명령 위치"), ("module avail|load|list|unload", "툴 환경 모듈"),
               ("whoami / hostname / uname -a / date / id / ps / uptime", "정보"), ("clear", "화면 지우기"),
               ("man <cmd>", "설명"), ("exit / logout", "종료")]),
    ("EDA 툴", [("dc_shell [-f script] [-x cmd] [-topo] [-output_log_file f]", "Design Compiler 실행"),
               ("pt_shell [-f script]", "PrimeTime (STA) 실행"),
               ("fm_shell [-f script]", "Formality (등가성 검증) 실행"),
               ("dc_shell -f run.tcl | tee dc.log", "로그 남기며 실행"), ("design_vision", "GUI (미지원 안내)")]),
    ("시뮬레이터 전용", [("modfind [dir]", "폴더 안 .v/.sv 에서 모듈/포트/top 후보 찾기"),
                   ("newproj <name> [top] [-lib sky130|edu]", "~/projects/<name> 연습 프로젝트 생성 (기본 sky130)"),
                   ("restore_demo", "지운 예제(~/projects/demo) 복구"),
                   ("mount [PC경로 이름] / umount", "PC 폴더를 /mnt/<이름> 으로 연결 (읽기 전용)"),
                   ("winpath <path>", "가상 경로의 실제 PC 경로 보기"),
                   ("explorer [dir]", "실제 폴더를 Windows 탐색기로 열기")]),
])

MAN = {
    "ls": "ls [-l 자세히] [-a 숨김포함] [-h 사람이 읽기 쉬운 크기] [-R 재귀] [-t 시간순] [-r 역순] [-1 한 줄에 하나] [-d 폴더 자체]",
    "cd": "cd <dir>   (cd 만 입력: 홈, cd - : 이전 폴더, cd .. : 상위)",
    "grep": "grep [-r 재귀] [-n 줄번호] [-i 대소문자무시] [-l 파일명만] [-v 반전] [-c 개수] [-w 단어] [-E 확장정규식] PATTERN [FILE...]",
    "find": "find [path...] [-name PAT] [-iname PAT] [-type f|d] [-maxdepth N] [-mindepth N]",
    "cp": "cp [-r] SRC... DEST   (PC 폴더(/mnt/..)에서 홈으로 복사 가능, PC 폴더로 쓰기는 불가)",
    "rm": "rm [-r] [-f] FILE...   (홈/tmp 안에서만 가능)",
    "dc_shell": ("dc_shell [-f script.tcl] [-x \"cmd\"] [-topographical_mode] [-no_home_init] [-no_local_init]\n"
                 "         [-output_log_file file] [-version]\n"
                 "  * 현재 폴더 / 홈의 .synopsys_dc.setup 을 자동으로 읽습니다.\n"
                 "  * -f 스크립트가 exit 로 끝나지 않으면 dc_shell> 프롬프트로 남습니다.\n"
                 "  * dc_shell 안에서 exit 를 입력하면 리눅스 터미널로 돌아옵니다."),
    "pt_shell": ("pt_shell [-f script.tcl] [-x cmd] [-output_log_file file]\n"
                 "  * DC 의 write -format verilog netlist 와 write_sdc 결과를 읽어 STA 를 합니다.\n"
                 "  * link_path 로 라이브러리 지정, read_verilog / link_design / read_sdc / check_timing /\n"
                 "    update_timing / report_analysis_coverage / report_global_timing / report_timing"),
    "fm_shell": ("fm_shell [-f script.tcl]\n"
                 "  * set_svf, read_db -technology_library, read_verilog -r (RTL) / -i (netlist),\n"
                 "    set_top r:/WORK/top, set_top i:/WORK/top, match, verify, report_failing_points\n"
                 "  * 프롬프트가 fm_shell (setup)> -> (match)> -> (verify)> 로 바뀝니다."),
    "modfind": "modfind [dir] [-maxdepth N]  : dir 아래 .v/.sv 파일을 읽어 모듈, 포트 수, 파라미터, 하위 인스턴스, top 후보를 보여줍니다.",
    "newproj": "newproj <name> [top] : ~/projects/<name>/{rtl,scripts,reports,outputs} 와 .synopsys_dc.setup, scripts/run_dc.tcl 생성",
    "mount": "mount                 : 연결 목록\nmount <PC경로> [이름] : PC 폴더를 /mnt/<이름> 로 읽기 전용 연결 (설정 파일에 저장됨)\numount /mnt/<이름>    : 연결 해제",
    "vi": "vi <file> : 홈 안의 파일은 메모장(EDITOR 변수, 기본 notepad)으로 열어 편집합니다. PC 폴더 파일은 읽기 전용 보기.",
    "module": "module avail | load <mod> | unload <mod> | list | purge   (환경 모듈 흉내. syn 을 unload 하면 dc_shell 을 못 찾습니다)",
}

MODULES = OrderedDict([
    ("syn/" + SYN_VER, dict(path=SYN_BIN, env={"SYNOPSYS": SYN_ROOT})),
    ("pt/" + SYN_VER, dict(path=PT_BIN, env={"PT_HOME": PT_BIN[:-4]})),
    ("fm/" + SYN_VER, dict(path=FM_BIN, env={"FM_HOME": FM_BIN[:-4]})),
    ("lib/edu_lib", dict(path=None, env={"EDU_LIB": LIB_ROOT})),
])


def flags(args, with_val=""):
    fl, vals, pos = set(), {}, []
    i = 0
    endopt = False
    while i < len(args):
        a = args[i]
        if endopt or not a.startswith("-") or a == "-" or re.match(r"^-\d+$", a) is not None and not with_val:
            pos.append(a)
            i += 1
            continue
        if a == "--":
            endopt = True
            i += 1
            continue
        if a.startswith("--"):
            fl.add(a)
            i += 1
            continue
        j = 1
        while j < len(a):
            ch = a[j]
            if ch in with_val:
                v = a[j + 1:]
                if not v and i + 1 < len(args):
                    i += 1
                    v = args[i]
                vals[ch] = v
                break
            fl.add(ch)
            j += 1
        i += 1
    return fl, vals, pos


def hsize(n):
    for unit in ("", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            if unit == "":
                return str(int(n))
            return ("%.1f%s" % (n, unit)) if n < 10 else ("%d%s" % (round(n), unit))
        n /= 1024.0


def lsdate(ts):
    d = datetime.datetime.fromtimestamp(ts)
    if abs(time.time() - ts) > 180 * 86400:
        return "%s %2d  %d" % (d.strftime("%b"), d.day, d.year)
    return "%s %2d %02d:%02d" % (d.strftime("%b"), d.day, d.hour, d.minute)


# --------------------------------------------------------------------------
# 터미널
# --------------------------------------------------------------------------
class Terminal:
    def __init__(self, dcmod, sim_root):
        self.dcmod = dcmod
        self.sim_root = sim_root
        u = re.sub(r"[^A-Za-z0-9_.-]", "", (getpass.getuser() or "user")) or "user"
        self.user = u.lower()
        self.home_real = os.path.join(sim_root, "home")
        first = not os.path.isdir(self.home_real)
        os.makedirs(self.home_real, exist_ok=True)
        tmp_real = os.path.join(self.home_real, ".tmp")
        os.makedirs(tmp_real, exist_ok=True)
        self.fs = VFS(self.user, self.home_real, tmp_real)
        self.cfg_path = os.path.join(sim_root, "sim_mounts.json")
        self._mount_drives()
        self._load_user_mounts()
        self._virtual_tree()
        self.stdout = TermOut()
        self.env = OrderedDict([
            ("USER", self.user), ("LOGNAME", self.user), ("HOME", self.fs.home), ("HOSTNAME", HOSTNAME),
            ("SHELL", "/bin/bash"), ("TERM", "xterm-256color"), ("LANG", "ko_KR.UTF-8"),
            ("PATH", "%s:/usr/bin:/bin" % SIM_BIN), ("PWD", self.fs.home), ("OLDPWD", self.fs.home),
            ("EDITOR", "notepad" if IS_WIN else (os.environ.get("EDITOR") or "vi")),
        ])
        self.cwd = self.fs.home
        self.aliases = OrderedDict()
        self.loaded_modules = []
        self.status = 0
        self.history = []
        self.in_dc = False
        self.cmds = self._build_cmds()
        if first or not os.listdir(self.home_real) or os.listdir(self.home_real) == [".tmp"]:
            self._init_home()
        self._upgrade_home()
        self._load_history()

    # ---------------- 구성 ----------------
    def _mount_drives(self):
        if IS_WIN:
            import string
            for L in string.ascii_uppercase:
                root = "%s:\\" % L
                try:
                    if os.path.exists(root):
                        self.fs.add_mount("/mnt/" + L.lower(), root, False, "drive")
                except OSError:
                    pass
        else:
            self.fs.add_mount("/mnt/host", "/", False, "drive")
        self.fs.add_vdir("/mnt")

    def _load_user_mounts(self):
        try:
            with open(self.cfg_path, encoding="utf-8") as f:
                data = json.load(f)
            for name, real in data.get("mounts", {}).items():
                if os.path.isdir(real):
                    self.fs.add_mount("/mnt/" + name, real, False, "user")
        except Exception:
            pass

    def _save_user_mounts(self):
        data = {"_comment": "%s : mount 명령으로 연결한 PC 폴더 (읽기 전용)" % MARKER,
                "mounts": {vp[5:]: m["real"] for vp, m in self.fs.mounts.items() if m["kind"] == "user"}}
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _virtual_tree(self):
        fs = self.fs
        dcm = self.dcmod
        exe = "-rwxr-xr-x"
        fs.add_vfile("/etc/hostname", HOSTNAME + "\n")
        fs.add_vfile("/etc/redhat-release", "CentOS Linux release 7.9.2009 (Core)   [dc_shell_sim virtual]\n")
        fs.add_vfile("/etc/os-release", 'NAME="CentOS Linux"\nVERSION="7 (Core)"\nPRETTY_NAME="CentOS Linux 7 (Core) - dc_shell_sim virtual"\n')
        fs.add_vfile("/dev/null", "", "crw-rw-rw-")
        for c in sorted(self._bin_names("/usr/bin")):
            fs.add_vfile("/usr/bin/" + c, "#!/bin/sh\n# %s builtin (simulated)\n" % c, exe)
        for c in ("modfind", "newproj", "restore_demo", "mount", "umount", "winpath", "explorer"):
            fs.add_vfile(SIM_BIN + "/" + c, "#!/bin/sh\n# dc_shell_sim helper\n", exe)
        fs.add_vfile("/bin/bash", "#!/bin/sh\n# simulated bash\n", exe)
        fs.add_vfile("/bin/sh", "#!/bin/sh\n# simulated sh\n", exe)
        for c in ("dc_shell", "dc_shell-t", "design_vision"):
            fs.add_vfile(SYN_BIN + "/" + c, "#!/bin/sh\n# Synopsys %s (simulated by dc_shell_sim)\n" % c, exe)
        fs.add_vfile(PT_BIN + "/pt_shell", "#!/bin/sh\n# Synopsys PrimeTime (simulated by dc_shell_sim)\n", exe)
        fs.add_vfile(FM_BIN + "/fm_shell", "#!/bin/sh\n# Synopsys Formality (simulated by dc_shell_sim)\n", exe)
        fs.add_vfile("/tools/modulefiles/pt/" + SYN_VER, "#%Module (simulated)\n")
        fs.add_vfile("/tools/modulefiles/fm/" + SYN_VER, "#%Module (simulated)\n")
        fs.add_vfile(SYN_ROOT + "/admin/setup/.synopsys_dc.setup", dcm.SYSTEM_SETUP)
        for f in ("gtech.db", "standard.sldb", "dw_foundation.sldb", "generic.sdb"):
            fs.add_vfile(SYN_ROOT + "/libraries/syn/" + f, "<binary Synopsys db file : %s (simulated)>\n" % f)
        fs.add_vfile(SYN_ROOT + "/dw/syn_ver/README", "DesignWare synthesis models (simulated)\n")
        fs.add_vfile(SYN_ROOT + "/dw/sim_ver/README", "DesignWare simulation models (simulated)\n")
        fs.add_vfile(SYN_ROOT + "/doc/README", "dc_shell_sim : man <command> 또는 dc_shell> help 를 사용하세요.\n")
        for dbf, l in dcm.LIBS.items():
            fs.add_vfile(LIB_ROOT + "/db/" + dbf, "<binary Synopsys db file : %s (simulated)>\n" % dbf)
            fs.add_vfile(LIB_ROOT + "/lib/" + dbf.replace(".db", ".lib"), (lambda lib=l: dcm.liberty_text(lib)))
        fs.add_vfile(LIB_ROOT + "/doc/README.txt", dcm.LIB_README)
        libroot = os.path.join(self.sim_root, "libs")
        if os.path.isdir(libroot):
            for nm in sorted(os.listdir(libroot)):
                real = os.path.join(libroot, nm)
                if not os.path.isdir(real) or nm == "edu_lib":
                    continue
                vp = "/tools/lib/" + nm
                fs.add_mount(vp, real, False, "lib")
                ldir = os.path.join(real, "lib")
                if os.path.isdir(ldir):
                    for f in sorted(os.listdir(ldir)):
                        if f.lower().endswith(".lib"):
                            fs.add_vfile(vp + "/db/" + f[:-4] + ".db",
                                         "<binary Synopsys db file compiled from ../lib/%s (simulated)>\n" % f)
        fs.add_vfile("/tools/modulefiles/syn/" + SYN_VER, "#%Module (simulated)\n")
        fs.add_vfile("/tools/modulefiles/lib/edu_lib", "#%Module (simulated)\n")

    def _init_home(self):
        for rel, content in TEMPLATES.items():
            real = os.path.join(self.home_real, *rel.split("/"))
            if os.path.exists(real):
                continue
            os.makedirs(os.path.dirname(real), exist_ok=True)
            with open(real, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        for d in ("projects/demo/reports", "projects/demo/outputs", "projects/sky130_demo/reports",
                  "projects/sky130_demo/outputs"):
            os.makedirs(os.path.join(self.home_real, *d.split("/")), exist_ok=True)

    def _upgrade_home(self):
        """새 버전에서 추가된 예제 프로젝트만 한 번 생성 (기존 파일은 건드리지 않음)"""
        mk = os.path.join(self.home_real, ".sim_template_version")
        try:
            with open(mk, encoding="utf-8") as f:
                v = int(f.read().strip() or 0)
        except (OSError, ValueError):
            v = 0
        if v >= TEMPLATE_VERSION:
            return
        prefixes = tuple(p for ver, ps in TEMPLATE_NEW.items() if ver > v for p in ps)
        for rel, content in TEMPLATES.items():
            if not rel.startswith(prefixes):
                continue
            proj = os.path.join(self.home_real, *rel.split("/")[:2])
            if not rel.startswith("projects/sky130_demo/") and not os.path.isdir(proj):
                continue
            real = os.path.join(self.home_real, *rel.split("/"))
            if os.path.exists(real):
                continue
            os.makedirs(os.path.dirname(real), exist_ok=True)
            with open(real, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        for d in ("projects/sky130_demo/reports", "projects/sky130_demo/outputs"):
            os.makedirs(os.path.join(self.home_real, *d.split("/")), exist_ok=True)
        try:
            with open(mk, "w", encoding="utf-8") as f:
                f.write(str(TEMPLATE_VERSION))
        except OSError:
            pass

    def _load_history(self):
        p = os.path.join(self.home_real, ".bash_history")
        try:
            with open(p, encoding="utf-8") as f:
                self.history = [l.rstrip("\n") for l in f if l.strip()][-1000:]
        except OSError:
            self.history = []
        try:
            import readline
            for h in self.history:
                readline.add_history(h)
        except Exception:
            pass

    def _save_history_line(self, line):
        try:
            with open(os.path.join(self.home_real, ".bash_history"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ---------------- 명령 표 ----------------
    def _bin_names(self, d):
        if d == "/usr/bin":
            return [n for n, (fn, loc) in self._raw_cmds().items() if loc == "/usr/bin"]
        return []

    def _raw_cmds(self):
        B = "builtin"
        U = "/usr/bin"
        S = SIM_BIN
        T = SYN_BIN
        m = OrderedDict()
        for name, loc in [
            ("cd", B), ("pwd", B), ("echo", B), ("export", B), ("setenv", B), ("unsetenv", B), ("unset", B),
            ("alias", B), ("unalias", B), ("history", B), ("source", B), (".", B), ("exit", B), ("logout", B),
            ("type", B), ("help", B), ("module", B), ("printf", B), ("true", B), ("false", B),
            ("ls", U), ("ll", U), ("mkdir", U), ("rmdir", U), ("rm", U), ("cp", U), ("mv", U), ("touch", U),
            ("cat", U), ("head", U), ("tail", U), ("less", U), ("more", U), ("grep", U), ("egrep", U), ("find", U),
            ("wc", U), ("sort", U), ("uniq", U), ("cut", U), ("diff", U), ("tee", U), ("tree", U), ("clear", U),
            ("whoami", U), ("hostname", U), ("uname", U), ("date", U), ("env", U), ("printenv", U), ("which", U),
            ("man", U), ("bash", U), ("sh", U), ("du", U), ("df", U), ("ps", U), ("free", U), ("uptime", U),
            ("chmod", U), ("ln", U), ("basename", U), ("dirname", U), ("file", U), ("sleep", U), ("id", U),
            ("groups", U), ("realpath", U), ("vi", U), ("vim", U), ("gvim", U), ("nano", U), ("gedit", U),
            ("emacs", U), ("code", U), ("notepad", U), ("xdg-open", U),
            ("modfind", S), ("newproj", S), ("restore_demo", S), ("mount", S), ("umount", S), ("winpath", S), ("explorer", S),
            ("dc_shell", T), ("dc_shell-t", T), ("design_vision", T),
            ("pt_shell", PT_BIN), ("fm_shell", FM_BIN),
        ]:
            fn = getattr(self, "b_" + {".": "source", "dc_shell-t": "dc_shell", "xdg-open": "explorer",
                                         "egrep": "grep", "ll": "ll", "logout": "exit", "printenv": "env",
                                         "vim": "vi", "gvim": "vi", "nano": "vi", "gedit": "vi", "emacs": "vi",
                                         "code": "vi", "notepad": "vi", "more": "less", "sh": "bash",
                                         "setenv": "setenv", "unsetenv": "unset"}.get(name, name).replace("-", "_"))
            m[name] = (fn, loc)
        return m

    def _build_cmds(self):
        return self._raw_cmds()

    def lookup(self, name):
        if name in self.cmds and self.cmds[name][1] == "builtin":
            return self.cmds[name][0]
        if "/" in name:
            ap = self.abs(name)
            st = self.fs.stat(ap)
            if not st:
                return None
            base = ap.rsplit("/", 1)[1]
            d = ap.rsplit("/", 1)[0] or "/"
            if base in self.cmds and self.cmds[base][1] in (d,):
                return self.cmds[base][0]
            if st["kind"] == "file" and (ap.endswith((".sh", ".bash", ".csh")) or
                                         self.fs.read_text(ap).startswith("#!")):
                return lambda a, i, o, e, p=ap: self.run_script_file(p, a, o, e)
            return "noexec"
        for d in self.env.get("PATH", "").split(":"):
            d = d.rstrip("/") or "/"
            for n, (fn, loc) in self.cmds.items():
                if n == name and loc == d:
                    return fn
        return None

    # ---------------- 유틸 ----------------
    def abs(self, p):
        return self.fs.abspath(p, self.cwd)

    def disp(self, ap):
        h = self.fs.home
        if ap == h:
            return "~"
        if ap.startswith(h + "/"):
            return "~" + ap[len(h):]
        return ap

    def prompt(self, rl=False):
        base = "~" if self.cwd == self.fs.home else (self.cwd.rsplit("/", 1)[1] or "/")
        if not (USE_COLOR and self.stdout.tty) or (rl and IS_WIN):
            return "[%s@%s %s]$ " % (self.user, HOSTNAME, base)
        a, b = ("\001", "\002") if rl else ("", "")
        g = a + "\033[1;32m" + b
        bl = a + "\033[1;34m" + b
        r = a + "\033[0m" + b
        return "%s[%s@%s%s %s%s%s%s]$%s " % (g, self.user, HOSTNAME, r, bl, base, r, g, r)

    def motd(self):
        o = self.stdout
        last = os.path.join(self.home_real, ".lastlogin")
        try:
            with open(last, encoding="utf-8") as f:
                prev = f.read().strip()
        except OSError:
            prev = ""
        if prev:
            o.write("Last login: %s from 10.10.1.%d\n" % (prev, 20 + len(prev) % 50))
        try:
            with open(last, "w", encoding="utf-8") as f:
                f.write(datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Y"))
        except OSError:
            pass
        pc = self.fs.to_virtual(os.path.dirname(self.sim_root)) or "/mnt/c/..."
        lines = [
            "=" * 78,
            "  %s  |  CentOS Linux 7 (가상)  |  Synopsys Design Compiler 학습 환경" % HOSTNAME,
            "-" * 78,
            "  * 진짜 리눅스가 아니라 dc_shell_sim 이 흉내 내는 가상 터미널입니다.",
            "  * dc_shell             Design Compiler 실행  (dc_shell> exit 로 돌아오기)",
            "  * cd ~/projects/demo   예제 → dc_shell -f scripts/run_dc.tcl | tee dc.log",
            "  * 실제 PC 폴더(읽기 전용):  cd %s" % pc,
            "  * 실제 공개 라이브러리: cd ~/projects/sky130_demo  (SkyWater SKY130 tt/ss/ff)",
            "  * modfind <폴더>       .v 모듈 찾기     newproj <이름>  새 연습 프로젝트",
            "  * help  명령 목록      cat ~/README_실습.txt  실습 안내",
            "=" * 78,
        ]
        for l in lines:
            o.write(C(o, "36", l) + "\n")

    # ======================================================================
    # 파서
    # ======================================================================
    OPS = ["2>&1", "&>", "&&", "||", ">>", "2>", "|", ";", ">", "<", "&"]

    def expand_var(self, s, i):
        n = len(s)
        if i + 1 >= n:
            return "$", i + 1
        c = s[i + 1]
        if c == "{":
            j = s.find("}", i + 2)
            if j < 0:
                return "$", i + 1
            return self.env.get(s[i + 2:j], ""), j + 1
        if c == "?":
            return str(self.status), i + 2
        if c == "$":
            return "2101", i + 2
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*|\d", s[i + 1:])
        if m:
            return self.env.get(m.group(0), ""), i + 1 + m.end()
        return "$", i + 1

    def split_segments(self, line):
        """따옴표 밖의 ; && || 로 나눔 -> [(connector, text)]"""
        segs, cur, conn = [], [], None
        i, n = 0, len(line)
        q = None
        while i < n:
            c = line[i]
            if q:
                cur.append(c)
                if c == "\\" and q == '"' and i + 1 < n:
                    cur.append(line[i + 1])
                    i += 2
                    continue
                if c == q:
                    q = None
                i += 1
                continue
            if c in "'\"":
                q = c
                cur.append(c)
                i += 1
                continue
            if c == "\\" and i + 1 < n:
                cur.append(line[i:i + 2])
                i += 2
                continue
            if c == "#" and (not cur or "".join(cur)[-1:] in " \t"):
                break
            op = "&&" if line.startswith("&&", i) else "||" if line.startswith("||", i) else ";" if c == ";" else None
            if op:
                text = "".join(cur).strip()
                if not text:
                    raise ParseError("syntax error near unexpected token `%s'" % op)
                segs.append((conn, text))
                conn = op if op != ";" else None
                cur = []
                i += len(op)
                continue
            cur.append(c)
            i += 1
        if q:
            raise ParseError("unexpected EOF while looking for matching `%s'" % q)
        text = "".join(cur).strip()
        if text:
            segs.append((conn, text))
        elif conn:
            raise ParseError("syntax error: unexpected end of file")
        return segs

    def tokenize(self, line):
        toks = []
        cur = []
        st = {"q": False, "has": False}
        n = len(line)
        i = 0

        def push():
            if st["has"]:
                toks.append(("w", "".join(cur), st["q"]))
            del cur[:]
            st["q"] = False
            st["has"] = False
        while i < n:
            c = line[i]
            if c in " \t\r\n":
                push()
                i += 1
                continue
            if c == "#" and not st["has"]:
                break
            if c == "\\" and i + 1 < n:
                cur.append(line[i + 1])
                st["has"] = True
                st["q"] = True
                i += 2
                continue
            if c == "'":
                j = line.find("'", i + 1)
                if j < 0:
                    raise ParseError("unexpected EOF while looking for matching `''")
                cur.append(line[i + 1:j])
                st["has"] = True
                st["q"] = True
                i = j + 1
                continue
            if c == '"':
                j = i + 1
                buf = []
                while j < n and line[j] != '"':
                    if line[j] == "\\" and j + 1 < n and line[j + 1] in '"\\$`':
                        buf.append(line[j + 1])
                        j += 2
                        continue
                    if line[j] == "$":
                        v, j = self.expand_var(line, j)
                        buf.append(v)
                        continue
                    buf.append(line[j])
                    j += 1
                if j >= n:
                    raise ParseError("unexpected EOF while looking for matching `\"'")
                cur.append("".join(buf))
                st["has"] = True
                st["q"] = True
                i = j + 1
                continue
            if c == "$":
                v, i = self.expand_var(line, i)
                cur.append(v)
                st["has"] = True
                continue
            if c == "~" and not st["has"] and (i + 1 == n or line[i + 1] in "/ \t;|&<>"):
                cur.append(self.env.get("HOME", self.fs.home))
                st["has"] = True
                i += 1
                continue
            hit = None
            for op in self.OPS:
                if line.startswith(op, i):
                    if op.startswith("2") and st["has"]:
                        continue
                    hit = op
                    break
            if hit:
                push()
                toks.append(("op", hit))
                i += len(hit)
                continue
            cur.append(c)
            st["has"] = True
            i += 1
        push()
        return toks

    def parse(self, toks):
        seq = []            # [(connector_before, [cmd,...], background)]
        pipeline = []
        cmd = {"words": [], "redirs": []}
        conn = None
        i = 0

        def end_cmd():
            if cmd["words"] or cmd["redirs"]:
                pipeline.append(dict(cmd))
            elif pipeline:
                raise ParseError("syntax error near unexpected token `|'")
        while i < len(toks):
            t = toks[i]
            if t[0] == "w":
                cmd["words"].append((t[1], t[2]))
                i += 1
                continue
            op = t[1]
            if op in (">", ">>", "<", "2>", "&>"):
                if i + 1 >= len(toks) or toks[i + 1][0] != "w":
                    raise ParseError("syntax error near unexpected token `newline'")
                cmd["redirs"].append((op, toks[i + 1][1]))
                i += 2
                continue
            if op == "2>&1":
                cmd["redirs"].append((op, None))
                i += 1
                continue
            if op == "|":
                end_cmd()
                cmd = {"words": [], "redirs": []}
                i += 1
                continue
            # ; && || &
            end_cmd()
            if pipeline:
                seq.append((conn, pipeline, op == "&"))
            elif op in ("&&", "||"):
                raise ParseError("syntax error near unexpected token `%s'" % op)
            pipeline = []
            cmd = {"words": [], "redirs": []}
            conn = op if op in ("&&", "||") else None
            i += 1
        end_cmd()
        if pipeline:
            seq.append((conn, pipeline, False))
        elif conn:
            raise ParseError("syntax error: unexpected end of file")
        return seq

    def glob(self, pattern):
        """상대/절대 패턴을 그대로 유지하며 확장"""
        absolute = pattern.startswith("/")
        segs = [s for s in pattern.split("/") if s != ""]
        results = [("/" if absolute else "", self.abs("/") if absolute else self.cwd)]
        for k, seg in enumerate(segs):
            nxt = []
            for shown, ap in results:
                if any(ch in seg for ch in "*?["):
                    try:
                        names = self.fs.listdir(ap)
                    except FSError:
                        continue
                    for nm in names:
                        if nm.startswith(".") and not seg.startswith("."):
                            continue
                        if fnmatch.fnmatchcase(nm, seg):
                            child = self.fs.abspath(nm, ap)
                            if k < len(segs) - 1 and not self.fs.isdir(child):
                                continue
                            nxt.append(((shown + "/" if shown and not shown.endswith("/") else shown) + nm, child))
                else:
                    child = self.fs.abspath(seg, ap)
                    nxt.append(((shown + "/" if shown and not shown.endswith("/") else shown) + seg, child))
            results = nxt
        out = sorted(set(s for s, ap in results if self.fs.exists(ap)))
        return out

    def expand_words(self, words):
        res = []
        for idx, (w, q) in enumerate(words):
            if idx == 0 and not q and w in self.aliases:
                res += [t[1] for t in self.tokenize(self.aliases[w]) if t[0] == "w"]
                continue
            if not q and any(ch in w for ch in "*?[") and w != "[":
                g = self.glob(w)
                res += g if g else [w]
            else:
                res.append(w)
        return res

    # ======================================================================
    # 실행
    # ======================================================================
    def run_line(self, line, out=None, err=None, cwd=None):
        out = out or self.stdout
        err = err or out
        saved = None
        if cwd is not None:
            saved = (self.cwd, dict(self.env))
            self.cwd = cwd
        try:
            try:
                segs = self.split_segments(line)
            except ParseError as e:
                err.write("bash: %s\n" % e)
                self.status = 2
                return 2
            for conn, text in segs:
                if conn == "&&" and self.status != 0:
                    continue
                if conn == "||" and self.status == 0:
                    continue
                try:
                    seq = self.parse(self.tokenize(text))      # 변수는 실행 직전에 확장
                except ParseError as e:
                    err.write("bash: %s\n" % e)
                    self.status = 2
                    return 2
                for _, pipeline, bg in seq:
                    if bg:
                        err.write("[sim] 백그라운드(&) 실행은 지원하지 않아 포그라운드로 실행합니다.\n")
                    self.status = self.exec_pipeline(pipeline, out, err)
            return self.status
        finally:
            if saved:
                self.cwd = saved[0]

    def exec_pipeline(self, cmds, out, err):
        stdin = None
        status = 0
        idx = 0
        while idx < len(cmds):
            cmd = cmds[idx]
            last = idx == len(cmds) - 1
            words = self.expand_words(cmd["words"])
            stage_out = out if last else BufOut()
            stage_err = err
            opened = []
            ok = True
            tee_files = None
            # dc_shell ... | tee [-a] file  → 실시간 tee
            if words and words[0] in ("dc_shell", "dc_shell-t") and idx == len(cmds) - 2:
                nw = self.expand_words(cmds[idx + 1]["words"])
                if nw and nw[0] == "tee":
                    fl, _, pos = flags(nw[1:])
                    tee_files = []
                    for p in pos:
                        try:
                            f = self.fs.open_write(self.abs(p), "a" in fl)
                            tee_files.append(f)
                            opened.append(FileOut(f))
                        except FSError as e:
                            err.write("tee: %s: %s\n" % (p, e))
                    stage_out = TeeOut(out, tee_files)
            for op, target in cmd["redirs"]:
                try:
                    if op in (">", ">>", "&>"):
                        tg = self.expand_words([(target, False)])
                        f = self.fs.open_write(self.abs(tg[0]), op == ">>")
                        fo = FileOut(f)
                        opened.append(fo)
                        stage_out = fo
                        if op == "&>":
                            stage_err = fo
                    elif op == "2>":
                        tg = self.expand_words([(target, False)])
                        f = self.fs.open_write(self.abs(tg[0]))
                        fo = FileOut(f)
                        opened.append(fo)
                        stage_err = fo
                    elif op == "2>&1":
                        stage_err = stage_out
                    elif op == "<":
                        tg = self.expand_words([(target, False)])
                        stdin = self.fs.read_text(self.abs(tg[0]))
                except FSError as e:
                    err.write("bash: %s: %s\n" % (target, e))
                    ok = False
            if ok and words:
                status = self.run_command(words, stdin, stage_out, stage_err)
            elif not ok:
                status = 1
            for fo in opened:
                fo.close()
            if tee_files is not None:
                idx += 2
                continue
            stdin = stage_out.getvalue() if isinstance(stage_out, BufOut) else ""
            idx += 1
        return status

    def run_command(self, words, stdin, out, err):
        name = words[0]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", name)
        if m:
            self.env[m.group(1)] = m.group(2)
            if len(words) == 1:
                return 0
            words = words[1:]
            name = words[0]
        fn = self.lookup(name)
        if fn is None:
            if name in self.cmds:
                err.write("bash: %s: command not found\n" % name)
                if self.cmds[name][1] in (SYN_BIN, PT_BIN, FM_BIN):
                    mod = {SYN_BIN: "syn", PT_BIN: "pt", FM_BIN: "fm"}[self.cmds[name][1]]
                    err.write(C(err, "36", "  [학습 힌트] 툴 환경이 로드되지 않았습니다. module load %s 을 실행하세요.\n" % mod))
                return 127
            err.write("bash: %s: command not found\n" % name)
            hints = {"python": "시뮬레이터 안에서는 python 을 지원하지 않습니다.",
                     "sed": "sed 는 지원하지 않습니다. grep / cut / head / tail 을 사용해 보세요.",
                     "awk": "awk 는 지원하지 않습니다. cut -d ' ' -f 2 등을 사용해 보세요.",
                     "dir": "리눅스에서는 ls 를 사용합니다.", "cls": "리눅스에서는 clear 를 사용합니다.",
                     "copy": "리눅스에서는 cp 를 사용합니다.", "del": "리눅스에서는 rm 을 사용합니다.",
                     "type": "", "lc_shell": "lc_shell 은 지원하지 않습니다.", "pt_shell": "pt_shell 은 지원하지 않습니다.",
                     "icc2_shell": "icc2_shell 은 지원하지 않습니다.", "vcs": "VCS 시뮬레이터는 지원하지 않습니다."}
            if hints.get(name):
                err.write(C(err, "36", "  [학습 힌트] %s\n" % hints[name]))
            return 127
        if fn == "noexec":
            err.write("bash: %s: Permission denied\n" % name)
            return 126
        try:
            r = fn(words[1:], stdin, out, err)
            return r if isinstance(r, int) else 0
        except FSError as e:
            err.write("%s: %s\n" % (name, e))
            return 1
        except KeyboardInterrupt:
            out.write("^C\n")
            return 130

    def run_script_file(self, ap, args, out, err):
        text = self.fs.read_text(ap)
        saved = {k: self.env.get(k) for k in [str(i) for i in range(10)]}
        self.env["0"] = ap
        for i, a in enumerate(args[:9], 1):
            self.env[str(i)] = a
        st = 0
        try:
            for line in text.splitlines():
                if line.strip() and not line.strip().startswith("#"):
                    st = self.run_line(line, out, err)
        except TermExit:
            pass
        finally:
            for k, v in saved.items():
                if v is None:
                    self.env.pop(k, None)
                else:
                    self.env[k] = v
        return st

    # ---------------- 메인 루프 ----------------
    def completer_setup(self):
        try:
            import readline
        except Exception:
            return False

        def comp(text, state):
            try:
                buf = readline.get_line_buffer()
                beg = readline.get_begidx()
                first = not buf[:beg].strip() or buf[:beg].rstrip().endswith(("|", ";", "&&"))
                if first and "/" not in text:
                    opts = sorted(set(list(self.cmds) + list(self.aliases)))
                    m = [c + " " for c in opts if c.startswith(text)]
                else:
                    d, _, pre = text.rpartition("/")
                    base = self.abs(d if d else ".") if (d or not text.startswith("/")) else "/"
                    if text.startswith("/") and not d:
                        base = "/"
                    m = []
                    for nm in self.fs.listdir(base):
                        if nm.startswith(pre) and (pre.startswith(".") or not nm.startswith(".")):
                            full = (d + "/" if d or text.startswith("/") else "") + nm
                            if text.startswith("/") and not d:
                                full = "/" + nm
                            m.append(full + ("/" if self.fs.isdir(self.fs.abspath(nm, base)) else ""))
                return m[state] if state < len(m) else None
            except Exception:
                return None
        readline.set_completer(comp)
        readline.set_completer_delims(" \t\n;|&><")
        try:
            if "libedit" in (readline.__doc__ or ""):
                readline.parse_and_bind("bind ^I rl_complete")
            else:
                readline.parse_and_bind("tab: complete")
        except Exception:
            pass
        return True

    # ---------------- 탭 자동완성 ----------------
    def complete_path(self, word, dirs_only=False, cwd=None):
        cwd = cwd or self.cwd
        w = word
        if w == "~":
            w = "~/"
        d, sep, pre = w.rpartition("/")
        if sep:
            base = self.fs.abspath(d if d else "/", cwd) if (d or w.startswith("/")) else cwd
            if d.startswith("~"):
                base = self.fs.abspath(d, cwd)
        else:
            base = cwd
        try:
            names = self.fs.listdir(base)
        except FSError:
            return []
        res = []
        for nm in names:
            if not nm.startswith(pre):
                continue
            if nm.startswith(".") and not pre.startswith("."):
                continue
            isdir = self.fs.isdir(self.fs.abspath(nm, base))
            if dirs_only and not isdir:
                continue
            res.append((d + "/" if sep else "") + nm + ("/" if isdir else ""))
        return res

    def complete(self, line, b, e, word):
        before = line[:b]
        segs = re.split(r"\|\||&&|[|;]", before)
        seg = segs[-1] if segs else ""
        toks = seg.split()
        cmdpos = not toks
        if word.startswith("$"):
            pre = word[1:].lstrip("{")
            return ["$" + k for k in self.env if k.startswith(pre) and not k.isdigit()]
        if cmdpos:
            if "/" in word or word.startswith(("~", ".")):
                return self.complete_path(word)
            names = set(n for n, (fn, loc) in self.cmds.items() if loc == "builtin" or self._which(n))
            names |= set(self.aliases)
            return [n for n in names if n.startswith(word)]
        cmd = toks[0]
        if cmd in ("cd", "pushd", "rmdir", "tree", "explorer", "modfind", "winpath") and not word.startswith("-"):
            return self.complete_path(word, dirs_only=(cmd in ("cd", "pushd", "rmdir")))
        if cmd == "module":
            if len(toks) == 1:
                return [x for x in ("avail", "load", "unload", "list", "purge") if x.startswith(word)]
            return [m for m in MODULES if m.startswith(word) or m.split("/")[0].startswith(word)]
        if cmd in ("man", "which", "type", "help"):
            return [n for n in self.cmds if n.startswith(word)]
        if cmd in ("export", "unset", "printenv", "env", "setenv"):
            return [k for k in self.env if k.startswith(word) and not k.isdigit()]
        if cmd in ("dc_shell", "pt_shell", "fm_shell", "dc_shell-t") and word.startswith("-"):
            return [x for x in ("-f", "-x", "-topographical_mode", "-no_home_init", "-no_local_init",
                                "-output_log_file", "-version") if x.startswith(word)]
        return self.complete_path(word)

    def hist_expand(self, line):
        if "!" not in line or line.strip().startswith(("echo '", "grep '")):
            return line, False

        def rep(m):
            tok = m.group(0)
            if tok == "!!":
                if not self.history:
                    raise ParseError("!!: event not found")
                return self.history[-1]
            if re.match(r"!\d+$", tok):
                k = int(tok[1:])
                if 1 <= k <= len(self.history):
                    return self.history[k - 1]
                raise ParseError("%s: event not found" % tok)
            pre = tok[1:]
            for h in reversed(self.history):
                if h.startswith(pre):
                    return h
            raise ParseError("%s: event not found" % tok)
        new = re.sub(r"!!|!\d+|![A-Za-z_][\w-]*", rep, line)
        return new, new != line

    def run(self):
        for m in list(MODULES):
            if not m.startswith("lib/"):
                self.b_module(["load", m], None, BufOut(), BufOut())
        self.motd()
        rc = os.path.join(self.home_real, ".bashrc")
        if os.path.exists(rc):
            self.run_script_file(self.fs.home + "/.bashrc", [], self.stdout, self.stdout)
        self.status = 0
        rl = False
        while True:
            try:
                if lineedit.available():
                    line = lineedit.read_line(self.prompt(False), self.complete, self.history)
                else:
                    rl = self.completer_setup()
                    line = input(self.prompt(rl))
            except EOFError:
                self.stdout.write("logout\n")
                break
            except KeyboardInterrupt:
                self.stdout.write("^C\n")
                continue
            if not line.strip():
                continue
            try:
                line, changed = self.hist_expand(line)
                if changed:
                    self.stdout.write(line + "\n")
            except ParseError as e:
                self.stdout.write("bash: %s\n" % e)
                continue
            self.history.append(line)
            self._save_history_line(line)
            try:
                self.run_line(line)
            except TermExit:
                self.stdout.write("logout\n")
                break
            except KeyboardInterrupt:
                self.stdout.write("^C\n")
            except Exception as e:
                self.stdout.write("bash: internal simulator error: %s\n" % e)

    # ======================================================================
    # 내장 명령
    # ======================================================================
    def b_cd(self, a, i, o, e):
        t = a[0] if a else self.env.get("HOME", self.fs.home)
        if t == "-":
            t = self.env.get("OLDPWD", self.cwd)
            o.write(self.disp(self.abs(t)) + "\n")
        ap = self.abs(t)
        st = self.fs.stat(ap)
        if not st:
            e.write("bash: cd: %s: No such file or directory\n" % t)
            return 1
        if st["kind"] != "dir":
            e.write("bash: cd: %s: Not a directory\n" % t)
            return 1
        try:
            self.fs.listdir(ap)
        except FSError as ex:
            e.write("bash: cd: %s: %s\n" % (t, ex))
            return 1
        self.env["OLDPWD"] = self.cwd
        self.cwd = ap
        self.env["PWD"] = ap
        return 0

    def b_pwd(self, a, i, o, e):
        o.write(self.cwd + "\n")

    def b_echo(self, a, i, o, e):
        nl, esc = True, False
        while a and a[0] in ("-n", "-e", "-ne", "-en", "-E"):
            if "n" in a[0]:
                nl = False
            if "e" in a[0][1:]:
                esc = True
            a = a[1:]
        s = " ".join(a)
        if esc:
            s = s.replace("\\n", "\n").replace("\\t", "\t")
        o.write(s + ("\n" if nl else ""))

    def b_printf(self, a, i, o, e):
        if not a:
            return 1
        fmt = a[0].replace("\\n", "\n").replace("\\t", "\t")
        vals = []
        for x in a[1:]:
            try:
                vals.append(int(x))
            except ValueError:
                try:
                    vals.append(float(x))
                except ValueError:
                    vals.append(x)
        try:
            o.write(fmt % tuple(vals) if vals else fmt.replace("%%", "%"))
        except Exception:
            o.write(fmt)

    def b_true(self, a, i, o, e):
        return 0

    def b_false(self, a, i, o, e):
        return 1

    def _refresh_opts(self):
        global HIGHLIGHT
        HIGHLIGHT = self.env.get("SIM_HIGHLIGHT", "1") not in ("0", "false", "off")

    def b_export(self, a, i, o, e):
        if not a or a == ["-p"]:
            for k, v in self.env.items():
                if not k.isdigit():
                    o.write('declare -x %s="%s"\n' % (k, v))
            return 0
        for x in a:
            if "=" in x:
                k, v = x.split("=", 1)
                self.env[k] = v
        self._refresh_opts()
        return 0

    def b_setenv(self, a, i, o, e):
        if not a:
            return self.b_env([], i, o, e)
        self.env[a[0]] = " ".join(a[1:])

    def b_unset(self, a, i, o, e):
        for x in a:
            self.env.pop(x, None)

    def b_env(self, a, i, o, e):
        if a:
            if a[0] in self.env:
                o.write(self.env[a[0]] + "\n")
                return 0
            return 1
        for k, v in self.env.items():
            if not k.isdigit():
                o.write("%s=%s\n" % (k, v))

    def b_alias(self, a, i, o, e):
        if not a:
            for k, v in self.aliases.items():
                o.write("alias %s='%s'\n" % (k, v))
            return 0
        for x in a:
            if "=" in x:
                k, v = x.split("=", 1)
                self.aliases[k] = v
            elif x in self.aliases:
                o.write("alias %s='%s'\n" % (x, self.aliases[x]))
            else:
                e.write("bash: alias: %s: not found\n" % x)
                return 1

    def b_unalias(self, a, i, o, e):
        for x in a:
            if x == "-a":
                self.aliases.clear()
            self.aliases.pop(x, None)

    def b_history(self, a, i, o, e):
        if a and a[0] == "-c":
            self.history = []
            try:
                open(os.path.join(self.home_real, ".bash_history"), "w").close()
            except OSError:
                pass
            return 0
        n = int(a[0]) if a and a[0].isdigit() else len(self.history)
        start = max(0, len(self.history) - n)
        for k in range(start, len(self.history)):
            o.write("%5d  %s\n" % (k + 1, self.history[k]))

    def b_source(self, a, i, o, e):
        if not a:
            e.write("bash: source: filename argument required\n")
            return 2
        ap = self.abs(a[0])
        if not self.fs.isfile(ap):
            e.write("bash: %s: No such file or directory\n" % a[0])
            return 1
        return self.run_script_file(ap, a[1:], o, e)

    def b_bash(self, a, i, o, e):
        if not a:
            e.write("[sim] 이미 bash 안입니다. 스크립트를 실행하려면: bash script.sh\n")
            return 0
        if a[0] == "-c" and len(a) > 1:
            return self.run_line(a[1], o, e)
        return self.b_source(a, i, o, e)

    def b_exit(self, a, i, o, e):
        raise TermExit()

    def b_type(self, a, i, o, e):
        st = 0
        for x in a:
            if x in self.aliases:
                o.write("%s is aliased to `%s'\n" % (x, self.aliases[x]))
            elif x in self.cmds and self.cmds[x][1] == "builtin":
                o.write("%s is a shell builtin\n" % x)
            else:
                p = self._which(x)
                if p:
                    o.write("%s is %s\n" % (x, p))
                else:
                    e.write("bash: type: %s: not found\n" % x)
                    st = 1
        return st

    def _which(self, x):
        for d in self.env.get("PATH", "").split(":"):
            d = d.rstrip("/") or "/"
            if x in self.cmds and self.cmds[x][1] == d:
                return d + "/" + x
        return None

    def b_which(self, a, i, o, e):
        st = 0
        for x in [y for y in a if not y.startswith("-")]:
            if x in self.aliases:
                o.write("alias %s='%s'\n" % (x, self.aliases[x]))
                p = self._which(self.aliases[x].split()[0])
                if p:
                    o.write("\t%s\n" % p)
                continue
            p = self._which(x)
            if p:
                o.write(p + "\n")
            else:
                e.write("/usr/bin/which: no %s in (%s)\n" % (x, self.env.get("PATH", "")))
                st = 1
        return st

    def b_help(self, a, i, o, e):
        o.write("\n")
        for g, items in LINUX_HELP.items():
            o.write(C(o, "1", "[%s]" % g) + "\n")
            for c, d in items:
                o.write("  %-46s %s\n" % (c, d))
            o.write("\n")
        o.write("  파이프 |, 리다이렉트 > >> < 2>&1, 연결 ; && ||, 와일드카드 * ?, $변수, ~ 지원\n")
        o.write("  자세한 설명: man <명령>\n\n")

    def b_man(self, a, i, o, e):
        if not a:
            e.write("What manual page do you want?\n")
            return 1
        x = a[0]
        if x in MAN:
            text = "%s(1)  dc_shell_sim manual\n\nNAME\n    %s\n\nUSAGE\n%s\n" % (
                x.upper(), x, "\n".join("    " + l for l in MAN[x].splitlines()))
            return self._page(text, o)
        for g, items in LINUX_HELP.items():
            for c, d in items:
                if re.split(r"[ /\[]", c)[0] == x or (" " + x + " ") in (" " + c.replace("/", " ") + " "):
                    o.write("%s - %s\n    사용법: %s\n" % (x, d, c))
                    return 0
        dcm = self.dcmod
        if x in dcm.HELP:
            g, s, u = dcm.HELP[x]
            o.write("%s  (dc_shell 명령)\n    %s\n    %s\n  ※ dc_shell 안에서 man %s 로도 볼 수 있습니다.\n" % (x, s, u, x))
            return 0
        e.write("No manual entry for %s\n" % x)
        return 16

    def b_module(self, a, i, o, e):
        sub = a[0] if a else "help"
        if sub == "avail":
            o.write("\n------------------------------ /tools/modulefiles ------------------------------\n")
            o.write("   " + "   ".join(m + ("(default)" if m.startswith("syn") else "") for m in MODULES) + "\n\n")
            return 0
        if sub == "list":
            if not self.loaded_modules:
                o.write("No Modulefiles Currently Loaded.\n")
            else:
                o.write("Currently Loaded Modulefiles:\n")
                for k, m in enumerate(self.loaded_modules, 1):
                    o.write("  %d) %s\n" % (k, m))
            return 0
        if sub in ("load", "add"):
            for x in a[1:]:
                full = next((m for m in MODULES if m == x or m.split("/")[0] == x), None)
                if not full:
                    e.write("ERROR: Unable to locate a modulefile for '%s'\n" % x)
                    return 1
                if full in self.loaded_modules:
                    continue
                info = MODULES[full]
                if info["path"]:
                    self.env["PATH"] = info["path"] + ":" + self.env.get("PATH", "")
                self.env.update(info["env"])
                self.loaded_modules.append(full)
            return 0
        if sub in ("unload", "rm"):
            for x in a[1:]:
                full = next((m for m in self.loaded_modules if m == x or m.split("/")[0] == x), None)
                if full:
                    info = MODULES[full]
                    if info["path"]:
                        self.env["PATH"] = ":".join(p for p in self.env.get("PATH", "").split(":") if p != info["path"])
                    for k in info["env"]:
                        self.env.pop(k, None)
                    self.loaded_modules.remove(full)
            return 0
        if sub == "purge":
            return self.b_module(["unload"] + list(self.loaded_modules), i, o, e)
        o.write("Usage: module avail | load <name> | unload <name> | list | purge\n")

    # ---------------- 파일 명령 ----------------
    def _entries(self, ap, all_, almost):
        names = self.fs.listdir(ap)
        if not (all_ or almost):
            names = [n for n in names if not n.startswith(".")]
        if all_:
            names = [".", ".."] + names
        return names

    def _fmt_name(self, o, name, st, F=False):
        if not st:
            return name
        if st["kind"] == "dir":
            s = C(o, "1;34", name)
            return s + ("/" if F else "")
        if "x" in st["mode"][1:4] and not name.endswith((".v", ".txt", ".tcl")):
            return C(o, "1;32", name) + ("*" if F else "")
        if name.endswith((".tar", ".gz", ".zip", ".tgz")):
            return C(o, "1;31", name)
        return name

    def _ls_block(self, o, items, long_, human, one, F):
        """items: [(display_name, stat)]"""
        if long_:
            for name, st in items:
                if not st:
                    o.write("?????????? ? ?    ?        ?            ? %s\n" % name)
                    continue
                size = hsize(st["size"]) if human else str(st["size"])
                own = st.get("owner", "root")
                grp = "eda" if own != "root" else "root"
                o.write("%s %2d %-6s %-5s %6s %s %s\n" % (st["mode"], 2 if st["kind"] == "dir" else 1, own, grp,
                                                         size, lsdate(st["mtime"]), self._fmt_name(o, name, st, F)))
            return
        if one or not o.tty:
            for name, st in items:
                o.write(self._fmt_name(o, name, st, F) + "\n")
            return
        width = shutil.get_terminal_size((100, 24)).columns
        n = len(items)
        if n == 0:
            return
        lens = [len(nm) + (1 if F else 0) for nm, _ in items]
        best = 1
        for cols in range(min(n, 12), 0, -1):
            rows = (n + cols - 1) // cols
            cw = []
            for c in range(cols):
                col = lens[c * rows:(c + 1) * rows]
                cw.append(max(col) + 2 if col else 0)
            if sum(cw) <= width:
                best = cols
                break
        rows = (n + best - 1) // best
        cw = [max(lens[c * rows:(c + 1) * rows] or [0]) + 2 for c in range(best)]
        for r in range(rows):
            line = []
            for c in range(best):
                k = c * rows + r
                if k < n:
                    nm, st = items[k]
                    pad = cw[c] - lens[k] if c < best - 1 else 0
                    line.append(self._fmt_name(o, nm, st, F) + " " * pad)
            o.write("".join(line).rstrip() + "\n")

    def b_ll(self, a, i, o, e):
        return self.b_ls(["-l"] + a, i, o, e)

    def b_ls(self, a, i, o, e):
        fl, _, pos = flags(a)
        long_ = "l" in fl
        all_ = "a" in fl
        almost = "A" in fl
        human = "h" in fl
        rec = "R" in fl
        one = "1" in fl
        tsort = "t" in fl
        rev = "r" in fl
        dflag = "d" in fl
        F = "F" in fl
        targets = pos or ["."]
        files, dirs, status = [], [], 0
        for t in targets:
            ap = self.abs(t)
            st = self.fs.stat(ap)
            if not st:
                e.write("ls: cannot access '%s': No such file or directory\n" % t)
                status = 2
                continue
            if st["kind"] == "dir" and not dflag:
                dirs.append((t, ap))
            else:
                files.append((t, st))

        def order(items):
            if tsort:
                items.sort(key=lambda x: -(x[1]["mtime"] if x[1] else 0))
            else:
                items.sort(key=lambda x: x[0].lower().lstrip("."))
            if rev:
                items.reverse()
            return items
        if files:
            self._ls_block(o, order(files), long_, human, one, F)
        queue = list(dirs)
        first = True
        multi = len(targets) > 1 or rec or files
        while queue:
            t, ap = queue.pop(0)
            try:
                names = self._entries(ap, all_, almost)
            except FSError as ex:
                e.write("ls: cannot open directory '%s': %s\n" % (t, ex))
                status = 2
                continue
            items = []
            for nm in names:
                cap = ap if nm == "." else (self.fs.abspath("..", ap) if nm == ".." else self.fs.abspath(nm, ap))
                items.append((nm, self.fs.stat(cap)))
            items = order(items)
            if multi:
                if not first or files:
                    o.write("\n")
                o.write("%s:\n" % t)
            first = False
            if long_:
                total = sum((it[1]["size"] if it[1] else 0) for it in items) // 1024
                o.write("total %d\n" % total)
            self._ls_block(o, items, long_, human, one, F)
            if rec:
                sub = [(t.rstrip("/") + "/" + nm, self.fs.abspath(nm, ap)) for nm, st in items
                       if st and st["kind"] == "dir" and nm not in (".", "..")]
                queue = sub + queue
        return status

    def b_mkdir(self, a, i, o, e):
        fl, _, pos = flags(a)
        if not pos:
            e.write("mkdir: missing operand\n")
            return 1
        st = 0
        for p in pos:
            try:
                self.fs.mkdir(self.abs(p), "p" in fl)
            except FSError as ex:
                e.write("mkdir: cannot create directory '%s': %s\n" % (p, ex))
                st = 1
        return st

    def b_rmdir(self, a, i, o, e):
        st = 0
        for p in a:
            try:
                self.fs.rmdir(self.abs(p))
            except FSError as ex:
                e.write("rmdir: failed to remove '%s': %s\n" % (p, ex))
                st = 1
        return st

    def b_rm(self, a, i, o, e):
        fl, _, pos = flags(a)
        r = "r" in fl or "R" in fl
        force = "f" in fl
        if not pos and not force:
            e.write("rm: missing operand\n")
            return 1
        st = 0
        for p in pos:
            ap = self.abs(p)
            if ap == "/":
                e.write("rm: it is dangerous to operate recursively on '/'\n")
                return 1
            s = self.fs.stat(ap)
            if not s:
                if not force:
                    e.write("rm: cannot remove '%s': No such file or directory\n" % p)
                    st = 1
                continue
            if s["kind"] == "dir" and not r:
                e.write("rm: cannot remove '%s': Is a directory\n" % p)
                st = 1
                continue
            try:
                self.fs.remove(ap, recursive=r)
            except FSError as ex:
                e.write("rm: cannot remove '%s': %s\n" % (p, ex))
                st = 1
        return st

    def _copy(self, src, dst, recursive, e, name="cp"):
        s = self.fs.stat(src)
        if s["kind"] == "dir":
            if not recursive:
                e.write("%s: -r not specified; omitting directory '%s'\n" % (name, self.disp(src)))
                return 1
            if not self.fs.exists(dst):
                self.fs.mkdir(dst)
            st = 0
            for nm in self.fs.listdir(src):
                st |= self._copy(self.fs.abspath(nm, src), self.fs.abspath(nm, dst), True, e, name)
            return st
        data = self.fs.read_bytes(src)
        f = self.fs.open_write(dst, binary=True)
        try:
            f.write(data)
        finally:
            f.close()
        return 0

    def b_cp(self, a, i, o, e):
        fl, _, pos = flags(a)
        if len(pos) < 2:
            e.write("cp: missing destination file operand\n")
            return 1
        r = "r" in fl or "R" in fl or "a" in fl
        dst = self.abs(pos[-1])
        srcs = pos[:-1]
        dst_isdir = self.fs.isdir(dst)
        if len(srcs) > 1 and not dst_isdir:
            e.write("cp: target '%s' is not a directory\n" % pos[-1])
            return 1
        st = 0
        for s in srcs:
            sap = self.abs(s)
            if not self.fs.exists(sap):
                e.write("cp: cannot stat '%s': No such file or directory\n" % s)
                st = 1
                continue
            target = self.fs.abspath(sap.rsplit("/", 1)[1], dst) if dst_isdir else dst
            if target == sap:
                e.write("cp: '%s' and '%s' are the same file\n" % (s, pos[-1]))
                st = 1
                continue
            try:
                st |= self._copy(sap, target, r, e)
            except FSError as ex:
                e.write("cp: cannot create '%s': %s\n" % (self.disp(target), ex))
                st = 1
        return st

    def b_mv(self, a, i, o, e):
        fl, _, pos = flags(a)
        if len(pos) < 2:
            e.write("mv: missing destination file operand\n")
            return 1
        dst = self.abs(pos[-1])
        dst_isdir = self.fs.isdir(dst)
        st = 0
        for s in pos[:-1]:
            sap = self.abs(s)
            if not self.fs.exists(sap):
                e.write("mv: cannot stat '%s': No such file or directory\n" % s)
                st = 1
                continue
            target = self.fs.abspath(sap.rsplit("/", 1)[1], dst) if dst_isdir else dst
            try:
                self.fs.rename(sap, target)
            except FSError as ex:
                e.write("mv: cannot move '%s' to '%s': %s\n" % (s, pos[-1], ex))
                st = 1
        return st

    def b_touch(self, a, i, o, e):
        st = 0
        for p in [x for x in a if not x.startswith("-")]:
            try:
                self.fs.touch(self.abs(p))
            except FSError as ex:
                e.write("touch: cannot touch '%s': %s\n" % (p, ex))
                st = 1
        return st

    def _read_inputs(self, pos, stdin, e, name):
        """[(label, text)]"""
        if not pos:
            return [("(standard input)", stdin or "")]
        res = []
        for p in pos:
            if p == "-":
                res.append(("(standard input)", stdin or ""))
                continue
            try:
                res.append((p, self.fs.read_text(self.abs(p))))
            except FSError as ex:
                e.write("%s: %s: %s\n" % (name, p, ex))
                res.append((p, None))
        return res

    def b_cat(self, a, i, o, e):
        fl, _, pos = flags(a)
        st = 0
        k = 0
        for label, txt in self._read_inputs(pos, i, e, "cat"):
            if txt is None:
                st = 1
                continue
            if "n" in fl:
                for line in txt.splitlines():
                    k += 1
                    o.write("%6d\t%s\n" % (k, line))
            else:
                o.write(txt)
        return st

    def _num_arg(self, a, default=10):
        n = default
        rest = []
        k = 0
        while k < len(a):
            x = a[k]
            if re.match(r"^-\d+$", x):
                n = int(x[1:])
            elif x == "-n" and k + 1 < len(a):
                n = int(a[k + 1].lstrip("+-") or default)
                k += 1
            elif x.startswith("-n") and x[2:].lstrip("+-").isdigit():
                n = int(x[2:].lstrip("+-"))
            elif x.startswith("-"):
                pass
            else:
                rest.append(x)
            k += 1
        return n, rest

    def b_head(self, a, i, o, e):
        n, pos = self._num_arg(a)
        ins = self._read_inputs(pos, i, e, "head")
        for k, (label, txt) in enumerate(ins):
            if txt is None:
                continue
            if len(ins) > 1:
                o.write(("\n" if k else "") + "==> %s <==\n" % label)
            for line in txt.splitlines()[:n]:
                o.write(line + "\n")

    def b_tail(self, a, i, o, e):
        n, pos = self._num_arg(a)
        ins = self._read_inputs(pos, i, e, "tail")
        for k, (label, txt) in enumerate(ins):
            if txt is None:
                continue
            if len(ins) > 1:
                o.write(("\n" if k else "") + "==> %s <==\n" % label)
            lines = txt.splitlines()
            for line in lines[-n:] if n else []:
                o.write(line + "\n")

    def _page(self, text, o):
        lines = text.splitlines()
        if not (getattr(o, "screen", False) and getattr(o, "tty", False) and sys.stdin.isatty()):
            o.write(text if text.endswith("\n") or not text else text + "\n")
            return 0
        h = max(5, shutil.get_terminal_size((100, 30)).lines - 2)
        k = 0
        while k < len(lines):
            for line in lines[k:k + h]:
                o.write(line + "\n")
            k += h
            if k >= len(lines):
                break
            try:
                ans = input(C(o, "7", "--More--(%d%%) [Enter: 다음, q: 종료]" % (100 * k // len(lines))))
            except (EOFError, KeyboardInterrupt):
                o.write("\n")
                break
            if ans.strip().lower().startswith("q"):
                break
        return 0

    def b_less(self, a, i, o, e):
        fl, _, pos = flags(a)
        text = ""
        st = 0
        for label, txt in self._read_inputs(pos, i, e, "less"):
            if txt is None:
                st = 1
                continue
            text += txt
        if "N" in fl:
            text = "\n".join("%6d %s" % (k + 1, l) for k, l in enumerate(text.splitlines()))
        self._page(text, o)
        return st

    def _grep_re(self, pat, fl):
        if "F" in fl:
            p = re.escape(pat)
        else:
            p = pat
            if "E" not in fl:
                out, k = [], 0
                while k < len(pat):
                    ch = pat[k]
                    if ch == "\\" and k + 1 < len(pat) and pat[k + 1] in "|(){}+?":
                        out.append(pat[k + 1])
                        k += 2
                        continue
                    out.append("\\" + ch if ch in "|(){}+?" else ch)
                    k += 1
                p = "".join(out)
        if "w" in fl:
            p = r"\b(?:%s)\b" % p
        try:
            return re.compile(p, re.I if "i" in fl else 0)
        except re.error as ex:
            raise FSError("Invalid regular expression (%s)" % ex)

    def _walk_files(self, ap, shown, e, maxdepth=50, depth=0):
        st = self.fs.stat(ap)
        if not st:
            return
        if st["kind"] == "file":
            yield shown, ap
            return
        if depth >= maxdepth:
            return
        try:
            names = self.fs.listdir(ap)
        except FSError as ex:
            e.write("%s: %s\n" % (shown, ex))
            return
        for nm in names:
            yield from self._walk_files(self.fs.abspath(nm, ap), (shown.rstrip("/") + "/" + nm) if shown != "" else nm,
                                        e, maxdepth, depth + 1)

    def b_grep(self, a, i, o, e):
        fl, vals, pos = flags(a, with_val="emABC")
        if "e" in vals:
            pat = vals["e"]
        elif pos:
            pat = pos.pop(0)
        else:
            e.write("Usage: grep [OPTION]... PATTERN [FILE]...\n")
            return 2
        rx = self._grep_re(pat, fl)
        rec = "r" in fl or "R" in fl
        if rec and not pos:
            pos = ["."]
        sources = []
        if not pos:
            sources = [("(standard input)", ("text", i or ""))]
        else:
            for p in pos:
                ap = self.abs(p)
                if not self.fs.exists(ap):
                    if "s" not in fl:
                        e.write("grep: %s: No such file or directory\n" % p)
                    continue
                if self.fs.isdir(ap):
                    if not rec:
                        e.write("grep: %s: Is a directory\n" % p)
                        continue
                    for shown, fap in self._walk_files(ap, p, e):
                        sources.append((shown, ("file", fap)))
                else:
                    sources.append((p, ("file", ap)))
        show_name = ("H" in fl or rec or len(sources) > 1) and "h" not in fl
        found = False
        maxc = int(vals["m"]) if "m" in vals and vals["m"].isdigit() else None
        for label, src in sources:
            if src[0] == "file":
                try:
                    data = self.fs.read_bytes(src[1])
                except FSError as ex:
                    e.write("grep: %s: %s\n" % (label, ex))
                    continue
                if b"\0" in data[:2048]:
                    txt = decode_text(data)
                    if rx.search(txt):
                        o.write("Binary file %s matches\n" % label)
                        found = True
                    continue
                txt = decode_text(data)
            else:
                txt = src[1]
            cnt = 0
            ca = int(vals.get("A") or vals.get("C") or 0) if (vals.get("A") or vals.get("C") or "0").isdigit() else 0
            cb = int(vals.get("B") or vals.get("C") or 0) if (vals.get("B") or vals.get("C") or "0").isdigit() else 0
            if (ca or cb) and not ({"l", "c", "q"} & fl):
                lines = txt.splitlines()
                hits = [k for k, l in enumerate(lines) if bool(rx.search(l)) != ("v" in fl)]
                if maxc:
                    hits = hits[:maxc]
                show = set()
                for k in hits:
                    show.update(range(max(0, k - cb), min(len(lines), k + ca + 1)))
                last = None
                for k in sorted(show):
                    if last is not None and k != last + 1:
                        o.write(C(o, "36", "--") + "\n")
                    sep = ":" if k in hits else "-"
                    pre = (C(o, "35", label) + C(o, "36", sep)) if show_name else ""
                    if "n" in fl:
                        pre += C(o, "32", str(k + 1)) + C(o, "36", sep)
                    line = lines[k]
                    if k in hits and "v" not in fl and getattr(o, "tty", False) and USE_COLOR:
                        line = rx.sub(lambda mm: C(o, "1;31", mm.group(0)), line)
                    o.write(pre + line + "\n")
                    last = k
                if hits:
                    found = True
                continue
            for ln, line in enumerate(txt.splitlines(), 1):
                m = rx.search(line)
                hit = bool(m) != ("v" in fl)
                if not hit:
                    continue
                found = True
                cnt += 1
                if "l" in fl or "q" in fl or "c" in fl:
                    if maxc and cnt >= maxc:
                        break
                    continue
                pre = ""
                if show_name:
                    pre += C(o, "35", label) + C(o, "36", ":")
                if "n" in fl:
                    pre += C(o, "32", str(ln)) + C(o, "36", ":")
                if m and "v" not in fl and getattr(o, "tty", False) and USE_COLOR:
                    line = rx.sub(lambda mm: C(o, "1;31", mm.group(0)), line)
                o.write(pre + line + "\n")
                if maxc and cnt >= maxc:
                    break
            if "c" in fl:
                o.write(("%s:" % label if show_name else "") + "%d\n" % cnt)
            elif "l" in fl and cnt:
                o.write(label + "\n")
            if "q" in fl and found:
                return 0
        return 0 if found else 1

    def b_find(self, a, i, o, e):
        paths = []
        k = 0
        while k < len(a) and not a[k].startswith("-") and a[k] not in ("!", "("):
            paths.append(a[k])
            k += 1
        paths = paths or ["."]
        conds = []
        maxd, mind = 50, 0
        while k < len(a):
            x = a[k]
            v = a[k + 1] if k + 1 < len(a) else ""
            if x in ("-name", "-iname", "-type", "-path"):
                conds.append((x, v))
                k += 2
            elif x == "-maxdepth":
                maxd = int(v)
                k += 2
            elif x == "-mindepth":
                mind = int(v)
                k += 2
            elif x in ("-print", "-print0"):
                k += 1
            else:
                e.write("find: unknown predicate '%s'\n" % x)
                return 1

        def ok(shown, nm, st):
            for c, v in conds:
                if c == "-name" and not fnmatch.fnmatchcase(nm, v):
                    return False
                if c == "-iname" and not fnmatch.fnmatch(nm.lower(), v.lower()):
                    return False
                if c == "-type" and ((v == "f" and st["kind"] != "file") or (v == "d" and st["kind"] != "dir")):
                    return False
                if c == "-path" and not fnmatch.fnmatchcase(shown, v):
                    return False
            return True
        st0 = 0

        def walk(ap, shown, depth):
            nonlocal st0
            st = self.fs.stat(ap)
            if not st:
                return
            nm = shown.rstrip("/").rsplit("/", 1)[-1] if depth else shown
            if depth >= mind and ok(shown, nm if depth else ap.rsplit("/", 1)[-1], st):
                o.write(shown + "\n")
            if st["kind"] == "dir" and depth < maxd:
                try:
                    names = self.fs.listdir(ap)
                except FSError as ex:
                    e.write("find: '%s': %s\n" % (shown, ex))
                    st0 = 1
                    return
                for c in names:
                    walk(self.fs.abspath(c, ap), shown.rstrip("/") + "/" + c, depth + 1)
        for p in paths:
            ap = self.abs(p)
            if not self.fs.exists(ap):
                e.write("find: '%s': No such file or directory\n" % p)
                st0 = 1
                continue
            walk(ap, p, 0)
        return st0

    def b_wc(self, a, i, o, e):
        fl, _, pos = flags(a)
        sel = [c for c in "lwc" if c in fl] or ["l", "w", "c"]
        tot = [0, 0, 0]
        ins = self._read_inputs(pos, i, e, "wc")
        for label, txt in ins:
            if txt is None:
                continue
            v = [txt.count("\n"), len(txt.split()), len(txt.encode("utf-8"))]
            tot = [x + y for x, y in zip(tot, v)]
            nums = " ".join("%7d" % v["lwc".index(c)] for c in sel)
            o.write(nums + ("" if label == "(standard input)" else " " + label) + "\n")
        if len(ins) > 1:
            o.write(" ".join("%7d" % tot["lwc".index(c)] for c in sel) + " total\n")

    def b_sort(self, a, i, o, e):
        fl, _, pos = flags(a)
        lines = []
        for _, txt in self._read_inputs(pos, i, e, "sort"):
            if txt:
                lines += txt.splitlines()
        if "n" in fl:
            def key(s):
                m = re.match(r"\s*(-?\d+(\.\d+)?)", s)
                return (float(m.group(1)) if m else 0.0, s)
            lines.sort(key=key)
        else:
            lines.sort(key=(lambda s: s.lower()) if "f" in fl else None)
        if "r" in fl:
            lines.reverse()
        if "u" in fl:
            lines = list(OrderedDict.fromkeys(lines))
        for l in lines:
            o.write(l + "\n")

    def b_uniq(self, a, i, o, e):
        fl, _, pos = flags(a)
        lines = []
        for _, txt in self._read_inputs(pos[:1], i, e, "uniq"):
            if txt:
                lines += txt.splitlines()
        groups = []
        for l in lines:
            if groups and groups[-1][0] == l:
                groups[-1][1] += 1
            else:
                groups.append([l, 1])
        for l, c in groups:
            if "d" in fl and c < 2:
                continue
            o.write(("%7d %s\n" % (c, l)) if "c" in fl else l + "\n")

    def b_cut(self, a, i, o, e):
        fl, vals, pos = flags(a, with_val="dfc")
        d = vals.get("d", "\t")
        sel = set()
        spec = vals.get("f") or vals.get("c") or ""
        for part in spec.split(","):
            if "-" in part:
                x, y = part.split("-", 1)
                sel.update(range(int(x or 1), int(y or 999) + 1))
            elif part.isdigit():
                sel.add(int(part))
        for _, txt in self._read_inputs(pos, i, e, "cut"):
            for l in (txt or "").splitlines():
                if "c" in vals:
                    o.write("".join(ch for k, ch in enumerate(l, 1) if k in sel) + "\n")
                else:
                    if d not in l:
                        if "s" not in fl:
                            o.write(l + "\n")
                        continue
                    fs_ = l.split(d)
                    o.write(d.join(f for k, f in enumerate(fs_, 1) if k in sel) + "\n")

    def b_diff(self, a, i, o, e):
        fl, _, pos = flags(a)
        if len(pos) != 2:
            e.write("diff: missing operand\n")
            return 2
        t1 = self.fs.read_text(self.abs(pos[0])).splitlines()
        t2 = self.fs.read_text(self.abs(pos[1])).splitlines()
        lines = list(difflib.unified_diff(t1, t2, pos[0], pos[1], lineterm=""))
        for l in lines:
            if l.startswith("+") and not l.startswith("+++"):
                l = C(o, "32", l)
            elif l.startswith("-") and not l.startswith("---"):
                l = C(o, "31", l)
            elif l.startswith("@@"):
                l = C(o, "36", l)
            o.write(l + "\n")
        return 1 if lines else 0

    def b_tee(self, a, i, o, e):
        fl, _, pos = flags(a)
        txt = i or ""
        st = 0
        for p in pos:
            try:
                f = self.fs.open_write(self.abs(p), "a" in fl)
                f.write(txt)
                f.close()
            except FSError as ex:
                e.write("tee: %s: %s\n" % (p, ex))
                st = 1
        o.write(txt)
        return st

    def b_tree(self, a, i, o, e):
        fl, vals, pos = flags(a, with_val="L")
        maxd = int(vals.get("L", "50") or 50)
        root = pos[0] if pos else "."
        ap = self.abs(root)
        if not self.fs.isdir(ap):
            e.write("%s [error opening dir]\n" % root)
            return 2
        o.write(C(o, "1;34", root) + "\n")
        cnt = [0, 0]

        def walk(ap, pre, depth):
            try:
                names = self.fs.listdir(ap)
            except FSError:
                return
            if "a" not in fl:
                names = [n for n in names if not n.startswith(".")]
            items = [(n, self.fs.stat(self.fs.abspath(n, ap))) for n in names]
            if "d" in fl:
                items = [x for x in items if x[1] and x[1]["kind"] == "dir"]
            for k, (n, st) in enumerate(items):
                lastc = k == len(items) - 1
                o.write(pre + ("└── " if lastc else "├── ") + self._fmt_name(o, n, st) + "\n")
                if st and st["kind"] == "dir":
                    cnt[0] += 1
                    if depth + 1 < maxd:
                        walk(self.fs.abspath(n, ap), pre + ("    " if lastc else "│   "), depth + 1)
                else:
                    cnt[1] += 1
        walk(ap, "", 0)
        o.write("\n%d directories%s\n" % (cnt[0], "" if "d" in fl else ", %d files" % cnt[1]))

    def b_clear(self, a, i, o, e):
        if getattr(o, "screen", False):
            if IS_WIN:
                os.system("cls")
            else:
                o.write("\033[2J\033[H")

    def b_whoami(self, a, i, o, e):
        o.write(self.user + "\n")

    def b_id(self, a, i, o, e):
        o.write("uid=1000(%s) gid=1000(eda) groups=1000(eda),1001(synopsys)\n" % self.user)

    def b_groups(self, a, i, o, e):
        o.write("eda synopsys\n")

    def b_hostname(self, a, i, o, e):
        o.write(HOSTNAME + "\n")

    def b_uname(self, a, i, o, e):
        fl, _, _ = flags(a)
        if "a" in fl:
            o.write("Linux %s 3.10.0-1160.el7.x86_64 #1 SMP Mon Oct 19 16:18:59 UTC 2020 x86_64 x86_64 x86_64 GNU/Linux\n" % HOSTNAME)
        elif "r" in fl:
            o.write("3.10.0-1160.el7.x86_64\n")
        elif "n" in fl:
            o.write(HOSTNAME + "\n")
        elif "m" in fl:
            o.write("x86_64\n")
        else:
            o.write("Linux\n")

    def b_date(self, a, i, o, e):
        d = datetime.datetime.now()
        if a and a[0].startswith("+"):
            try:
                o.write(d.strftime(a[0][1:].replace("%e", str(d.day))) + "\n")
                return 0
            except ValueError:
                pass
        o.write("%s %s %2d %s KST %d\n" % (d.strftime("%a"), d.strftime("%b"), d.day, d.strftime("%H:%M:%S"), d.year))

    def b_uptime(self, a, i, o, e):
        up = int(time.time() - self.fs.t0)
        o.write(" %s up %d min,  1 user,  load average: 0.08, 0.03, 0.01\n" % (datetime.datetime.now().strftime("%H:%M:%S"), up // 60))

    def b_ps(self, a, i, o, e):
        o.write("  PID TTY          TIME CMD\n 2101 pts/0    00:00:00 bash\n 2318 pts/0    00:00:00 ps\n")

    def b_free(self, a, i, o, e):
        o.write("              total        used        free      shared  buff/cache   available\n"
                "Mem:            62G        4.1G         51G        212M        6.8G         57G\n"
                "Swap:           15G          0B         15G\n")

    def b_df(self, a, i, o, e):
        o.write("Filesystem      Size  Used Avail Use% Mounted on\n")
        o.write("/dev/sda2       100G   38G   62G  38% /\n")
        o.write("nfs:/tools      2.0T  1.2T  800G  60% /tools\n")
        for vp, m in self.fs.mounts.items():
            if vp.startswith("/mnt") or vp.startswith("/home"):
                try:
                    u = shutil.disk_usage(m["real"])
                    o.write("%-15s %5s %5s %5s %3d%% %s\n" % ((m["real"][:15] if m["kind"] != "home" else "simhome"),
                                                          hsize(u.total), hsize(u.used), hsize(u.free),
                                                          100 * u.used // max(u.total, 1), vp))
                except OSError:
                    pass

    def b_du(self, a, i, o, e):
        fl, _, pos = flags(a)
        pos = pos or ["."]
        for p in pos:
            ap = self.abs(p)
            total = 0
            n = 0
            for shown, fap in self._walk_files(ap, p, e, maxdepth=20):
                st = self.fs.stat(fap)
                total += st["size"] if st else 0
                n += 1
                if n > 20000:
                    break
            o.write("%s\t%s\n" % (hsize(total) if "h" in fl else str(max(1, total // 1024)), p))

    def b_chmod(self, a, i, o, e):
        for p in [x for x in a[1:] if not x.startswith("-")]:
            if not self.fs.exists(self.abs(p)):
                e.write("chmod: cannot access '%s': No such file or directory\n" % p)
                return 1
        return 0

    def b_ln(self, a, i, o, e):
        e.write("ln: 시뮬레이터에서는 링크를 지원하지 않습니다. cp 를 사용하세요.\n")
        return 1

    def b_basename(self, a, i, o, e):
        if a:
            b = a[0].rstrip("/").rsplit("/", 1)[-1]
            if len(a) > 1 and b.endswith(a[1]):
                b = b[:-len(a[1])]
            o.write(b + "\n")

    def b_dirname(self, a, i, o, e):
        if a:
            d = a[0].rstrip("/").rsplit("/", 1)
            o.write((d[0] if len(d) > 1 and d[0] else ("/" if a[0].startswith("/") else ".")) + "\n")

    def b_realpath(self, a, i, o, e):
        for p in a or ["."]:
            o.write(self.abs(p) + "\n")

    def b_file(self, a, i, o, e):
        for p in a:
            ap = self.abs(p)
            st = self.fs.stat(ap)
            if not st:
                o.write("%s: cannot open (No such file or directory)\n" % p)
                continue
            if st["kind"] == "dir":
                kind = "directory"
            elif p.endswith((".v", ".sv", ".vh")):
                kind = "Verilog source, ASCII text"
            elif p.endswith(".tcl") or p.endswith("setup"):
                kind = "Tcl script, ASCII text"
            elif p.endswith(".db") or p.endswith(".sldb"):
                kind = "data (Synopsys db)"
            else:
                data = self.fs.read_bytes(ap)[:2048]
                kind = "data" if b"\0" in data else ("empty" if not data else "ASCII text")
            o.write("%s: %s\n" % (p, kind))

    def b_sleep(self, a, i, o, e):
        try:
            time.sleep(min(float(a[0]), 30))
        except (ValueError, IndexError):
            pass

    def b_vi(self, a, i, o, e):
        names = [x for x in a if not x.startswith("-") and not x.startswith("+")]
        if not names:
            e.write("[sim] 사용법: vi <파일>  (홈 폴더 안의 파일을 메모장으로 엽니다)\n")
            return 1
        p = names[0]
        ap = self.abs(p)
        st = self.fs.stat(ap)
        if st and st["kind"] == "dir":
            e.write('"%s" is a directory\n' % p)
            return 1
        m = self.fs.mount_of(ap)
        if not (m and m[2]):
            if st:
                o.write(C(o, "33", '"%s" [readonly] - 실제 PC/시스템 파일은 읽기 전용이라 보기만 합니다. '
                                   '수정하려면 cp 로 홈에 복사하세요.' % p) + "\n")
                return self._page(self.fs.read_text(ap), o)
            e.write('"%s" E212: Can\'t open file for writing (Read-only file system)\n' % p)
            return 1
        real = m[1]
        if not st:
            if not os.path.isdir(os.path.dirname(real)):
                e.write('"%s" E212: Can\'t open file for writing (No such directory)\n' % p)
                return 1
            open(real, "a", encoding="utf-8").close()
        ed = self.env.get("EDITOR", "notepad")
        o.write(C(o, "36", "[sim] '%s' 을(를) 편집기(%s)로 엽니다. 저장 후 편집기를 닫으면 터미널로 돌아옵니다..." % (p, ed)) + "\n")
        try:
            if IS_WIN:
                if ed in ("code",) and shutil.which("code"):
                    subprocess.call("code --wait \"%s\"" % real, shell=True)
                else:
                    subprocess.call([shutil.which(ed) or "notepad.exe", real])
            else:
                exe = shutil.which(ed) or shutil.which("nano") or shutil.which("vi")
                if not exe:
                    e.write("[sim] 편집기를 찾을 수 없습니다. 실제 파일 위치: %s\n" % real)
                    return 1
                subprocess.call([exe, real])
        except Exception as ex:
            e.write("[sim] 편집기 실행 실패: %s  (실제 파일 위치: %s)\n" % (ex, real))
            return 1
        return 0

    # ---------------- 시뮬레이터 전용 ----------------
    def b_mount(self, a, i, o, e):
        if not a:
            for vp, m in sorted(self.fs.mounts.items()):
                typ = {"home": "simhome", "tmp": "tmpfs", "drive": "drvfs", "user": "drvfs", "lib": "nfs"}[m["kind"]]
                o.write("%s on %s type %s (%s)\n" % (m["real"], vp, typ, "rw" if m["rw"] else "ro"))
            o.write("nfs:/tools on /tools type nfs (ro)   [virtual]\n")
            return 0
        real = a[0]
        if not os.path.isdir(real):
            e.write("mount: %s: 실제 PC 폴더가 아닙니다. 예) mount C:\\Users\\%s\\Documents\\rtl myrtl\n" % (real, self.user))
            return 32
        name = a[1] if len(a) > 1 else os.path.basename(os.path.normpath(real))
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", name).strip("_") or "pc"
        vp = "/mnt/" + name
        if vp in self.fs.mounts:
            e.write("mount: %s already mounted\n" % vp)
            return 32
        self.fs.add_mount(vp, os.path.abspath(real), False, "user")
        try:
            self._save_user_mounts()
        except OSError:
            pass
        o.write("%s -> %s  (읽기 전용으로 연결됨, 다음 실행 때도 유지)\n" % (real, vp))

    def b_umount(self, a, i, o, e):
        for x in a:
            vp = x if x.startswith("/") else "/mnt/" + x
            m = self.fs.mounts.get(vp)
            if not m or m["kind"] != "user":
                e.write("umount: %s: not mounted (또는 해제할 수 없는 기본 연결)\n" % x)
                return 32
            self.fs.remove_mount(vp)
            self._save_user_mounts()
            if self.cwd.startswith(vp):
                self.cwd = self.fs.home
        return 0

    def b_winpath(self, a, i, o, e):
        for p in a or ["."]:
            ap = self.abs(p)
            r = self.fs.real(ap)
            o.write("%s -> %s\n" % (ap, r if r else "(가상 경로: 실제 PC 파일 아님)"))

    def b_explorer(self, a, i, o, e):
        ap = self.abs(a[0] if a else ".")
        r = self.fs.real(ap)
        if not r or not os.path.exists(r):
            e.write("explorer: %s 는 가상 경로라 열 수 없습니다.\n" % ap)
            return 1
        if IS_WIN:
            try:
                os.startfile(r)
            except Exception as ex:
                e.write("explorer: %s\n" % ex)
                return 1
        else:
            o.write("실제 경로: %s\n" % r)

    def b_modfind(self, a, i, o, e):
        maxd = 8
        pos = []
        k = 0
        while k < len(a):
            if a[k] == "-maxdepth" and k + 1 < len(a):
                maxd = int(a[k + 1])
                k += 2
                continue
            pos.append(a[k])
            k += 1
        root = pos[0] if pos else "."
        ap = self.abs(root)
        if not self.fs.exists(ap):
            e.write("modfind: %s: No such file or directory\n" % root)
            return 1
        dcm = self.dcmod
        mods = []
        nfiles = 0
        o.write(C(o, "36", "[modfind] %s 아래 .v/.sv 파일 검색 중... (Ctrl+C 로 중단)" % root) + "\n")
        for shown, fap in self._walk_files(ap, root, BufOut(), maxdepth=maxd):
            if not fap.lower().endswith((".v", ".sv")) or "/WORK/" in fap:
                continue
            if fap.lower().endswith("_netlist.v"):
                continue
            nfiles += 1
            if nfiles > 2000:
                o.write("... 파일이 너무 많아 2000개에서 중단\n")
                break
            try:
                ms, _ = dcm.parse_verilog_text(self.fs.read_text(fap), shown)
            except Exception:
                continue
            for m in ms:
                mods.append(m)
        if not mods:
            o.write("모듈을 찾지 못했습니다. (검색한 파일 %d개)\n" % nfiles)
            return 1
        inst = set(t[0] for m in mods for t in m.instances)
        o.write("\n%-22s %5s %4s  %-18s %-24s %s\n" % ("Module", "Ports", "FFs", "Params", "Instantiates", "File"))
        o.write("-" * 110 + "\n")
        names = set(m.name for m in mods)
        for m in mods:
            params = ",".join("%s=%s" % (p, v) for p, v in m.params)[:18] or "-"
            subs = " ".join(sorted(set(t[0] for t in m.instances if t[0] in names)))[:24] or "-"
            o.write("%-22s %5d %4s  %-18s %-24s %s\n" % (m.name, len(m.ports), len(m.ff_names) or "-", params, subs, m.path))
        tops = [m.name for m in mods if m.name not in inst]
        o.write("-" * 110 + "\n")
        o.write("모듈 %d개 / 파일 %d개.  " % (len(mods), nfiles))
        o.write(C(o, "1;32", "Top 후보: %s" % (" ".join(tops) if tops else "(없음)")) + "\n")
        unknown = sorted(set(t[0] for m in mods for t in m.instances) - names)
        if unknown:
            o.write(C(o, "33", "정의를 못 찾은 하위 모듈: %s  (다른 폴더에 있거나 라이브러리 셀)" % " ".join(unknown[:15])) + "\n")

    def b_restore_demo(self, a, i, o, e):
        self._init_home()
        o.write("~/projects/demo 와 기본 파일 중 없어진 것을 다시 만들었습니다. (있는 파일은 건드리지 않음)\n")

    def b_newproj(self, a, i, o, e):
        libsel = "sky130"
        if "-lib" in a:
            k = a.index("-lib")
            libsel = a[k + 1] if k + 1 < len(a) else "sky130"
            a = a[:k] + a[k + 2:]
        if not a:
            e.write("사용법: newproj <프로젝트이름> [top모듈] [-lib sky130|edu]\n")
            return 1
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", a[0])
        top = a[1] if len(a) > 1 else "CHANGE_ME_TOP"
        base = self.fs.home + "/projects/" + name
        if self.fs.exists(base):
            e.write("newproj: %s 이(가) 이미 있습니다.\n" % self.disp(base))
            return 1
        for d in ("", "/rtl", "/scripts", "/reports", "/outputs"):
            self.fs.mkdir(base + d, parents=True)
        setup = T_SETUP if libsel == "edu" else T_SETUP_SKY
        run = T_RUN_NEW % {"name": name, "top": top}
        if libsel != "edu":
            run = run.replace("set CLK_PERIOD 5.0", "set CLK_PERIOD 10.0")
        for rel, txt in ((".synopsys_dc.setup", setup), ("scripts/run_dc.tcl", run)):
            f = self.fs.open_write(base + "/" + rel)
            f.write(txt)
            f.close()
        o.write("프로젝트 생성: %s   (라이브러리: %s)\n" % (self.disp(base), "edu_lib (가상)" if libsel == "edu"
                                                          else "SKY130 sky130_fd_sc_hd tt (실제 공개 lib)"))
        o.write("  rtl/  scripts/run_dc.tcl  reports/  outputs/  .synopsys_dc.setup\n\n")
        o.write("다음 단계:\n")
        o.write("  cp <PC의 .v 파일들> %s/rtl/     예) cp /mnt/c/.../rtl/*.v %s/rtl/\n" % (self.disp(base), self.disp(base)))
        o.write("  cd %s ; modfind rtl\n" % self.disp(base))
        if top == "CHANGE_ME_TOP":
            o.write("  vi scripts/run_dc.tcl          # set TOP 을 top 모듈 이름으로 수정\n")
        o.write("  dc_shell -f scripts/run_dc.tcl | tee dc.log\n")

    # ---------------- EDA ----------------
    def b_design_vision(self, a, i, o, e):
        e.write("Error: Unable to open display. DISPLAY variable is not set.\n")
        e.write(C(e, "36", "  [sim] GUI(design_vision)는 지원하지 않습니다. dc_shell 을 사용하세요.\n"))
        return 1

    def b_pt_shell(self, a, i, o, e):
        return self.b_dc_shell(a, i, o, e, kind="pt")

    def b_fm_shell(self, a, i, o, e):
        return self.b_dc_shell(a, i, o, e, kind="fm")

    def b_dc_shell(self, a, i, o, e, kind="dc"):
        tool = {"dc": "dc_shell", "pt": "pt_shell", "fm": "fm_shell"}[kind]
        if self.in_dc:
            e.write("[sim] 툴 셸 안에서 %s 을(를) 다시 실행할 수 없습니다.\n" % tool)
            return 1
        script, xcmds, logf = None, [], None
        topo = no_home = no_local = False
        k = 0
        while k < len(a):
            x = a[k]
            v = a[k + 1] if k + 1 < len(a) else None
            if x in ("-f", "-file") and v:
                script = v
                k += 2
                continue
            if x == "-x" and v:
                xcmds.append(v)
                k += 2
                continue
            if x == "-output_log_file" and v:
                logf = v
                k += 2
                continue
            if x in ("-topographical_mode", "-topo", "-t"):
                topo = True
            elif x == "-no_home_init":
                no_home = True
            elif x == "-no_local_init":
                no_local = True
            elif x in ("-version", "-v"):
                o.write("%s version    -  %s  (dc_shell_sim, simulated)\n" % (tool, self.dcmod.REPORT_VERSION))
                return 0
            elif x in ("-h", "-help", "--help"):
                o.write(MAN["dc_shell"] + "\n")
                return 0
            elif x in ("-64bit", "-64", "-no_gui", "-shell", "-wait"):
                pass
            elif x.startswith("-"):
                e.write("Error: unknown option '%s' (CMD-010)\n" % x)
                return 1
            k += 1
        screen = getattr(o, "screen", False)
        if not screen and not script and not xcmds:
            e.write("[sim] -f 스크립트 없이 dc_shell 출력을 파일/파이프로 보낼 수 없습니다. (| tee 는 가능)\n")
            return 1
        dcm = self.dcmod
        if kind == "dc":
            sh = dcm.Shell()
        else:
            import eda_tools
            pt_cls, fm_cls = eda_tools.make_classes(dcm)
            sh = pt_cls() if kind == "pt" else fm_cls()
        log_fo = None
        stream = None if (o is self.stdout) else o
        if logf:
            try:
                log_fo = self.fs.open_write(self.abs(logf))
                stream = TeeOut(o, [log_fo])
            except FSError as ex:
                e.write("Error: Cannot open log file '%s': %s\n" % (logf, ex))
        self.in_dc = True
        status = 0
        try:
            sh.attach(self, self.fs, self.cwd, self.env, topo=topo, no_home=no_home, no_local=no_local,
                      logname={"dc": "command.log", "pt": "pt_shell_command.log", "fm": "fm_shell_command.log"}[kind])
            if stream is not None:
                sh.streams.append(stream)
            try:
                sh.banner()
                for c in xcmds:
                    sh.exec_line(c)
                if script:
                    sap = self.abs(script)
                    if not self.fs.isfile(sap):
                        sh.out("Error: Cannot open script file '%s'. (CMD-015)" % script)
                        status = 1
                    else:
                        sh.run_script(self.fs.read_text(sap), echo=True)
                if screen and sys.stdin.isatty() or (screen and not script and not xcmds):
                    sh.repl()
                    status = 0
                else:
                    sh.goodbye()
            except SystemExit as ex:
                sh.goodbye()
                status = ex.code if isinstance(ex.code, int) else 0
        finally:
            self.in_dc = False
            sh.close()
            if log_fo:
                log_fo.close()
        return status


def run_terminal(dcmod, sim_root, color=True):
    global USE_COLOR
    USE_COLOR = color
    t = Terminal(dcmod, sim_root)
    t.run()
