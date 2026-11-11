#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dc_shell_sim.py  -  Synopsys Design Compiler(dc_shell) 화면을 흉내 내는 교육용 시뮬레이터

  * 이 프로그램은 Synopsys 제품이 아니며 실제 합성을 하지 않습니다.
  * .v 파일에서 모듈/포트/레지스터/연산자 정도만 가볍게 읽어서,
    그럴듯한 dc_shell 메시지와 리포트(타이밍/면적/파워 등)를 "가상으로" 만들어 보여줍니다.
  * 숫자는 RTL 구조, 클럭 주기, 라이브러리, compile 옵션에 따라 일관되게 변하도록 만들어져
    있어서 "클럭을 줄이면 slack 이 음수가 되고, compile_ultra 를 쓰면 개선된다" 같은
    흐름을 연습할 수 있습니다.

사용법:
    python dc_shell_sim.py                 # 대화형 dc_shell
    python dc_shell_sim.py -f scripts/run_dc.tcl   # 스크립트 실행 후 종료
    python dc_shell_sim.py -f run.tcl -i   # 스크립트 실행 후 대화형 유지
    python dc_shell_sim.py -x "help"       # 명령 하나 실행 후 대화형
    python dc_shell_sim.py -no_color       # 색상 끄기

표준 라이브러리만 사용합니다 (Python 3.8+).
"""
import os
import re
import sys
import math
import time
import random
import hashlib
import fnmatch
import datetime
from collections import Counter, OrderedDict

VERSION = "SIM-2026.11"
REPORT_VERSION = os.environ.get("SIM_TOOL_VERSION", "S-2021.06-SP5-2")   # 리포트/배너에 표시되는 툴 버전
HL = None            # 터미널 키워드 강조 함수 (vterm.highlight)
MARKER = "dc_shell_sim"          # 이 프로그램이 만든 파일 표시 (덮어쓰기 보호용)

# --------------------------------------------------------------------------
# 색상
# --------------------------------------------------------------------------
USE_COLOR = True


def _c(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if USE_COLOR else s


# --------------------------------------------------------------------------
# 가상 셀 라이브러리
# --------------------------------------------------------------------------
LIBS = OrderedDict([
    ("edu_lib_tt.db", dict(name="edu_lib_tt", oc="tt1p05v25c", volt=1.05, scale=1.00, leak=1.0,
                           desc="Typical  1.05V  25C")),
    ("edu_lib_ss.db", dict(name="edu_lib_ss", oc="ss0p95v125c", volt=0.95, scale=1.35, leak=2.6,
                           desc="Slow     0.95V 125C (setup 검증용 worst)")),
    ("edu_lib_ff.db", dict(name="edu_lib_ff", oc="ff1p16vn40c", volt=1.16, scale=0.78, leak=0.55,
                           desc="Fast     1.16V -40C (hold 검증용 best)")),
])

# name: (area, delay(ns), kind, input pins, output pin)
CELLS = OrderedDict([
    ("INVX1",   (1.271, 0.032, "inv",  ["A"], "Y")),
    ("INVX4",   (2.033, 0.024, "inv",  ["A"], "Y")),
    ("BUFX2",   (2.033, 0.058, "buf",  ["A"], "Y")),
    ("NAND2X1", (1.525, 0.044, "comb", ["A1", "A2"], "Y")),
    ("NAND2X2", (2.287, 0.036, "comb", ["A1", "A2"], "Y")),
    ("NOR2X1",  (1.779, 0.053, "comb", ["A1", "A2"], "Y")),
    ("AND2X1",  (2.033, 0.068, "comb", ["A1", "A2"], "Y")),
    ("OR2X1",   (2.033, 0.072, "comb", ["A1", "A2"], "Y")),
    ("XOR2X1",  (3.050, 0.088, "comb", ["A1", "A2"], "Y")),
    ("XNOR2X1", (3.050, 0.090, "comb", ["A1", "A2"], "Y")),
    ("AOI21X1", (2.287, 0.063, "comb", ["A1", "A2", "A3"], "Y")),
    ("OAI21X1", (2.287, 0.061, "comb", ["A1", "A2", "A3"], "Y")),
    ("AO22X1",  (3.304, 0.081, "comb", ["A1", "A2", "A3", "A4"], "Y")),
    ("MUX21X1", (3.304, 0.094, "comb", ["A1", "A2", "S0"], "Y")),
    ("HADDX1",  (4.066, 0.086, "comb", ["A0", "B0"], "SO")),
    ("FADDX1",  (6.099, 0.138, "comb", ["A", "B", "CI"], "S")),
    ("DFFX1",   (6.608, 0.178, "seq",  ["D", "CLK"], "Q")),
    ("DFFARX1", (7.625, 0.188, "seq",  ["D", "CLK", "RSTB"], "Q")),
])
GTECH = {
    "INVX1": "GTECH_NOT", "INVX4": "GTECH_NOT", "BUFX2": "GTECH_BUF", "NAND2X1": "GTECH_NAND2",
    "NAND2X2": "GTECH_NAND2", "NOR2X1": "GTECH_NOR2", "AND2X1": "GTECH_AND2", "OR2X1": "GTECH_OR2",
    "XOR2X1": "GTECH_XOR2", "XNOR2X1": "GTECH_XNOR2", "AOI21X1": "GTECH_AOI21", "OAI21X1": "GTECH_OAI21",
    "AO22X1": "GTECH_AO22", "MUX21X1": "GTECH_MUX2", "HADDX1": "GTECH_ADD_AB", "FADDX1": "GTECH_ADD_ABC",
    "DFFX1": "**SEQGEN**", "DFFARX1": "**SEQGEN**",
}
SETUP_TIME = 0.05
HOLD_TIME = 0.02

# --------------------------------------------------------------------------
# 예외
# --------------------------------------------------------------------------


class DCError(Exception):
    def __init__(self, msg, code="CMD-001", hint=None, ret0=True):
        Exception.__init__(self, msg)
        self.msg, self.code, self.hint, self.ret0 = msg, code, hint, ret0


class TclBreak(Exception):
    pass


class TclContinue(Exception):
    pass


# --------------------------------------------------------------------------
# 아주 가벼운 Verilog 파서 (구조 정보만 추출)
# --------------------------------------------------------------------------
VKEYS = set("""module endmodule macromodule input output inout wire reg logic tri signed unsigned integer real
parameter localparam assign always always_ff always_comb always_latch begin end if else case casez casex
endcase default posedge negedge or and not nand nor xor xnor buf for while repeat forever generate
endgenerate genvar initial function endfunction task endtask return defparam supply0 supply1 fork join
""".split())


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"^\s*`[^\n]*", "", text, flags=re.M)      # `timescale, `define 등 무시
    return text


def clog2(x):
    x = int(x)
    return 0 if x <= 1 else (x - 1).bit_length()


def eval_int(expr, env):
    """Verilog 상수식을 정수로 계산. 실패하면 None."""
    if expr is None:
        return None
    e = str(expr).strip()
    if not e:
        return None
    e = re.sub(r"\d*\s*'[sS]?[dD]\s*([0-9_]+)", lambda m: m.group(1).replace("_", ""), e)
    e = re.sub(r"\d*\s*'[sS]?[hH]\s*([0-9a-fA-F_]+)", lambda m: str(int(m.group(1).replace("_", ""), 16)), e)
    e = re.sub(r"\d*\s*'[sS]?[bB]\s*([01_]+)", lambda m: str(int(m.group(1).replace("_", ""), 2)), e)
    e = re.sub(r"\d*\s*'[sS]?[oO]\s*([0-7_]+)", lambda m: str(int(m.group(1).replace("_", ""), 8)), e)
    e = e.replace("$clog2", "clog2")
    bad = []

    def rep(m):
        k = m.group(0)
        if k == "clog2":
            return k
        if k in env and env[k] is not None:
            return str(env[k])
        bad.append(k)
        return "0"
    e = re.sub(r"[A-Za-z_]\w*", rep, e)
    if bad or not re.fullmatch(r"[\d\s+\-*/%()<>clog2]*", e):
        return None
    e = re.sub(r"(?<!/)/(?!/)", "//", e)
    try:
        return int(eval(e, {"__builtins__": {}, "clog2": clog2}))
    except Exception:
        return None


def seg_until(s, i):
    """depth 0 에서 ; 또는 닫는 괄호를 만날 때까지의 문자열"""
    depth = 0
    j = i
    while j < len(s):
        ch = s[j]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == ";" and depth == 0:
            break
        j += 1
    return s[i:j]


def split_top(s, sep=","):
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return out


def balanced_paren(s, i):
    """s[i] == '(' 일 때 짝 ')' 의 index"""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(s) - 1


class VModule:
    def __init__(self, name, path, body, line):
        self.name, self.path, self.line, self.body = name, path, line, body
        self.params = []          # [(name, expr)]
        self.ports = []           # [(dir, name, msb, lsb)]
        self.sigs = {}            # name -> (msb, lsb)
        self.ff_names = []
        self.ff_line = None
        self.async_reset = False
        self.ops = Counter()
        self.instances = []       # [(type, inst, {param:expr}, [pos params], conn text)]
        self.text_for_usage = ""
        self._parse()

    def _parse(self):
        b = self.body
        # parameters
        for m in re.finditer(r"\b(parameter|localparam)\b", b):
            seg = seg_until(b, m.end())
            seg = re.split(r"\b(?:parameter|localparam)\b", seg)[0]
            for piece in split_top(seg):
                pm = re.match(r"\s*(?:integer\s+|signed\s+|unsigned\s+)?(?:\[[^\]]*\]\s*)?([A-Za-z_]\w*)\s*=\s*(.+)",
                              piece, re.S)
                if pm and pm.group(1) not in [p[0] for p in self.params]:
                    self.params.append((pm.group(1), pm.group(2).strip()))
        # ports
        seen = set()
        for m in re.finditer(r"\b(input|output|inout)\b", b):
            seg = seg_until(b, m.end())
            seg = re.split(r"\b(?:input|output|inout)\b", seg)[0]
            mm = re.match(r"\s*((?:(?:wire|reg|logic|signed|unsigned|tri)\s+)*)"
                          r"(\[\s*([^\]:]+?)\s*:\s*([^\]]+?)\s*\])?\s*(.*)", seg, re.S)
            if not mm:
                continue
            msb, lsb, rest = mm.group(3), mm.group(4), mm.group(5)
            for piece in split_top(rest):
                nm = re.match(r"\s*([A-Za-z_]\w*)", piece)
                if nm and nm.group(1) not in VKEYS and nm.group(1) not in seen:
                    seen.add(nm.group(1))
                    self.ports.append((m.group(1), nm.group(1), msb, lsb))
                    self.sigs[nm.group(1)] = (msb, lsb)
        # wire / reg widths
        for m in re.finditer(r"\b(wire|reg|logic)\b", b):
            seg = seg_until(b, m.end())
            mm = re.match(r"\s*(?:signed\s+|unsigned\s+)?(\[\s*([^\]:]+?)\s*:\s*([^\]]+?)\s*\])?\s*(.*)", seg, re.S)
            if not mm:
                continue
            for piece in split_top(mm.group(4)):
                nm = re.match(r"\s*([A-Za-z_]\w*)", piece)
                if nm and nm.group(1) not in VKEYS:
                    if mm.group(1) or nm.group(1) not in self.sigs:
                        self.sigs[nm.group(1)] = (mm.group(2), mm.group(3))
        # flip-flops (posedge/negedge always)
        alw = list(re.finditer(r"\balways(?:_ff)?\s*@\s*\(([^)]*)\)|\balways(?:_ff)?\s*@\s*\*|\bassign\b", b))
        for idx, m in enumerate(alw):
            sens = m.group(1)
            if not sens or ("posedge" not in sens and "negedge" not in sens):
                continue
            if len(re.findall(r"(posedge|negedge)", sens)) >= 2:
                self.async_reset = True
            end = alw[idx + 1].start() if idx + 1 < len(alw) else len(b)
            blk = b[m.end():end]
            if self.ff_line is None:
                self.ff_line = self.line + b[:m.start()].count("\n")
            for lm in re.finditer(r"(?:^|[;)]|\bbegin\b|\belse\b|:)\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*<=",
                                  blk, re.M):
                n = lm.group(1)
                if n not in VKEYS and n not in self.ff_names:
                    self.ff_names.append(n)
        # instances
        pat = re.compile(r"(?:^|;|\bend\b|\bbegin\b)\s*([A-Za-z_]\w*)\b\s*"
                         r"(#\s*\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\))?\s*([A-Za-z_]\w*)\s*\(", re.M)
        inst_spans = []
        for m in pat.finditer(b):
            typ, inst = m.group(1), m.group(3)
            if typ in VKEYS or inst in VKEYS:
                continue
            pov, ppos = {}, []
            if m.group(2):
                inner = m.group(2)[m.group(2).index("(") + 1:-1]
                named = re.findall(r"\.\s*(\w+)\s*\(([^()]*(?:\([^()]*\))?[^()]*)\)", inner)
                if named:
                    pov = {k: v.strip() for k, v in named}
                else:
                    ppos = [x.strip() for x in split_top(inner)]
            p0 = m.end() - 1
            p1 = balanced_paren(b, p0)
            self.instances.append((typ, inst, pov, ppos, b[p0 + 1:p1]))
            inst_spans.append((m.start(), p1))
        # 연산자 통계 (선언/인스턴스 제거 후)
        t = b
        for s0, s1 in reversed(inst_spans):
            t = t[:s0] + ";" + t[s1 + 1:]
        t = re.sub(r"\b(input|output|inout|wire|reg|logic|parameter|localparam|integer|genvar)\b[^;]*;", ";", t)
        t = re.sub(r"#\s*\((?:[^()]|\([^()]*\))*\)", "", t)
        t = re.sub(r"\[[^\]]*\]", "", t)
        t = re.sub(r"\d*\s*'[sS]?[bdhoBDHO]\s*[0-9a-fA-F_xzXZ?]+", "0", t)
        self.text_for_usage = t
        t = re.sub(r"@\s*\([^)]*\)", "", t)
        t = t.replace("@*", "")
        t2 = re.sub(r"(?<=[\w\)\]\s])<=", "=", t)
        self.ops["add"] = len(re.findall(r"(?<![+])\+(?![+:])", t2))
        self.ops["sub"] = len(re.findall(r"(?<![-])-(?![->:])", t2))
        self.ops["mul"] = len(re.findall(r"(?<![*])\*(?![*])", t2))
        self.ops["cmp"] = len(re.findall(r"==|!=|>=|(?<![>=-])>(?![>=])|(?<![<])<(?![<=])", t2))
        self.ops["shift"] = len(re.findall(r"<<|>>", t2))
        self.ops["mux"] = t2.count("?") + len(re.findall(r"\bif\b", t2)) + \
            3 * len(re.findall(r"\bcase[zx]?\b", t2))
        self.ops["logic"] = len(re.findall(r"(?<![&|])[&|^~](?![&|])", t2))

    def uses(self, name):
        return len(re.findall(r"\b%s\b" % re.escape(name), self.text_for_usage))


def parse_verilog_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return parse_verilog_text(text, path)


def parse_verilog_text(text, path):
    clean = strip_comments(text)
    mods = []
    for m in re.finditer(r"\b(?:module|macromodule)\s+([A-Za-z_]\w*)(.*?)\bendmodule\b", clean, re.S):
        line = clean[:m.start()].count("\n") + 1
        mods.append(VModule(m.group(1), path, m.group(2), line))
    return mods, text.count("\n") + 1


# --------------------------------------------------------------------------
# 디자인 객체
# --------------------------------------------------------------------------
class Design:
    def __init__(self, name, mod, env):
        self.name, self.mod, self.env = name, mod, env
        self.ports = []          # [(dir, name, width)]
        self.flops = []          # [(name, width)]
        self.subs = []           # [(inst, design name)]
        self.unresolved = []     # [(inst, type)]
        self.linked = False
        self.mapped = False
        self.flat = False
        self.dont_touch = False
        self.effort = None
        self.st = None           # 합성 결과 통계
        self.est = None          # GTECH 추정 통계
        # constraints
        self.clocks = OrderedDict()   # name -> dict(period, wave, src)
        self.unc = {}
        self.latency = {}
        self.transition = {}
        self.in_delay = {}        # port -> (val, clk)
        self.out_delay = {}
        self.loads = {}
        self.driving = {}
        self.max_area = None
        self.max_fanout = None
        self.max_transition = None
        self.false_paths = []
        self.mcp = []
        self.sdc = []
        self.wlm = None
        self.oc = None

    @property
    def datawidth(self):
        ws = [w for _, _, w in self.ports] + [w for _, w in self.flops]
        return max(ws) if ws else 1

    def port(self, name):
        for p in self.ports:
            if p[1] == name:
                return p
        return None

    def flop_bits(self):
        bits = []
        for n, w in self.flops:
            if w > 1:
                bits += ["%s_reg_%d_" % (n, i) for i in range(w)]
            else:
                bits.append("%s_reg" % n)
        return bits

    def port_bits(self, direction):
        bits = []
        for d, n, w in self.ports:
            if d == direction or (direction == "input" and d == "inout"):
                bits += (["%s[%d]" % (n, i) for i in range(w)] if w > 1 else [n])
        return bits


class Coll:
    """dc_shell 의 collection 흉내"""

    def __init__(self, kind, names):
        self.kind = kind
        self.names = list(OrderedDict.fromkeys(names))

    def __str__(self):
        return " ".join(self.names)

    def display(self):
        return "{" + " ".join(self.names) + "}"


# --------------------------------------------------------------------------
# 도움말
# --------------------------------------------------------------------------
HELP = OrderedDict()


def _h(group, name, summary, usage=""):
    HELP[name] = (group, summary, usage)


G_FILE, G_LIB, G_CONS, G_OPT, G_REP, G_OUT, G_TCL, G_SYS = (
    "Read / Elaborate", "Library / App Var", "Constraints (SDC)", "Optimization",
    "Reports", "Output", "Tcl / Collections", "System")
_h(G_FILE, "analyze", "HDL 파일을 분석해 WORK 라이브러리에 저장", "analyze [-library WORK] -format verilog|sverilog {file1.v file2.v}")
_h(G_FILE, "elaborate", "분석된 모듈로 디자인(GTECH) 생성", "elaborate <module> [-parameters \"W=16,D=4\"] [-architecture verilog]")
_h(G_FILE, "read_verilog", "analyze + elaborate 한 번에", "read_verilog {file1.v file2.v}")
_h(G_FILE, "read_file", "read_verilog 와 비슷 (-format verilog)", "read_file -format verilog {files}")
_h(G_FILE, "current_design", "현재 디자인 보기/변경", "current_design [design]")
_h(G_FILE, "link", "하위 모듈 참조와 라이브러리 연결", "link")
_h(G_FILE, "uniquify", "중복 인스턴스 디자인 고유화", "uniquify")
_h(G_FILE, "check_design", "연결/구조 문제 점검", "check_design [-summary]")
_h(G_FILE, "list_designs", "메모리에 있는 디자인 목록", "list_designs")
_h(G_FILE, "remove_design", "디자인 삭제", "remove_design -all | <design>")
_h(G_LIB, "set_app_var", "애플리케이션 변수 설정", "set_app_var target_library edu_lib_tt.db")
_h(G_LIB, "get_app_var", "애플리케이션 변수 값", "get_app_var <var>")
_h(G_LIB, "printvar", "변수 출력", "printvar [pattern]")
_h(G_LIB, "list_libs", "사용 가능한(모의) 라이브러리 목록", "list_libs")
_h(G_LIB, "report_lib", "라이브러리 셀 정보", "report_lib <lib_name>")
_h(G_CONS, "create_clock", "클럭 정의", "create_clock -period <ns> [-name clk] [-waveform {0 5}] [get_ports clk]")
_h(G_CONS, "set_clock_uncertainty", "클럭 불확실성(skew+jitter 여유)", "set_clock_uncertainty <ns> [get_clocks clk]")
_h(G_CONS, "set_clock_latency", "클럭 지연", "set_clock_latency <ns> [get_clocks clk]")
_h(G_CONS, "set_clock_transition", "클럭 트랜지션", "set_clock_transition <ns> [get_clocks clk]")
_h(G_CONS, "set_input_delay", "입력 지연", "set_input_delay <ns> -clock clk [ports]")
_h(G_CONS, "set_output_delay", "출력 지연", "set_output_delay <ns> -clock clk [ports]")
_h(G_CONS, "set_load", "출력 부하(pF)", "set_load <val> [all_outputs]")
_h(G_CONS, "set_driving_cell", "입력 구동 셀", "set_driving_cell -lib_cell BUFX2 [ports]")
_h(G_CONS, "set_max_area", "면적 목표", "set_max_area <val>")
_h(G_CONS, "set_max_fanout", "최대 fanout", "set_max_fanout <n> <design|ports>")
_h(G_CONS, "set_max_transition", "최대 트랜지션", "set_max_transition <ns> <design>")
_h(G_CONS, "set_false_path", "타이밍 예외(false path)", "set_false_path -from [get_ports rst_n]")
_h(G_CONS, "set_multicycle_path", "멀티사이클 경로", "set_multicycle_path 2 -setup -from .. -to ..")
_h(G_CONS, "set_dont_touch", "최적화 금지", "set_dont_touch <design|cell> [true|false]")
_h(G_CONS, "set_wire_load_model", "와이어로드 모델", "set_wire_load_model -name <model>")
_h(G_CONS, "set_operating_conditions", "동작 조건", "set_operating_conditions <name>")
_h(G_CONS, "reset_design", "현재 디자인의 제약 모두 삭제", "reset_design")
_h(G_CONS, "check_timing", "제약 누락 점검", "check_timing")
_h(G_OPT, "compile", "기본 합성 (매핑+최적화)", "compile [-map_effort low|medium|high] [-area_effort ..] [-incremental_mapping]")
_h(G_OPT, "compile_ultra", "고성능 합성 (DC Ultra)", "compile_ultra [-no_autoungroup] [-retime] [-gate_clock] [-incremental]")
_h(G_OPT, "ungroup", "계층 풀기", "ungroup -all -flatten | <cell>")
_h(G_REP, "report_timing", "타이밍 경로 리포트", "report_timing [-max_paths n] [-delay_type max|min] [-from ..] [-to ..] [-nworst n]")
_h(G_REP, "report_area", "면적 리포트", "report_area [-hierarchy]")
_h(G_REP, "report_power", "파워 리포트", "report_power [-hierarchy]")
_h(G_REP, "report_qor", "품질 요약(QoR)", "report_qor")
_h(G_REP, "report_constraint", "제약 위반 리포트", "report_constraint [-all_violators]")
_h(G_REP, "report_clock", "클럭 리포트", "report_clock")
_h(G_REP, "report_port", "포트 리포트", "report_port [-verbose]")
_h(G_REP, "report_design", "디자인 속성 리포트", "report_design")
_h(G_REP, "report_hierarchy", "계층 구조 리포트", "report_hierarchy")
_h(G_REP, "report_reference", "참조 셀 리포트", "report_reference")
_h(G_REP, "report_cell", "셀 목록 리포트", "report_cell")
_h(G_OUT, "write", "디자인 저장 (verilog netlist / ddc)", "write -format verilog|ddc [-hierarchy] -output <file>")
_h(G_OUT, "write_file", "write 와 동일", "write_file -format verilog -hierarchy -output <file>")
_h(G_OUT, "write_sdc", "제약을 SDC 로 저장", "write_sdc <file>")
_h(G_OUT, "write_sdf", "가상 SDF 저장", "write_sdf <file>")
_h(G_TCL, "set", "Tcl 변수 설정", "set var value")
_h(G_TCL, "unset", "Tcl 변수 삭제", "unset var")
_h(G_TCL, "echo", "문자열 출력", "echo \"text\"")
_h(G_TCL, "puts", "문자열 출력", "puts \"text\"")
_h(G_TCL, "expr", "수식 계산", "expr {$a * 2}")
_h(G_TCL, "if", "조건문", "if {expr} {body} elseif {..} {..} else {..}")
_h(G_TCL, "foreach", "반복문", "foreach v $list {body}")
_h(G_TCL, "for", "반복문", "for {set i 0} {$i < 3} {incr i} {body}")
_h(G_TCL, "while", "반복문", "while {expr} {body}")
_h(G_TCL, "incr", "정수 증가", "incr var [n]")
_h(G_TCL, "proc", "프로시저 정의", "proc name {args} {body}")
_h(G_TCL, "list", "리스트 생성", "list a b c")
_h(G_TCL, "llength", "리스트 길이", "llength $list")
_h(G_TCL, "lindex", "리스트 원소", "lindex $list i")
_h(G_TCL, "lappend", "리스트에 추가", "lappend var x")
_h(G_TCL, "format", "문자열 포맷", "format \"%.2f\" $x")
_h(G_TCL, "get_ports", "포트 collection", "get_ports <pattern>")
_h(G_TCL, "get_clocks", "클럭 collection", "get_clocks <pattern>")
_h(G_TCL, "get_cells", "셀 collection", "get_cells [-hierarchical] <pattern>")
_h(G_TCL, "get_designs", "디자인 collection", "get_designs <pattern>")
_h(G_TCL, "get_lib_cells", "라이브러리 셀 collection", "get_lib_cells edu_lib_tt/*")
_h(G_TCL, "all_inputs", "모든 입력 포트", "all_inputs")
_h(G_TCL, "all_outputs", "모든 출력 포트", "all_outputs")
_h(G_TCL, "all_clocks", "모든 클럭", "all_clocks")
_h(G_TCL, "all_registers", "모든 레지스터", "all_registers")
_h(G_TCL, "remove_from_collection", "collection 에서 제거", "remove_from_collection [all_inputs] [get_ports clk]")
_h(G_TCL, "sizeof_collection", "collection 크기", "sizeof_collection [all_inputs]")
_h(G_TCL, "get_object_name", "collection 이름 문자열", "get_object_name [get_ports *]")
_h(G_TCL, "foreach_in_collection", "collection 반복", "foreach_in_collection p [all_inputs] {body}")
_h(G_SYS, "source", "Tcl 스크립트 실행", "source [-echo] <file.tcl>")
_h(G_SYS, "redirect", "명령 출력을 파일로", "redirect [-append] <file> {command}")
_h(G_SYS, "file", "file mkdir/exists", "file mkdir <dir> | file exists <path>")
_h(G_SYS, "history", "명령 기록", "history")
_h(G_SYS, "help", "명령 목록 / 사용법", "help [pattern] | help -verbose <cmd>")
_h(G_SYS, "man", "명령 설명", "man <cmd>")
_h(G_SYS, "ls", "파일 목록", "ls [dir]")
_h(G_SYS, "pwd", "현재 디렉터리", "pwd")
_h(G_SYS, "cd", "디렉터리 이동", "cd <dir>")
_h(G_SYS, "date", "날짜", "date")
_h(G_SYS, "clear", "화면 지우기", "clear")
_h(G_SYS, "tutorial", "(시뮬레이터 전용) 학습용 추천 흐름 보기", "tutorial")
_h(G_SYS, "exit", "종료", "exit | quit")


# --------------------------------------------------------------------------
# 셸
# --------------------------------------------------------------------------
def stable_seed(*parts):
    h = hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:12], 16)


def dc_date():
    d = datetime.datetime.now()
    return "%s %s %2d %s %d" % (d.strftime("%a"), d.strftime("%b"), d.day, d.strftime("%H:%M:%S"), d.year)


class ShellCore:
    def gate_factor(self, d):
        return 1.0

    # ---- 셀 정보 (가상 edu_lib 또는 실제 liberty) ----
    def ci(self, k, lib):
        """generic role -> (area, delay, kind, ins, out, name, tran)"""
        cm = lib.get("cmap") if lib else None
        if cm and cm.get(k):
            return cm[k]
        if cm:
            for alt in ({"DFFARX1": "DFFX1", "DFFX1": "DFFARX1", "INVX4": "INVX1", "NAND2X2": "NAND2X1",
                         "BUFX2": "INVX1"}.get(k, "NAND2X1"), "NAND2X1", "INVX1"):
                if cm.get(alt):
                    return cm[alt]
        a, dl, kd, ins, out = CELLS[k]
        sc = lib["scale"] if lib else 1.0
        return (a, dl * sc, kd, ins, out, k, 0.03 + dl * 0.6 * sc)

    def setup_t(self, lib):
        return lib["setup"] if lib.get("setup") is not None else SETUP_TIME * lib["scale"]

    def hold_t(self, lib):
        return lib["hold"] if lib.get("hold") is not None else HOLD_TIME * lib["scale"]

    def leak_pw(self, cells, lib, area):
        t = lib.get("tech")
        if t and lib.get("cmap"):
            return sum(t.leak_w(self.ci(k, lib)[5]) * v for k, v in cells.items()) * 1e12
        return area * 21000.0 * lib["leak"]

    def _lib_exists(self, name):
        return False

    def read_input(self, prompt):
        return input(prompt)

    def _netlist_meta(self, d):
        return ""

    def lib_file(self, lib):
        return lib.get("file") or (lib["name"] + ".db")

    def read_text(self, p):
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def abspath(self, p):
        return os.path.abspath(p)

    def __init__(self):
        self.vars = OrderedDict([
            ("search_path", ". "),
            ("target_library", "your_library.db"),
            ("link_library", "* your_library.db"),
            ("synthetic_library", ""),
            ("symbol_library", "your_library.sdb"),
            ("hdlin_check_no_latch", "false"),
            ("compile_seqmap_propagate_constants", "true"),
            ("sh_product_version", VERSION),
            ("sh_command_log_file", ""),
            ("sim_hints", "true"),       # 시뮬레이터 전용: 학습 힌트 출력
            ("sim_fast", "false"),       # 시뮬레이터 전용: compile 진행 연출(지연) 끄기
        ])
        self.app_vars = set(k for k in self.vars)
        self.procs = {}
        self.modules = OrderedDict()
        self.designs = OrderedDict()
        self.cur = None
        self.streams = []
        self.history = []
        self.t0 = time.time()
        self.cpu = 0.0
        self.cmd_count = 0
        self.depth = 0
        self.cmds = {}
        for name in dir(self):
            if name.startswith("c_"):
                self.cmds[name[2:]] = getattr(self, name)
        self.cmds["quit"] = self.c_exit
        self.cmds["write_file"] = self.c_write
        self.cmds["read_file"] = self.c_read_verilog
        self.cmds["man"] = self.c_help_man

    # ---------------- 출력 -----------------
    def out(self, text="", color=None):
        if self.streams:
            self.streams[-1].write(text + "\n")
        else:
            if color and USE_COLOR:
                text = _c(color, text)
            elif HL and USE_COLOR and sys.stdout.isatty():
                text = HL(text)
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def warn(self, msg, code):
        self.out("Warning: %s (%s)" % (msg, code), "33")

    def info(self, msg, code=None):
        self.out("Information: %s%s" % (msg, " (%s)" % code if code else ""))

    def hint(self, msg):
        if self.vars.get("sim_hints", "true") in ("true", "1") and not self.streams:
            self.out("  [학습 힌트] " + msg, "36")

    def pause(self, sec):
        if self.vars.get("sim_fast") in ("true", "1") or self.streams or not sys.stdout.isatty():
            return
        time.sleep(sec)

    def header(self, title, extra=()):
        d = self.need_design()
        self.out("")
        self.out("*" * 40)
        self.out("Report : %s" % title)
        for e in extra:
            self.out("        %s" % e)
        self.out("Design : %s" % d.name)
        self.out("Version: %s" % REPORT_VERSION)
        self.out("Date   : %s" % dc_date())
        self.out("*" * 40)
        self.out("")

    # ---------------- 파일 쓰기 보호 -----------------
    def safe_open(self, path, append=False):
        path = os.path.expanduser(path)
        if os.path.exists(path):
            if os.path.isdir(path):
                raise DCError("'%s' is a directory." % path, "FILE-2")
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    head = f.read(400)
            except Exception:
                head = ""
            if MARKER not in head:
                raise DCError("File '%s' already exists and was not created by %s; refusing to overwrite." % (path, MARKER),
                              "SIM-1", hint="기존 파일 보호 기능입니다. 다른 파일 이름을 사용하세요.")
        parent = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(parent):
            raise DCError("Cannot open file '%s' for writing: directory does not exist." % path, "FILE-4",
                          hint="file mkdir <디렉터리> 로 먼저 만드세요.")
        f = open(path, "a" if append else "w", encoding="utf-8")
        if not append or f.tell() == 0:
            pass
        return f

    # ======================================================================
    # Tcl 해석기 (부분 구현)
    # ======================================================================
    def to_str(self, v):
        if isinstance(v, Coll):
            return str(v)
        if isinstance(v, float):
            s = "%.6f" % v
            s = s.rstrip("0").rstrip(".") if "." in s else s
            return s if s not in ("", "-0") else "0"
        if v is None:
            return ""
        return str(v)

    @staticmethod
    def match_brace(s, i):
        depth = 0
        j = i
        while j < len(s):
            ch = s[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return j
            j += 1
        raise DCError("missing close-brace", "CMD-033", ret0=False)

    def match_bracket(self, s, i):
        depth = 0
        j = i
        while j < len(s):
            ch = s[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "{":
                j = self.match_brace(s, j) + 1
                continue
            if ch == '"':
                k = j + 1
                while k < len(s) and s[k] != '"':
                    k += 2 if s[k] == "\\" else 1
                j = k + 1
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return j
            j += 1
        raise DCError("missing close-bracket", "CMD-033", ret0=False)

    def getvar(self, name):
        if name in self.vars:
            return self.vars[name]
        if name == "env(HOME)":
            return os.path.expanduser("~")
        raise DCError("can't read \"%s\": no such variable" % name, "CMD-013", ret0=False)

    def subst(self, s, i, stop):
        parts, buf = [], []
        n = len(s)
        while i < n and not stop(s, i):
            c = s[i]
            if c == "\\" and i + 1 < n:
                nx = s[i + 1]
                if nx == "\n":
                    buf.append(" ")
                else:
                    buf.append({"n": "\n", "t": "\t"}.get(nx, nx))
                i += 2
                continue
            if c == "[":
                j = self.match_bracket(s, i)
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append(self.eval(s[i + 1:j]))
                i = j + 1
                continue
            if c == "$":
                m = re.match(r"\$(?:\{([^}]*)\}|([A-Za-z0-9_:]+(?:\([^)]*\))?))", s[i:])
                if m:
                    if buf:
                        parts.append("".join(buf))
                        buf = []
                    parts.append(self.getvar(m.group(1) or m.group(2)))
                    i += m.end()
                    continue
            buf.append(c)
            i += 1
        if buf:
            parts.append("".join(buf))
        if not parts:
            return "", i
        if len(parts) == 1:
            return parts[0], i
        return "".join(self.to_str(p) for p in parts), i

    def parse_words(self, s, i):
        words = []
        n = len(s)
        while i < n:
            c = s[i]
            if c in " \t\r":
                i += 1
                continue
            if c in "\n;":
                break
            if c == "\\" and i + 1 < n and s[i + 1] == "\n":
                i += 2
                continue
            if c == "{":
                j = self.match_brace(s, i)
                words.append(s[i + 1:j])
                i = j + 1
            elif c == '"':
                w, i = self.subst(s, i + 1, lambda t, k: t[k] == '"')
                if i >= n:
                    raise DCError("missing \"", "CMD-033", ret0=False)
                i += 1
                words.append(self.to_str(w))
            else:
                w, i = self.subst(s, i, lambda t, k: t[k] in " \t\r\n;")
                words.append(w)
        return words, i

    def eval(self, s):
        i, n, result = 0, len(s), ""
        while i < n:
            while i < n and s[i] in " \t\r\n;":
                i += 1
            if i >= n:
                break
            if s[i] == "#":
                while i < n and s[i] != "\n":
                    i += 1
                continue
            words, i = self.parse_words(s, i)
            if words:
                result = self.invoke(words)
        return result

    def invoke(self, words):
        name = self.to_str(words[0])
        args = list(words[1:])
        redirect = None
        if len(args) >= 2 and args[-2] in (">", ">>"):
            redirect = (self.to_str(args[-1]), args[-2] == ">>")
            args = args[:-2]
        if name in self.procs:
            fn = lambda a: self.run_proc(name, a)
        else:
            fn = self.cmds.get(name)
        if fn is None:
            cands = [c for c in self.cmds if c.startswith(name)] if len(name) >= 3 else []
            if len(cands) == 1:
                fn = self.cmds[cands[0]]
            else:
                raise DCError("unknown command '%s'" % name, "CMD-005", ret0=False,
                              hint="help 로 사용 가능한 명령을 볼 수 있습니다." if not cands else
                              "후보: " + ", ".join(sorted(cands)[:8]))
        if redirect:
            f = self.safe_open(redirect[0], redirect[1])
            self.streams.append(f)
            try:
                return fn(args)
            finally:
                self.streams.pop()
                f.close()
        return fn(args)

    def is_complete(self, text):
        depth_b = depth_k = 0
        inq = False
        i = 0
        while i < len(text):
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if c == '"' and depth_b == 0:
                inq = not inq
            elif not inq:
                if c == "{":
                    depth_b += 1
                elif c == "}":
                    depth_b -= 1
                elif c == "[" and depth_b == 0:
                    depth_k += 1
                elif c == "]" and depth_b == 0:
                    depth_k -= 1
            i += 1
        return depth_b <= 0 and depth_k <= 0 and not inq and not text.rstrip("\n").endswith("\\")

    # ---------------- Tcl 리스트 -----------------
    def tcl_list(self, v):
        if isinstance(v, Coll):
            return list(v.names)
        if isinstance(v, (list, tuple)):
            return [self.to_str(x) for x in v]
        s = self.to_str(v)
        out, i, n = [], 0, len(s)
        while i < n:
            if s[i].isspace():
                i += 1
                continue
            if s[i] == "{":
                j = self.match_brace(s, i)
                out.append(s[i + 1:j])
                i = j + 1
            elif s[i] == '"':
                j = s.find('"', i + 1)
                j = n if j < 0 else j
                out.append(s[i + 1:j])
                i = j + 1
            else:
                j = i
                while j < n and not s[j].isspace():
                    j += 1
                out.append(s[i:j])
                i = j
        return out

    def list_str(self, items):
        res = []
        for x in items:
            x = self.to_str(x)
            res.append("{%s}" % x if (x == "" or re.search(r"\s", x)) else x)
        return " ".join(res)

    # ---------------- 옵션 파서 -----------------
    def opts(self, cmd, args, flags=(), valued=()):
        res, pos = {}, []
        flags = [f for f in flags if f not in valued]
        allo = list(OrderedDict.fromkeys(list(flags) + list(valued)))
        i = 0
        while i < len(args):
            a = args[i]
            if isinstance(a, str) and len(a) > 1 and a.startswith("-") and not re.match(r"-[\d.]", a):
                cand = [o for o in allo if o == a] or [o for o in allo if o.startswith(a)]
                if len(cand) != 1:
                    if cand:
                        raise DCError("ambiguous option '%s' (%s)" % (a, ", ".join(cand)), "CMD-007")
                    raise DCError("unknown option '%s'" % a, "CMD-010",
                                  hint="help -verbose %s 로 옵션을 확인하세요." % cmd)
                o = cand[0]
                if o in valued:
                    if i + 1 >= len(args):
                        raise DCError("value not specified for option '%s'" % o, "CMD-008")
                    res[o] = args[i + 1]
                    i += 2
                else:
                    res[o] = True
                    i += 1
            else:
                pos.append(a)
                i += 1
        return res, pos

    def fnum(self, v, opt="value"):
        try:
            return float(self.to_str(v))
        except ValueError:
            raise DCError("value '%s' for option '%s' not of type 'float'" % (self.to_str(v), opt), "CMD-009")

    # ---------------- 공통 -----------------
    def need_design(self):
        if not self.cur or self.cur not in self.designs:
            raise DCError("Current design is not defined.", "UID-4",
                          hint="read_verilog/elaborate 로 디자인을 읽은 뒤 current_design <top> 을 지정하세요.")
        return self.designs[self.cur]

    def lib(self):
        tl = self.vars.get("target_library", "").split()
        if not tl:
            raise DCError("target_library variable is empty.", "UIO-3",
                          hint="set_app_var target_library edu_lib_tt.db")
        name = os.path.basename(tl[0])
        if name not in LIBS:
            raise DCError("Can't read link_library file '%s'." % name, "UID-3",
                          hint="이 시뮬레이터의 가상 라이브러리는 list_libs 로 확인. 예) "
                               "set_app_var target_library edu_lib_tt.db ; set_app_var link_library \"* edu_lib_tt.db\"")
        return LIBS[name]

    def resolve(self, path):
        path = os.path.expanduser(path)
        if os.path.isabs(path) and os.path.exists(path):
            return path
        if os.path.exists(path):
            return path
        for d in self.vars.get("search_path", ".").split():
            p = os.path.join(d, path)
            if os.path.exists(p):
                return p
        return None

    def names_of(self, v, kind="port"):
        if isinstance(v, Coll):
            return list(v.names)
        return self.tcl_list(v)

    def ports_from(self, d, items):
        names = []
        for it in items:
            for n in self.names_of(it):
                base = re.sub(r"\[.*\]$", "", n)
                if any(fnmatch.fnmatchcase(p[1], base) for p in d.ports):
                    names += [p[1] for p in d.ports if fnmatch.fnmatchcase(p[1], base)]
                elif n in d.clocks:
                    names.append(n)
                else:
                    self.warn("Can't find port '%s' in design '%s'." % (n, d.name), "UID-95")
        return list(OrderedDict.fromkeys(names))

    def all_designs_under(self, d, seen=None):
        res = [d]
        if d.flat:
            return res
        for _, c in d.subs:
            if c in self.designs:
                cd = self.designs[c]
                if cd not in res:
                    for x in self.all_designs_under(cd):
                        if x not in res:
                            res.append(x)
        return res

    # ======================================================================
    # Tcl 기본 명령
    # ======================================================================
    def c_set(self, a):
        if not a:
            raise DCError("wrong # args: should be \"set varName ?newValue?\"", "CMD-012")
        name = self.to_str(a[0])
        if len(a) == 1:
            return self.getvar(name)
        v = a[1]
        self.vars[name] = v if isinstance(v, Coll) else self.to_str(v)
        return v

    def c_set_app_var(self, a):
        if len(a) != 2:
            raise DCError("wrong # args: should be \"set_app_var var value\"", "CMD-012")
        name = self.to_str(a[0])
        if name not in self.app_vars:
            raise DCError("Variable '%s' is not an application variable." % name, "CMD-104",
                          hint="일반 Tcl 변수는 set 을 사용하세요.")
        self.vars[name] = self.to_str(a[1])
        return self.vars[name]

    def c_get_app_var(self, a):
        name = self.to_str(a[0]) if a else ""
        if name not in self.app_vars:
            raise DCError("Variable '%s' is not an application variable." % name, "CMD-104")
        return self.vars[name]

    def c_unset(self, a):
        for x in a:
            self.vars.pop(self.to_str(x), None)
        return ""

    def c_printvar(self, a):
        pat = self.to_str(a[0]) if a else "*"
        for k, v in self.vars.items():
            if fnmatch.fnmatchcase(k, pat):
                self.out('%-36s = "%s"' % (k, self.to_str(v)))
        return ""

    def c_echo(self, a):
        o, pos = self.opts("echo", a, flags=["-n"])
        self.out(" ".join(self.to_str(x) for x in pos))
        return ""

    def c_puts(self, a):
        a = [x for x in a if x != "-nonewline"]
        if len(a) == 2 and a[0] in ("stdout", "stderr"):
            a = a[1:]
        self.out(" ".join(self.to_str(x) for x in a))
        return ""

    def c_expr(self, a):
        s = " ".join(self.to_str(x) for x in a)
        v, _ = self.subst(s, 0, lambda t, k: False)
        e = self.to_str(v)
        e = e.replace("&&", " and ").replace("||", " or ")
        e = re.sub(r"!(?!=)", " not ", e)
        e = re.sub(r"\btrue\b", "1", e)
        e = re.sub(r"\bfalse\b", "0", e)
        env = {"__builtins__": {}, "abs": abs, "int": int, "double": float, "round": round, "min": min,
               "max": max, "sqrt": math.sqrt, "pow": pow, "log": math.log, "log10": math.log10,
               "ceil": math.ceil, "floor": math.floor}
        try:
            r = eval(e, env)
        except ZeroDivisionError:
            raise DCError("divide by zero", "CMD-013")
        except Exception:
            raise DCError("syntax error in expression \"%s\"" % s, "CMD-013")
        if isinstance(r, bool):
            return 1 if r else 0
        if isinstance(r, float) and r.is_integer() and "." not in e and "/" not in e:
            return int(r)
        return r

    def truth(self, cond):
        v = self.c_expr([cond])
        try:
            return float(self.to_str(v)) != 0
        except ValueError:
            return self.to_str(v) not in ("", "false", "no")

    def c_if(self, a):
        a = [x for x in a if x != "then"]
        i = 0
        while i < len(a):
            if self.truth(a[i]):
                return self.eval(self.to_str(a[i + 1]))
            i += 2
            if i < len(a):
                if a[i] == "elseif":
                    i += 1
                    continue
                if a[i] == "else":
                    return self.eval(self.to_str(a[i + 1]))
                return self.eval(self.to_str(a[i]))
        return ""

    def c_foreach(self, a):
        if len(a) != 3:
            raise DCError("wrong # args: should be \"foreach varName list body\"", "CMD-012")
        var, lst, body = self.to_str(a[0]), self.tcl_list(a[1]), self.to_str(a[2])
        for x in lst:
            self.vars[var] = x
            try:
                self.eval(body)
            except TclBreak:
                break
            except TclContinue:
                continue
        return ""

    def c_foreach_in_collection(self, a):
        var, coll, body = self.to_str(a[0]), a[1], self.to_str(a[2])
        kind = coll.kind if isinstance(coll, Coll) else "port"
        for x in self.names_of(coll):
            self.vars[var] = Coll(kind, [x])
            try:
                self.eval(body)
            except TclBreak:
                break
            except TclContinue:
                continue
        return ""

    def c_for(self, a):
        init, cond, step, body = [self.to_str(x) for x in a]
        self.eval(init)
        guard = 0
        while self.truth(cond):
            try:
                self.eval(body)
            except TclBreak:
                break
            except TclContinue:
                pass
            self.eval(step)
            guard += 1
            if guard > 100000:
                raise DCError("loop limit exceeded", "SIM-2")
        return ""

    def c_while(self, a):
        cond, body = self.to_str(a[0]), self.to_str(a[1])
        guard = 0
        while self.truth(cond):
            try:
                self.eval(body)
            except TclBreak:
                break
            except TclContinue:
                pass
            guard += 1
            if guard > 100000:
                raise DCError("loop limit exceeded", "SIM-2")
        return ""

    def c_break(self, a):
        raise TclBreak()

    def c_continue(self, a):
        raise TclContinue()

    def c_incr(self, a):
        name = self.to_str(a[0])
        step = int(self.to_str(a[1])) if len(a) > 1 else 1
        v = int(self.to_str(self.vars.get(name, "0"))) + step
        self.vars[name] = str(v)
        return v

    def c_proc(self, a):
        name, params, body = self.to_str(a[0]), self.tcl_list(a[1]), self.to_str(a[2])
        self.procs[name] = (params, body)
        return ""

    def run_proc(self, name, args):
        params, body = self.procs[name]
        for i, p in enumerate(params):
            pl = self.tcl_list(p)
            if pl and pl[0] == "args":
                self.vars["args"] = self.list_str(args[i:])
                break
            if i < len(args):
                self.vars[pl[0]] = args[i] if isinstance(args[i], Coll) else self.to_str(args[i])
            elif len(pl) > 1:
                self.vars[pl[0]] = pl[1]
        return self.eval(body)

    def c_list(self, a):
        return self.list_str(a)

    def c_llength(self, a):
        return len(self.tcl_list(a[0])) if a else 0

    def c_lindex(self, a):
        lst = self.tcl_list(a[0])
        idx = self.to_str(a[1])
        i = len(lst) - 1 if idx == "end" else int(idx)
        return lst[i] if 0 <= i < len(lst) else ""

    def c_lappend(self, a):
        name = self.to_str(a[0])
        lst = self.tcl_list(self.vars.get(name, "")) + [self.to_str(x) for x in a[1:]]
        self.vars[name] = self.list_str(lst)
        return self.vars[name]

    def c_concat(self, a):
        return " ".join(self.to_str(x).strip() for x in a)

    def c_format(self, a):
        fmt = self.to_str(a[0])
        vals = []
        for x in a[1:]:
            s = self.to_str(x)
            try:
                vals.append(int(s))
            except ValueError:
                try:
                    vals.append(float(s))
                except ValueError:
                    vals.append(s)
        try:
            return fmt % tuple(vals)
        except Exception:
            raise DCError("bad format string \"%s\"" % fmt, "CMD-013")

    def c_string(self, a):
        sub = self.to_str(a[0]) if a else ""
        if sub == "toupper":
            return self.to_str(a[1]).upper()
        if sub == "tolower":
            return self.to_str(a[1]).lower()
        if sub == "length":
            return len(self.to_str(a[1]))
        if sub == "equal":
            return 1 if self.to_str(a[1]) == self.to_str(a[2]) else 0
        if sub == "match":
            return 1 if fnmatch.fnmatchcase(self.to_str(a[2]), self.to_str(a[1])) else 0
        raise DCError("string %s: not supported in simulator" % sub, "SIM-3")

    def c_date(self, a):
        return dc_date()

    def c_clear(self, a):
        if not self.streams:
            os.system("cls" if os.name == "nt" else "clear")
        return ""

    # ======================================================================
    # 시스템 명령
    # ======================================================================
    def c_source(self, a):
        o, pos = self.opts("source", a, flags=["-echo", "-verbose", "-continue_on_error"])
        if not pos:
            raise DCError("wrong # args: should be \"source file\"", "CMD-012")
        p = self.resolve(self.to_str(pos[0]))
        if not p:
            raise DCError("Cannot find the file '%s'." % self.to_str(pos[0]), "CMD-015")
        text = self.read_text(p)
        self.depth += 1
        try:
            return self.run_script(text, echo=("-echo" in o or "-verbose" in o))
        finally:
            self.depth -= 1

    def run_script(self, text, echo=False, quiet=False):
        """줄 단위로 명령을 실행 (에러가 나도 다음 줄 계속: dc_shell 동작과 동일)"""
        buf = ""
        last = ""
        for line in text.splitlines(True):
            buf += line
            if not self.is_complete(buf):
                continue
            cmd = buf.strip()
            buf = ""
            if not cmd or cmd.startswith("#"):
                continue
            if echo:
                self.out(cmd)
            last = self.exec_line(cmd, from_script=True, quiet=quiet)
        if buf.strip():
            self.exec_line(buf.strip(), from_script=True)
        return last if last else ""

    def c_redirect(self, a):
        o, pos = self.opts("redirect", a, flags=["-append", "-tee"], valued=["-file", "-variable"])
        if "-file" in o:
            fname, body = self.to_str(o["-file"]), self.to_str(pos[0])
        elif "-variable" in o:
            import io
            buf = io.StringIO()
            self.streams.append(buf)
            try:
                self.exec_body(self.to_str(pos[0]))
            finally:
                self.streams.pop()
            self.vars[self.to_str(o["-variable"])] = buf.getvalue()
            return ""
        else:
            if len(pos) < 2:
                raise DCError("wrong # args: should be \"redirect file {command}\"", "CMD-012")
            fname, body = self.to_str(pos[0]), self.to_str(pos[1])
        f = self.safe_open(fname, "-append" in o)
        self.streams.append(f)
        try:
            self.exec_body(body)
        finally:
            self.streams.pop()
            f.close()
        return ""

    def exec_body(self, body):
        try:
            r = self.eval(body)
            if r not in (None, ""):
                self.out(self.display(r))
        except DCError as e:
            self.print_error(e)

    def c_file(self, a):
        sub = self.to_str(a[0]) if a else ""
        if sub == "mkdir":
            for p in a[1:]:
                os.makedirs(self.to_str(p), exist_ok=True)
            return ""
        if sub == "exists":
            return 1 if os.path.exists(self.to_str(a[1])) else 0
        if sub in ("tail", "rootname", "dirname", "extension"):
            p = self.to_str(a[1])
            return {"tail": os.path.basename(p), "rootname": os.path.splitext(p)[0],
                    "dirname": os.path.dirname(p) or ".", "extension": os.path.splitext(p)[1]}[sub]
        raise DCError("file %s: not supported in simulator (mkdir/exists/tail/rootname/dirname)" % sub, "SIM-3")

    def c_history(self, a):
        for i, h in enumerate(self.history[-50:], max(1, len(self.history) - 49)):
            self.out("%5d  %s" % (i, h))
        return ""

    def c_ls(self, a):
        d = self.to_str(a[0]) if a else "."
        try:
            items = sorted(os.listdir(d))
        except OSError:
            raise DCError("Cannot open directory '%s'." % d, "SIM-4")
        for it in items:
            self.out(it + ("/" if os.path.isdir(os.path.join(d, it)) else ""))
        return ""

    def c_pwd(self, a):
        return os.getcwd()

    def c_cd(self, a):
        d = self.to_str(a[0]) if a else os.path.expanduser("~")
        try:
            os.chdir(d)
        except OSError:
            raise DCError("Can't change directory to '%s'." % d, "SIM-4")
        return ""

    def c_sh(self, a):
        raise DCError("'sh' (shell escape) is disabled in the simulator.", "SIM-5",
                      hint="ls / pwd / cd / file mkdir 는 내장 명령으로 지원합니다.")

    def c_exec(self, a):
        return self.c_sh(a)

    def c_exit(self, a):
        raise SystemExit(0)

    def c_alias(self, a):
        if len(a) >= 2:
            name, body = self.to_str(a[0]), " ".join(self.to_str(x) for x in a[1:])
            self.procs[name] = (["args"], body + " {*}$args".replace(" {*}$args", ""))
        return ""

    def c_help(self, a):
        o, pos = self.opts("help", a, flags=["-verbose"])
        if "-verbose" in o and pos:
            return self.c_help_man(pos)
        pat = self.to_str(pos[0]) if pos else "*"
        groups = OrderedDict()
        for k, (g, s, u) in HELP.items():
            if fnmatch.fnmatchcase(k, pat):
                groups.setdefault(g, []).append((k, s))
        if not groups:
            self.out("Error: No commands match '%s'. (CMD-040)" % pat, "31")
            return ""
        for g, items in groups.items():
            self.out("")
            self.out("%s:" % g)
            for k, s in items:
                self.out("   %-26s # %s" % (k, s))
        self.out("")
        self.out("  ※ 'help -verbose <명령>' 또는 'man <명령>' 으로 사용법을 볼 수 있습니다.")
        return ""

    def c_help_man(self, a):
        name = self.to_str(a[0]) if a else ""
        if name not in HELP:
            raise DCError("No manual entry for '%s'." % name, "CMD-041")
        g, s, u = HELP[name]
        self.out("")
        self.out("NAME")
        self.out("       %s  - %s" % (name, s))
        self.out("")
        self.out("SYNTAX")
        self.out("       %s" % u)
        self.out("")
        self.out("CATEGORY")
        self.out("       %s" % g)
        self.out("")
        return ""

    def c_tutorial(self, a):
        lines = """
  ---------------- 추천 학습 흐름 (dc_shell 기본 플로우) ----------------
  1) 라이브러리 설정  set_app_var target_library edu_lib_tt.db
                      set_app_var link_library   "* edu_lib_tt.db"
  2) RTL 읽기         analyze -format verilog [glob ./rtl/*.v]
                      elaborate top
  3) 디자인 지정      current_design top ; link ; check_design
  4) 제약 걸기        create_clock -period 2.0 -name clk [get_ports clk]
                      set_clock_uncertainty 0.1 [get_clocks clk]
                      set_input_delay  0.3 -clock clk [remove_from_collection [all_inputs] [get_ports clk]]
                      set_output_delay 0.3 -clock clk [all_outputs]
  5) 합성             compile          (또는 compile_ultra)
  6) 결과 확인        report_timing / report_area / report_power / report_qor
  7) 저장             write -format verilog -hierarchy -output outputs/top_netlist.v
                      write_sdc outputs/top.sdc

  * 실습 아이디어
    - 클럭을 0.8 로 줄여 slack (VIOLATED) 를 만들어 보고 compile_ultra 로 비교
    - target_library 를 edu_lib_ss.db 로 바꾸면 느려지는 것 확인
    - report_area 로 compile 전(GTECH)과 후 비교
    - 전체 자동 실행: (리눅스에서) dc_shell -f scripts/run_dc.tcl | tee dc.log
  ---------------------------------------------------------------------
"""
        for l in lines.strip("\n").splitlines():
            self.out(l, "36")
        return ""

    # ======================================================================
    # Read / Elaborate
    # ======================================================================
    def c_analyze(self, a):
        o, pos = self.opts("analyze", a, flags=["-autoread", "-recursive"], valued=["-format", "-library", "-define", "-work"])
        fmt = self.to_str(o.get("-format", "verilog")).lower()
        if fmt not in ("verilog", "sverilog", "vhdl"):
            raise DCError("Invalid format '%s'." % fmt, "CMD-019")
        if fmt == "vhdl":
            raise DCError("VHDL is not supported in the simulator.", "SIM-6")
        files = []
        for p in pos:
            files += self.tcl_list(p)
        if not files:
            raise DCError("Required argument 'file_list' was not found", "CMD-007")
        self.out("Running PRESTO HDLC")
        ok = True
        for fn in files:
            p = self.resolve(fn)
            if not p:
                self.out("Error: Cannot find the file '%s'. (FILE-1)" % fn, "31")
                self.hint("search_path 에 파일이 있는 폴더를 추가하세요. 예) set_app_var search_path \". ./rtl\"")
                ok = False
                continue
            self.out("Compiling source file %s" % p)
            try:
                mods, nlines = parse_verilog_text(self.read_text(p), p)
            except Exception as e:
                self.out("Error: %s:1: Syntax error. (VER-294)  [%s]" % (p, e), "31")
                ok = False
                continue
            if not mods:
                self.out("Warning: %s: No modules found in file. (VER-130)" % p, "33")
            for m in mods:
                if m.name in self.modules:
                    self.out("Information: Module '%s' is redefined; overwriting. (VER-123)" % m.name)
                self.modules[m.name] = m
                for d, n, msb, lsb in m.ports:
                    pass
        if not ok:
            self.out("*** Presto compilation terminated with errors. ***", "31")
            return 0
        self.out("Presto compilation completed successfully.")
        lib = self.vars.get("target_library", "")
        self.info("Loading db file '%s'" % os.path.basename(lib.split()[0]) if lib.split() else "Loading db file", None)
        return 1

    def c_elaborate(self, a):
        o, pos = self.opts("elaborate", a, flags=["-update", "-gate_clock"],
                           valued=["-parameters", "-architecture", "-library", "-work"])
        if not pos:
            raise DCError("Required argument 'design_name' was not found", "CMD-007")
        name = self.to_str(pos[0])
        if name not in self.modules:
            raise DCError("Can't find design '%s' in library 'WORK'." % name, "LBR-0",
                          hint="먼저 analyze 로 해당 .v 파일을 분석해야 합니다.")
        ov = {}
        if "-parameters" in o:
            for item in re.split(r"[,\s]+", self.to_str(o["-parameters"]).strip()):
                if "=" in item:
                    k, v = item.split("=", 1)
                    iv = eval_int(v, {})
                    if iv is None:
                        raise DCError("Invalid parameter value '%s'." % item, "ELAB-920")
                    if k not in [p[0] for p in self.modules[name].params]:
                        self.warn("Parameter '%s' is not declared in '%s'." % (k, name), "ELAB-321")
                        continue
                    ov[k] = iv
        self.out("Loading db file 'gtech.db'")
        self.out("Loading db file 'standard.sldb'")
        self.out("  Loading link library 'gtech'")
        self.out("Running PRESTO HDLC")
        before = set(self.designs)
        top = self.elab(name, ov)
        created = [x for x in self.designs if x not in before]
        self.out("Presto compilation completed successfully. (%s)" % top)
        self.out("Elaborated %d design%s." % (len(created) or 1, "s" if len(created) > 1 else ""))
        self.cur = top
        self.out("Current design is now '%s'." % top)
        return 1

    def elab(self, modname, ov, depth=0):
        mod = self.modules[modname]
        env = {}
        for pn, pe in mod.params:
            if pn in ov:
                env[pn] = ov[pn]
            else:
                v = eval_int(pe, env)
                env[pn] = v if v is not None else 0
        defaults = {}
        for pn, pe in mod.params:
            v = eval_int(pe, defaults)
            defaults[pn] = v if v is not None else 0
        suffix = "_".join("%s%s" % (k, ov[k]) for k, _ in mod.params if k in ov and ov[k] != defaults.get(k))
        dname = modname + ("_" + suffix if suffix else "")
        if dname in self.designs:
            return dname
        d = Design(dname, mod, env)

        def width(sig):
            msb, lsb = mod.sigs.get(sig, (None, None))
            if msb is None:
                return 1
            a1, b1 = eval_int(msb, env), eval_int(lsb, env)
            if a1 is None or b1 is None:
                return 8
            return abs(a1 - b1) + 1
        for dr, n, msb, lsb in mod.ports:
            d.ports.append((dr, n, width(n)))
        for n in mod.ff_names:
            d.flops.append((n, width(n)))
        self.designs[dname] = d
        if depth < 20:
            for typ, inst, pov, ppos, conn in mod.instances:
                if typ in self.modules:
                    cm = self.modules[typ]
                    cov = {}
                    for k, e in pov.items():
                        v = eval_int(e, env)
                        if v is not None:
                            cov[k] = v
                    for idx, e in enumerate(ppos):
                        if idx < len(cm.params):
                            v = eval_int(e, env)
                            if v is not None:
                                cov[cm.params[idx][0]] = v
                    cname = self.elab(typ, cov, depth + 1)
                    d.subs.append((inst, cname))
                else:
                    d.unresolved.append((inst, typ))
        # inferred memory table
        if d.flops:
            self.out("")
            self.out("Inferred memory devices in process")
            self.out("\tin routine %s line %s in file" % (modname, mod.ff_line or mod.line))
            self.out("\t\t'%s'." % mod.path)
            bar = "=" * 79
            self.out(bar)
            self.out("|    Register Name    |   Type    | Width | Bus | MB | AR | AS | SR | SS | ST |")
            self.out(bar)
            for n, w in d.flops:
                self.out("| %19s | Flip-flop | %5d |  %s  | N  | %s  | N  | N  | N  | N  |"
                         % ((n + "_reg")[:19], w, "Y" if w > 1 else "N", "Y" if mod.async_reset else "N"))
            self.out(bar)
        if not d.flops and not d.subs:
            pass
        self.out("Presto compilation completed successfully. (%s)" % dname) if depth else None
        return dname

    def c_read_verilog(self, a):
        o, pos = self.opts("read_verilog", a, flags=["-rtl", "-netlist"], valued=["-format", "-library"])
        if "-format" in o and self.to_str(o["-format"]).lower() not in ("verilog", "sverilog"):
            raise DCError("Only -format verilog is supported in the simulator.", "SIM-6")
        before = set(self.modules)
        r = self.c_analyze(["-format", "verilog"] + pos)
        if r != 1:
            return 0
        new = [m for m in self.modules if m not in before] or list(self.modules)
        inst_types = set()
        for m in new:
            for t in self.modules[m].instances:
                inst_types.add(t[0])
        tops = [m for m in new if m not in inst_types] or new
        for t in tops:
            self.out("Current design is now '%s'." % self.elab(t, {}))
        self.cur = tops[-1] if tops[-1] in self.designs else self.cur
        self.out("Loaded %d design%s." % (len(new), "s" if len(new) > 1 else ""))
        self.out("Current design is '%s'." % self.cur)
        return Coll("design", new)

    def c_current_design(self, a):
        if not a:
            return Coll("design", [self.cur]) if self.cur else ""
        name = self.to_str(self.names_of(a[0])[0]) if a else ""
        if name not in self.designs:
            raise DCError("Can't find design '%s'." % name, "UID-109",
                          hint="list_designs 로 이름을 확인하세요. (파라미터 지정 시 이름에 _WIDTH8 같은 접미사가 붙습니다)")
        self.cur = name
        self.out("Current design is '%s'." % name)
        return Coll("design", [name])

    def c_list_designs(self, a):
        for n in self.designs:
            self.out("%s%s" % (n, " (*)" if n == self.cur else ""))
        return 1

    def c_get_designs(self, a):
        pat = self.names_of(a[0]) if a else ["*"]
        res = [n for n in self.designs for p in pat if fnmatch.fnmatchcase(n, p)]
        return Coll("design", res) if res else self._empty("design", pat)

    def _empty(self, kind, pat):
        self.warn("No %s objects matched '%s'" % (kind, " ".join(pat)), "SEL-004")
        return ""

    def c_remove_design(self, a):
        o, pos = self.opts("remove_design", a, flags=["-all", "-hierarchy", "-designs"])
        if "-all" in o:
            n = len(self.designs)
            self.designs.clear()
            self.cur = None
            self.out("Removing %d designs." % n)
            return 1
        for p in pos:
            for n in self.names_of(p):
                if n in self.designs:
                    del self.designs[n]
                    self.out("Removing design '%s'" % n)
                    if self.cur == n:
                        self.cur = None
        return 1

    def c_link(self, a):
        d = self.need_design()
        lib = self.lib()
        ll = self.vars.get("link_library", "").split()
        missing = [x for x in ll if x != "*" and os.path.basename(x) not in LIBS and not self._lib_exists(x)]
        if missing:
            raise DCError("Can't read link_library file '%s'." % missing[0], "UID-3",
                          hint="set_app_var link_library \"* edu_lib_tt.db\"")
        self.out("")
        self.out("  Linking design '%s'" % d.name)
        self.out("  Using the following designs and libraries:")
        self.out("  " + "-" * 74)
        ok = 1
        for x in self.all_designs_under(d):
            # 미해결 참조 자동 해결 시도
            still = []
            for inst, typ in x.unresolved:
                if typ in self.modules:
                    x.subs.append((inst, self.elab(typ, {})))
                else:
                    still.append((inst, typ))
            x.unresolved = still
            for inst, typ in still:
                self.warn("Unable to resolve reference '%s' in '%s'." % (typ, x.name), "LINK-5")
                ok = 0
            x.linked = True
            self.out("  %-30s %s" % (x.name, x.mod.path))
        self.out("  %-30s %s" % ("%s (library)" % lib["name"], self.lib_file(lib)))
        self.out("")
        if not ok:
            self.hint("인스턴스한 하위 모듈의 .v 파일도 analyze 했는지 확인하세요.")
        return ok

    def c_uniquify(self, a):
        d = self.need_design()
        cnt = Counter(c for x in self.all_designs_under(d) for _, c in x.subs)
        for c, n in cnt.items():
            if n > 1:
                self.info("Uniquified %d instances of design '%s'." % (n, c), "OPT-1056")
        return 1

    def c_check_design(self, a):
        d = self.need_design()
        o, pos = self.opts("check_design", a, flags=["-summary", "-unmapped", "-html"])
        warns = []
        for x in self.all_designs_under(d):
            m = x.mod
            for dr, n, w in x.ports:
                uses = m.uses(n)
                conn_uses = sum(len(re.findall(r"\b%s\b" % re.escape(n), t[4])) for t in m.instances)
                if dr == "input" and uses + conn_uses == 0:
                    warns.append(("LINT-1", "In design '%s', input port '%s' is unconnected." % (x.name, n)))
                if dr == "output" and uses + conn_uses == 0 and n not in m.ff_names:
                    warns.append(("LINT-52", "In design '%s', output port '%s' is not driven (connected to logic 0)." % (x.name, n)))
            for inst, typ in x.unresolved:
                warns.append(("LINK-5", "In design '%s', reference '%s' (cell '%s') is unresolved." % (x.name, typ, inst)))
            if len([1 for _, c in x.subs]) != len(set(c for _, c in x.subs)):
                pass
        self.out("")
        self.out("****************************************")
        self.out("Report : check_design")
        self.out("Design : %s" % d.name)
        self.out("Version: %s" % REPORT_VERSION)
        self.out("Date   : %s" % dc_date())
        self.out("****************************************")
        self.out("")
        if not warns:
            self.out(" Information: check_design summary:")
            self.out(" Version:     %s" % VERSION)
            self.out(" Date:        %s" % dc_date())
            self.out(" -----------------------------------------------------------------------------")
            self.out("                                                                Total")
            self.out(" -----------------------------------------------------------------------------")
            self.out(" No warnings found.")
            self.out(" -----------------------------------------------------------------------------")
            return 1
        cnt = Counter(c for c, _ in warns)
        self.out(" Information: check_design summary:")
        self.out(" -----------------------------------------------------------------------------")
        self.out(" Type             Code        Total")
        self.out(" -----------------------------------------------------------------------------")
        for c, n in cnt.items():
            self.out(" %-16s %-10s %6d" % ("Connection" if c.startswith("LINT") else "Link", c, n))
        self.out(" -----------------------------------------------------------------------------")
        if "-summary" not in o:
            self.out("")
            for c, msg in warns:
                self.warn(msg, c)
        return 1

    # ======================================================================
    # Collections
    # ======================================================================
    def c_get_ports(self, a):
        d = self.need_design()
        o, pos = self.opts("get_ports", a, flags=["-quiet", "-regexp", "-nocase"], valued=["-filter"])
        pats = []
        for p in pos:
            pats += self.names_of(p)
        pats = pats or ["*"]
        res = []
        for pat in pats:
            pat2 = re.sub(r"\[.*\]$", "", pat)
            hit = [p[1] for p in d.ports if fnmatch.fnmatchcase(p[1], pat2)]
            if hit and pat != pat2:
                res.append(pat)
            else:
                res += hit
            if not hit and "-quiet" not in o:
                self.warn("No port objects matched '%s'" % pat, "SEL-004")
        if "-filter" in o:
            f = self.to_str(o["-filter"])
            m = re.search(r"direction\s*==\s*\"?(\w+)", f)
            if m:
                want = {"in": "input", "out": "output", "inout": "inout"}.get(m.group(1), m.group(1))
                res = [r for r in res if d.port(r) and d.port(r)[0] == want]
        return Coll("port", res) if res else ""

    def c_all_inputs(self, a):
        d = self.need_design()
        return Coll("port", [p[1] for p in d.ports if p[0] in ("input", "inout")])

    def c_all_outputs(self, a):
        d = self.need_design()
        return Coll("port", [p[1] for p in d.ports if p[0] in ("output", "inout")])

    def c_get_clocks(self, a):
        d = self.need_design()
        pats = self.names_of(a[0]) if a else ["*"]
        res = [c for c in d.clocks for p in pats if fnmatch.fnmatchcase(c, p)]
        if not res:
            self.warn("No clock objects matched '%s'" % " ".join(pats), "SEL-004")
            return ""
        return Coll("clock", res)

    def c_all_clocks(self, a):
        d = self.need_design()
        return Coll("clock", list(d.clocks)) if d.clocks else ""

    def c_all_registers(self, a):
        d = self.need_design()
        o, pos = self.opts("all_registers", a, flags=["-edge_triggered", "-clock_pins", "-data_pins", "-output_pins"],
                           valued=["-clock"])
        regs = d.flop_bits() if d.mapped else [n + "_reg" for n, _ in d.flops]
        sfx = "/CLK" if "-clock_pins" in o else "/D" if "-data_pins" in o else "/Q" if "-output_pins" in o else ""
        return Coll("cell", [r + sfx for r in regs]) if regs else ""

    def c_get_cells(self, a):
        d = self.need_design()
        o, pos = self.opts("get_cells", a, flags=["-hierarchical", "-quiet"], valued=["-filter", "-of_objects"])
        pats = []
        for p in pos:
            pats += self.names_of(p)
        pats = pats or ["*"]
        names = [i for i, _ in d.subs]
        names += d.flop_bits() if d.mapped else [n + "_reg" for n, _ in d.flops]
        if d.mapped and d.st:
            names += ["U%d" % i for i in range(1, min(d.st["ncomb"], 400) + 1)]
        if "-hierarchical" in o:
            for inst, c in d.subs:
                cd = self.designs.get(c)
                if cd:
                    names += [inst + "/" + x for x in ([n + "_reg" for n, _ in cd.flops])]
        res = [n for n in names if any(fnmatch.fnmatchcase(n, p) for p in pats)]
        if not res:
            if "-quiet" not in o:
                self.warn("No cell objects matched '%s'" % " ".join(pats), "SEL-004")
            return ""
        return Coll("cell", res)

    def c_get_nets(self, a):
        d = self.need_design()
        pats = self.names_of(a[-1]) if a else ["*"]
        nets = [p[1] for p in d.ports] + list(d.mod.sigs)
        res = [n for n in OrderedDict.fromkeys(nets) if any(fnmatch.fnmatchcase(n, p) for p in pats)]
        return Coll("net", res) if res else self._empty("net", pats)

    def c_get_lib_cells(self, a):
        lib = self.lib()
        pats = self.names_of(a[-1]) if a else ["*/*"]
        res = []
        for c in CELLS:
            full = "%s/%s" % (lib["name"], c)
            if any(fnmatch.fnmatchcase(full, p) or fnmatch.fnmatchcase(c, p) for p in pats):
                res.append(full)
        return Coll("lib_cell", res) if res else self._empty("lib_cell", pats)

    def c_get_libs(self, a):
        return Coll("lib", [LIBS[os.path.basename(x)]["name"] for x in self.vars["link_library"].split()
                            if os.path.basename(x) in LIBS])

    def c_remove_from_collection(self, a):
        base, rem = a[0], a[1] if len(a) > 1 else ""
        kind = base.kind if isinstance(base, Coll) else "port"
        rset = set(self.names_of(rem))
        return Coll(kind, [n for n in self.names_of(base) if n not in rset])

    def c_add_to_collection(self, a):
        kind = a[0].kind if isinstance(a[0], Coll) else "port"
        return Coll(kind, self.names_of(a[0]) + (self.names_of(a[1]) if len(a) > 1 else []))

    def c_sizeof_collection(self, a):
        return len(self.names_of(a[0])) if a and a[0] != "" else 0

    def c_get_object_name(self, a):
        return self.list_str(self.names_of(a[0])) if a else ""

    def c_query_objects(self, a):
        self.out(self.display(a[0]) if a else "")
        return ""

    def c_list_libs(self, a):
        self.out("Library        File              Path                         Operating condition")
        self.out("-------        ----              ----                         -------------------")
        for f, l in LIBS.items():
            self.out("%-14s %-17s %-28s %s" % (l["name"], f, "(simulated)/lib/", l["desc"]))
        self.out("gtech          gtech.db          (simulated)/libraries/syn/")
        self.out("standard.sldb  standard.sldb     (simulated)/libraries/syn/")
        return 1

    def c_report_lib(self, a):
        name = self.to_str(a[0]) if a else ""
        lib = None
        for f, l in LIBS.items():
            if name in (l["name"], f):
                lib = l
        if not lib:
            raise DCError("Can't find library '%s'." % name, "UID-109", hint="list_libs 참고")
        self.out("")
        self.out("Library  : %s" % lib["name"])
        self.out("Op cond  : %s  (%.2fV)" % (lib["oc"], lib["volt"]))
        self.out("Time unit: 1ns   Cap unit: 1pF   Area unit: um^2   Leakage unit: 1pW")
        self.out("")
        self.out("Cell        Area     Delay(ns)  Type   Pins")
        self.out("-" * 60)
        for c, (ar, dl, k, ins, outp) in CELLS.items():
            self.out("%-10s %7.3f  %8.3f   %-5s  %s -> %s" % (c, ar, dl * lib["scale"], k, ",".join(ins), outp))
        return 1

    # ======================================================================
    # Constraints
    # ======================================================================
    def sdc(self, d, words):
        d.sdc.append(words)

    def c_create_clock(self, a):
        d = self.need_design()
        o, pos = self.opts("create_clock", a, flags=["-add"], valued=["-period", "-name", "-waveform"])
        if "-period" not in o:
            raise DCError("Required argument '-period' was not found", "CMD-007")
        per = self.fnum(o["-period"], "-period")
        if per <= 0:
            raise DCError("Value for '-period' must be greater than 0.", "CMD-019")
        srcs = []
        for p in pos:
            srcs += self.ports_from(d, [p])
        name = self.to_str(o.get("-name", srcs[0] if srcs else ""))
        if not name:
            raise DCError("Must specify either -name or source objects.", "UID-80")
        wave = [self.fnum(x) for x in self.tcl_list(o.get("-waveform", "0 %s" % self.to_str(per / 2)))]
        d.clocks[name] = dict(period=per, wave=wave, src=srcs)
        self.sdc(d, "create_clock [get_ports {%s}] -name %s -period %s -waveform {%s}" % (
            " ".join(srcs), name, self.to_str(per), " ".join(self.to_str(w) for w in wave)) if srcs else
            "create_clock -name %s -period %s -waveform {%s}" % (name, self.to_str(per), " ".join(self.to_str(w) for w in wave)))
        d.mapped_stale = True
        if not srcs:
            self.info("Creating virtual clock '%s'." % name, "UID-?")
        return 1

    def _clk_targets(self, d, pos):
        names = []
        for p in pos:
            names += self.names_of(p)
        return names or list(d.clocks)

    def c_set_clock_uncertainty(self, a):
        d = self.need_design()
        o, pos = self.opts("set_clock_uncertainty", a, flags=["-setup", "-hold"], valued=["-from", "-to"])
        if not pos:
            raise DCError("Required argument 'uncertainty' was not found", "CMD-007")
        v = self.fnum(pos[0])
        for c in self._clk_targets(d, pos[1:]):
            if c not in d.clocks:
                self.warn("Can't find clock '%s'." % c, "UID-95")
                continue
            if "-hold" in o:
                d.unc[c + ":hold"] = v
            else:
                d.unc[c] = v
            self.sdc(d, "set_clock_uncertainty %s%s [get_clocks %s]" % ("-hold " if "-hold" in o else "", self.to_str(v), c))
        return 1

    def c_set_clock_latency(self, a):
        d = self.need_design()
        o, pos = self.opts("set_clock_latency", a, flags=["-source", "-max", "-min", "-rise", "-fall"])
        v = self.fnum(pos[0])
        for c in self._clk_targets(d, pos[1:]):
            d.latency[c] = v
            self.sdc(d, "set_clock_latency %s%s [get_clocks %s]" % ("-source " if "-source" in o else "", self.to_str(v), c))
        return 1

    def c_set_clock_transition(self, a):
        d = self.need_design()
        o, pos = self.opts("set_clock_transition", a, flags=["-max", "-min", "-rise", "-fall"])
        v = self.fnum(pos[0])
        for c in self._clk_targets(d, pos[1:]):
            d.transition[c] = v
            self.sdc(d, "set_clock_transition %s [get_clocks %s]" % (self.to_str(v), c))
        return 1

    def _io_delay(self, a, cmd, store):
        d = self.need_design()
        o, pos = self.opts(cmd, a, flags=["-max", "-min", "-add_delay", "-clock_fall", "-rise", "-fall"],
                           valued=["-clock"])
        if not pos:
            raise DCError("Required argument 'delay_value' was not found", "CMD-007")
        v = self.fnum(pos[0], "delay_value")
        clk = self.to_str(self.names_of(o["-clock"])[0]) if "-clock" in o else None
        if clk and clk not in d.clocks:
            raise DCError("Can't find clock '%s'." % clk, "UID-95", hint="create_clock 을 먼저 실행하세요.")
        if not clk:
            self.warn("No clock specified for %s; treated as unclocked." % cmd, "UID-347")
        ports = self.ports_from(d, pos[1:])
        if not ports:
            raise DCError("Required argument 'port_pin_list' was not found or empty", "CMD-007")
        for p in ports:
            pp = d.port(p)
            if pp and clk and p in d.clocks.get(clk, {}).get("src", []):
                self.warn("Setting %s on clock port '%s'." % (cmd, p), "UID-401")
            store[p] = (v, clk)
        self.sdc(d, "%s %s%s [get_ports {%s}]" % (cmd, ("-clock %s " % clk) if clk else "", self.to_str(v), " ".join(ports)))
        return 1

    def c_set_input_delay(self, a):
        return self._io_delay(a, "set_input_delay", self.need_design().in_delay)

    def c_set_output_delay(self, a):
        return self._io_delay(a, "set_output_delay", self.need_design().out_delay)

    def c_set_load(self, a):
        d = self.need_design()
        o, pos = self.opts("set_load", a, flags=["-pin_load", "-wire_load", "-max", "-min", "-subtract_pin_load"])
        v = self.fnum(pos[0])
        ports = self.ports_from(d, pos[1:])
        for p in ports:
            d.loads[p] = v
        self.sdc(d, "set_load -pin_load %s [get_ports {%s}]" % (self.to_str(v), " ".join(ports)))
        return 1

    def c_set_driving_cell(self, a):
        d = self.need_design()
        o, pos = self.opts("set_driving_cell", a, flags=["-no_design_rule", "-max", "-min"],
                           valued=["-lib_cell", "-library", "-pin", "-from_pin"])
        cell = self.to_str(o.get("-lib_cell", "BUFX2")).split("/")[-1]
        if cell not in CELLS:
            raise DCError("Can't find lib_cell '%s'." % cell, "UID-993", hint="report_lib edu_lib_tt 로 셀 이름 확인")
        ports = self.ports_from(d, pos)
        for p in ports:
            d.driving[p] = cell
        self.sdc(d, "set_driving_cell -lib_cell %s [get_ports {%s}]" % (cell, " ".join(ports)))
        return 1

    def c_set_max_area(self, a):
        d = self.need_design()
        d.max_area = self.fnum(a[0])
        self.sdc(d, "set_max_area %s" % self.to_str(d.max_area))
        return 1

    def c_set_max_fanout(self, a):
        d = self.need_design()
        d.max_fanout = self.fnum(a[0])
        self.sdc(d, "set_max_fanout %s [current_design]" % self.to_str(d.max_fanout))
        return 1

    def c_set_max_transition(self, a):
        d = self.need_design()
        d.max_transition = self.fnum(a[0])
        self.sdc(d, "set_max_transition %s [current_design]" % self.to_str(d.max_transition))
        return 1

    def c_set_false_path(self, a):
        d = self.need_design()
        o, pos = self.opts("set_false_path", a, flags=["-setup", "-hold", "-rise", "-fall"],
                           valued=["-from", "-to", "-through"])
        fr = self.names_of(o.get("-from", ""))
        to = self.names_of(o.get("-to", ""))
        d.false_paths.append((fr, to))
        self.sdc(d, "set_false_path%s%s" % ((" -from {%s}" % " ".join(fr)) if fr else "",
                                            (" -to {%s}" % " ".join(to)) if to else ""))
        return 1

    def c_set_multicycle_path(self, a):
        d = self.need_design()
        o, pos = self.opts("set_multicycle_path", a, flags=["-setup", "-hold", "-start", "-end"],
                           valued=["-from", "-to", "-through"])
        n = int(self.fnum(pos[0])) if pos else 2
        d.mcp.append((n, self.names_of(o.get("-from", "")), self.names_of(o.get("-to", ""))))
        self.sdc(d, "set_multicycle_path %d%s" % (n, " -setup" if "-setup" in o else ""))
        return 1

    def c_set_dont_touch(self, a):
        o, pos = self.opts("set_dont_touch", a)
        val = self.to_str(pos[1]) if len(pos) > 1 else "true"
        for n in self.names_of(pos[0]) if pos else []:
            if n in self.designs:
                self.designs[n].dont_touch = val in ("true", "1")
        return 1

    def c_set_dont_touch_network(self, a):
        return 1

    def c_set_ideal_network(self, a):
        return 1

    def c_set_fix_multiple_port_nets(self, a):
        return 1

    def c_set_fix_hold(self, a):
        return 1

    def c_group_path(self, a):
        return 1

    def c_set_wire_load_model(self, a):
        d = self.need_design()
        o, pos = self.opts("set_wire_load_model", a, valued=["-name", "-library"])
        d.wlm = self.to_str(o.get("-name", pos[0] if pos else "ForQA"))
        self.sdc(d, "set_wire_load_model -name %s" % d.wlm)
        return 1

    def c_set_wire_load_mode(self, a):
        return 1

    def c_set_operating_conditions(self, a):
        d = self.need_design()
        o, pos = self.opts("set_operating_conditions", a, valued=["-max", "-min", "-library", "-analysis_type"])
        d.oc = self.to_str(o.get("-max", pos[0] if pos else ""))
        return 1

    def c_reset_design(self, a):
        d = self.need_design()
        keep = (d.name, d.mod, d.env, d.ports, d.flops, d.subs, d.unresolved, d.linked, d.mapped, d.flat, d.st, d.est, d.effort)
        d.__init__(d.name, d.mod, d.env)
        (d.name, d.mod, d.env, d.ports, d.flops, d.subs, d.unresolved, d.linked, d.mapped, d.flat, d.st, d.est, d.effort) = keep
        self.out("Resetting current design '%s'." % d.name)
        return 1

    def c_check_timing(self, a):
        d = self.need_design()
        self.out("")
        self.out("Information: Checking generated_clocks...")
        self.out("Information: Checking loops...")
        self.out("Information: Checking no_input_delay...")
        clk_ports = set(p for c in d.clocks.values() for p in c["src"])
        noin = [p[1] for p in d.ports if p[0] == "input" and p[1] not in d.in_delay and p[1] not in clk_ports]
        noout = [p[1] for p in d.ports if p[0] == "output" and p[1] not in d.out_delay]
        if noin:
            self.out("")
            self.warn("The following input ports have no input delay specified:", "TIM-111")
            for p in noin:
                self.out("        %s" % p)
        self.out("Information: Checking unconstrained_endpoints...")
        if noout:
            self.out("")
            self.warn("The following end-points are not constrained for maximum delay.", "TIM-104")
            self.out("        End point")
            self.out("        ---------------")
            for p in noout:
                self.out("        %s" % p)
        self.out("Information: Checking pulse_clock_cell_type...")
        self.out("Information: Checking no_driving_cell...")
        self.out("Information: Checking partial_input_delay...")
        if not d.clocks:
            self.out("")
            self.warn("Design '%s' has no clocks defined; all register endpoints are unclocked." % d.name, "TIM-208")
            self.hint("create_clock -period <ns> [get_ports clk] 로 클럭을 정의하세요.")
        return 1

    # ======================================================================
    # 합성 엔진 (가상)
    # ======================================================================
    def own_stats(self, d, effort, ultra, lib, mapped=True):
        m = d.mod
        rng = random.Random(stable_seed(d.name, effort, ultra, lib["name"]))
        cells = Counter()
        w = min(max(d.datawidth, 1), 64)
        nflop = sum(fw for _, fw in d.flops)
        if nflop:
            cells["DFFARX1" if m.async_reset else "DFFX1"] += nflop
        ops = m.ops
        glue_f = {"low": 1.15, "medium": 1.0, "high": 0.92}.get(effort, 1.0) * (0.85 if ultra else 1.0)

        def add(c, n):
            n = int(round(n * rng.uniform(0.9, 1.1)))
            if n > 0:
                cells[c] += n
        arith = ops["add"] + ops["sub"]
        for _ in range(arith):
            if ultra:
                add("FADDX1", w * 0.55)
                add("XOR2X1", w * 0.4)
                add("AOI21X1", w * 0.5)
                add("NAND2X1", w * 0.4)
            else:
                add("FADDX1", w * 0.9)
                add("HADDX1", w * 0.15)
        if ops["sub"]:
            add("INVX1", w * ops["sub"] * 0.7)
        for _ in range(ops["mul"]):
            mw = min(w, 32)
            add("FADDX1", mw * mw * (0.75 if ultra else 0.9))
            add("AND2X1", mw * mw)
            add("HADDX1", mw)
        for _ in range(ops["cmp"]):
            add("XNOR2X1", w * 0.8)
            add("NAND2X1", w * 0.5)
            add("NOR2X1", w * 0.3)
        add("MUX21X1", min(ops["mux"], 12) * w * 0.8 * glue_f)
        add("AO22X1", ops["shift"] * w * 1.5)
        add("XOR2X1", ops["logic"] * w * 0.25)
        io_bits = sum(pw for _, _, pw in d.ports)
        glue = (io_bits * 0.6 + nflop * 0.7 + ops["logic"] * w * 0.4) * glue_f
        for c, f in (("NAND2X1", 0.3), ("NOR2X1", 0.15), ("AOI21X1", 0.15), ("OAI21X1", 0.15),
                     ("INVX1", 0.15), ("AND2X1", 0.05), ("OR2X1", 0.05)):
            add(c, glue * f)
        ncomb_tmp = sum(v for k, v in cells.items() if CELLS[k][2] != "seq")
        add("BUFX2", (nflop + ncomb_tmp) / 14.0)
        if not cells:
            cells["BUFX2"] = 1
        # delay (ns) - ideal, before lib scale
        dl = 0.10 + rng.uniform(0.0, 0.04)
        if arith:
            dl += (0.12 * math.log2(w + 1) + 0.10) if ultra else (0.042 * w + 0.06) * (0.93 if effort == "high" else 1.0)
            dl += 0.03 * min(arith - 1, 4)
        if ops["mul"]:
            dl += (0.045 if ultra else 0.07) * min(w, 32) + 0.15
        if ops["cmp"]:
            dl += 0.035 * math.log2(w + 1) + 0.06
        dl += 0.07 * min(ops["mux"], 5)
        dl += 0.04 * min(ops["logic"], 4) + 0.05 * min(ops["shift"], 3)
        if effort == "low":
            dl *= 1.12
        dl *= lib["scale"]
        return dict(cells=cells, delay=dl, levels=max(2, int(dl / (0.06 * lib["scale"]))))

    def finalize(self, d, st, lib, mapped):
        cells = st["cells"]
        st["ncomb"] = sum(v for k, v in cells.items() if CELLS[k][2] != "seq")
        st["nseq"] = sum(v for k, v in cells.items() if CELLS[k][2] == "seq")
        st["nbuf"] = sum(v for k, v in cells.items() if CELLS[k][2] == "buf")
        st["ninv"] = sum(v for k, v in cells.items() if CELLS[k][2] == "inv")
        f = 1.0 if mapped else 0.0
        st["comb_area"] = f * sum(self.ci(k, lib)[0] * v for k, v in cells.items() if CELLS[k][2] != "seq")
        st["seq_area"] = f * sum(self.ci(k, lib)[0] * v for k, v in cells.items() if CELLS[k][2] == "seq")
        st["bi_area"] = f * sum(self.ci(k, lib)[0] * v for k, v in cells.items() if CELLS[k][2] in ("buf", "inv"))
        st["mapped"] = mapped
        st["lib"] = lib
        return st

    def timing_targets(self, d):
        """현재 디자인 제약 기준 (period, uncertainty)"""
        if not d.clocks:
            return None, 0.0
        c = list(d.clocks)[0]
        return d.clocks[c]["period"], d.unc.get(c, 0.0)

    def synth(self, top, effort, ultra, lib, area_effort="medium", autoungroup=True, incremental=False):
        """top 이하 계층을 가상 합성. 결과를 각 Design.st 에 저장"""
        per, unc = self.timing_targets(top)
        designs = list(reversed(self.all_designs_under(top)))
        log = []
        for d in designs:
            if d.dont_touch and d.mapped:
                continue
            st = self.own_stats(d, effort, ultra, lib)
            if incremental and d.st:
                st["cells"] = Counter(d.st["cells"])
                st["delay"] = d.st["delay"]
            # 하위 계층 흡수 (compile_ultra auto-ungroup / ungroup -flatten)
            if not d.flat and ultra and autoungroup:
                keep = []
                for inst, c in d.subs:
                    cd = self.designs.get(c)
                    if cd and cd.st and (sum(cd.st["cells"].values()) < 120 and not cd.subs) and not cd.dont_touch:
                        log.append("Information: Ungrouping hierarchy %s before Pass 1 (OPT-776)" % inst)
                        st["cells"] += Counter({k: v for k, v in cd.st["cells"].items()})
                        st["delay"] = max(st["delay"], cd.st["delay"] * 0.95)
                        st.setdefault("absorbed", []).append((inst, c))
                    else:
                        keep.append((inst, c))
                d.subs_eff = keep
            elif d.flat:
                for x in self.all_designs_under(d)[1:]:
                    pass
                d.subs_eff = []
            else:
                d.subs_eff = list(d.subs)
            # 타이밍 드리븐 최적화
            if per:
                clk2q = self.ci("DFFARX1", lib)[1]
                req = per - unc - self.setup_t(lib) - clk2q
                child_max = 0.0
                for inst, c in d.subs_eff:
                    cd = self.designs.get(c)
                    if cd and cd.st:
                        child_max = max(child_max, cd.st["delay"] + 0.08 * lib["scale"])
                tot = max(st["delay"], child_max)
                if st["delay"] > req and req > 0:
                    gap = (st["delay"] - req) / st["delay"]
                    maxgain = {"low": 0.08, "medium": 0.16, "high": 0.24}.get(effort, 0.16) + (0.12 if ultra else 0)
                    rng = random.Random(stable_seed(d.name, per, "opt"))
                    imp = min(gap, maxgain)
                    if gap <= maxgain:
                        st["delay"] = req * rng.uniform(0.965, 0.995)
                    else:
                        st["delay"] *= (1 - imp)
                    upsz = imp * 1.4
                    for k in list(st["cells"]):
                        if CELLS[k][2] in ("comb",):
                            st["cells"][k] = int(round(st["cells"][k] * (1 + upsz * 0.3)))
                    st["cells"]["NAND2X2"] += int(sum(st["cells"].values()) * upsz * 0.2) + 1
                    st["cells"]["INVX4"] += int(sum(st["cells"].values()) * upsz * 0.08) + 1
                    st["upsized"] = imp
                elif area_effort == "high" or d.max_area is not None:
                    for k in list(st["cells"]):
                        if CELLS[k][2] == "comb":
                            st["cells"][k] = int(round(st["cells"][k] * 0.95))
            self.finalize(d, st, lib, True)
            d.st = st
            d.mapped = True
            d.effort = ("ultra" if ultra else effort)
        return log

    def estimate(self, d, lib):
        """compile 전 GTECH 추정"""
        for x in reversed(self.all_designs_under(d)):
            if not x.mapped:
                st = self.own_stats(x, "medium", False, lib)
                st["delay"] *= 1.25
                self.finalize(x, st, lib, False)
                x.est = st
                x.subs_eff = list(x.subs)

    def stats(self, d):
        return d.st if d.mapped and d.st else d.est

    def ensure_stats(self, d):
        try:
            lib = self.lib()
        except DCError:
            lib = LIBS["edu_lib_tt.db"]
        if not d.mapped or not d.st:
            self.estimate(d, lib)
        return lib

    def totals(self, d):
        """하위 계층 포함 합계"""
        st = self.stats(d)
        tot = dict(cells=Counter(st["cells"]), comb_area=st["comb_area"], seq_area=st["seq_area"],
                   bi_area=st["bi_area"], ncomb=st["ncomb"], nseq=st["nseq"], nbuf=st["nbuf"], ninv=st["ninv"], nhier=0)
        for inst, c in getattr(d, "subs_eff", d.subs):
            cd = self.designs.get(c)
            if not cd or not self.stats(cd):
                continue
            ct = self.totals(cd)
            tot["cells"] += ct["cells"]
            for k in ("comb_area", "seq_area", "bi_area", "ncomb", "nseq", "nbuf", "ninv"):
                tot[k] += ct[k]
            tot["nhier"] += 1 + ct["nhier"]
        return tot

    def crit(self, d):
        st = self.stats(d)
        best = (st["delay"], "", d)
        for inst, c in getattr(d, "subs_eff", d.subs):
            cd = self.designs.get(c)
            if not cd or not self.stats(cd):
                continue
            cdl, cp, cdes = self.crit(cd)
            v = cdl + 0.08 * st["lib"]["scale"]
            if v > best[0]:
                best = (v, inst + "/" + cp, cdes)
        return best

    def all_flop_bits(self, d, prefix=""):
        bits = [(prefix + b, d) for b in d.flop_bits()]
        for inst, c in getattr(d, "subs_eff", d.subs):
            cd = self.designs.get(c)
            if cd:
                bits += self.all_flop_bits(cd, prefix + inst + "/")
        st = self.stats(d)
        for inst, c in (st.get("absorbed", []) if st else []):
            cd = self.designs.get(c)
            if cd:
                bits += [(inst + "/" + b, d) for b in cd.flop_bits()]
        return bits

    # ---------------- compile 출력 -----------------
    def progress_rows(self, d, phase_rows, area0, area1, wns0, wns1, tns0, tns1, endpoint):
        for i in range(phase_rows):
            t = i / max(phase_rows - 1, 1)
            area = area0 + (area1 - area0) * t
            wns = wns0 + (wns1 - wns0) * t
            tns = tns0 + (tns1 - tns0) * t
            self.tick = getattr(self, "tick", 0) + 1
            sec = self.tick // 2 + 1
            self.out("    0:00:%02d %9.1f %9.2f %9.1f %9.1f %s" % (
                sec, area, max(-wns, 0.0) if wns < 0 else 0.0, max(tns, 0.0), 0.0, endpoint if wns < 0 else ""))
            self.pause(0.08)

    def run_compile(self, ultra, a):
        self.tick = 0
        if ultra:
            o, pos = self.opts("compile_ultra", a,
                               flags=["-no_autoungroup", "-no_boundary_optimization", "-retime", "-gate_clock",
                                      "-incremental", "-timing_high_effort_script", "-area_high_effort_script",
                                      "-exact_map", "-no_seq_output_inversion", "-spg", "-scan"])
            effort = "high"
            area_effort = "high" if "-area_high_effort_script" in o else "medium"
        else:
            o, pos = self.opts("compile", a,
                               flags=["-incremental_mapping", "-exact_map", "-ungroup_all", "-boundary_optimization",
                                      "-no_design_rule", "-only_design_rule", "-gate_clock", "-scan"],
                               valued=["-map_effort", "-area_effort", "-power_effort"])
            effort = self.to_str(o.get("-map_effort", "medium"))
            if effort not in ("low", "medium", "high"):
                raise DCError("Invalid value '%s' for option '-map_effort'." % effort, "CMD-019")
            area_effort = self.to_str(o.get("-area_effort", "medium"))
        d = self.need_design()
        lib = self.lib()
        incremental = "-incremental" in o or "-incremental_mapping" in o
        if incremental and not d.mapped:
            self.warn("Design '%s' is not mapped; -incremental ignored." % d.name, "OPT-1206")
            incremental = False
        if not d.linked:
            self.c_link([])
        if "-ungroup_all" in o:
            d.flat = True
        name = "compile_ultra" if ultra else "compile"
        self.info("Starting %s (simulated)." % name)
        self.out("Information: Evaluating DesignWare library utilization. (UISN-27)")
        self.out("")
        bar = "=" * 76
        self.out(bar)
        self.out("| DesignWare Building Block Library  |         Version         | Available |")
        self.out(bar)
        self.out("| Basic DW Building Blocks           | %-23s |     *     |" % VERSION)
        self.out("| Licensed DW Building Blocks        | %-23s |     *     |" % VERSION)
        self.out(bar)
        self.out("")
        if ultra:
            self.out("Information: Sequential output inversion is enabled.  SVF file must be used for formal verification. (OPT-1208)")
            if "-retime" in o:
                self.out("Information: Retiming is enabled. (RTDC-1)")
        if not d.clocks:
            self.warn("Design '%s' has no clocks defined; timing-driven optimization is disabled." % d.name, "OPT-1010")
            self.hint("create_clock 없이 compile 하면 면적 위주로만 최적화됩니다.")
        alld = list(reversed(self.all_designs_under(d)))
        self.out("  Simplifying Design '%s'" % d.name)
        self.pause(0.2)
        # 미리 GTECH 추정치 (진행표 시작값)
        self.estimate(d, lib)
        old = {x.name: (x.mapped, x.st) for x in alld}
        for x in alld:
            if not incremental:
                x.mapped = False
                x.st = None
            x.subs_eff = list(x.subs)
        self.estimate(d, lib)
        est_tot = self.totals(d)
        est_area = sum(self.ci(k, lib)[0] * v for k, v in est_tot["cells"].items()) * 1.18
        est_delay = self.crit(d)[0]
        log = self.synth(d, effort, ultra, lib, area_effort, autoungroup="-no_autoungroup" not in o,
                         incremental=incremental)
        for l in log:
            self.out(l)
        self.out("")
        self.out("Loaded alib file './alib-52/%s.db.alib' (placeholder)" % lib["name"]) if ultra else None
        self.out("  Building model 'DW01_NAND2'")
        self.out("  Processing 'DW01_NAND2'")
        self.out("")
        self.out("  Beginning Pass 1 Mapping")
        self.out("  ------------------------")
        for x in alld:
            self.out("  Processing '%s'" % x.name)
            self.pause(0.06)
            for k, n in (("add", "DW01_add"), ("sub", "DW01_sub"), ("mul", "DW02_mult"), ("cmp", "DW01_cmp6")):
                for i in range(min(x.mod.ops[k], 3)):
                    self.out("  Processing '%s_%s_%d'" % (x.name, n, i))
        self.out("")
        self.out("  Updating timing information")
        self.info("Updating design information...", "UID-85")
        self.out("")
        self.out("  Beginning Implementation Selection")
        self.out("  ----------------------------------")
        for x in alld:
            for k, n in (("add", "DW01_add"), ("sub", "DW01_sub"), ("mul", "DW02_mult")):
                for i in range(min(x.mod.ops[k], 2)):
                    impl = ("pparch" if ultra else "rpl") if k != "mul" else ("pparch" if ultra else "csa")
                    self.out("  Mapping '%s_%s_%d' (%s)" % (x.name, n, i, impl))
        self.out("")
        tot = self.totals(d)
        area1 = tot["comb_area"] + tot["seq_area"]
        crit = self.crit(d)[0]
        per, unc = self.timing_targets(d)
        if per:
            clk2q = self.ci("DFFARX1", lib)[1]
            wns1 = per - unc - self.setup_t(lib) - clk2q - crit
            wns0 = per - unc - self.setup_t(lib) - clk2q - est_delay
        else:
            wns0 = wns1 = 0.0
        nbits = max(len(self.all_flop_bits(d)), 1)
        tns0 = max(-wns0, 0) * nbits * 0.4
        tns1 = max(-wns1, 0) * nbits * 0.25
        _, cpre, _ = self.crit(d)
        _bits = [x for x, _ in self.all_flop_bits(d) if x.startswith(cpre)] or [x for x, _ in self.all_flop_bits(d)] or ["out"]
        endpoint = _bits[-1] + ("/D" if _bits[-1] != "out" else "")
        hdr = ["                                  TOTAL",
               "   ELAPSED            WORST NEG   SETUP    DESIGN",
               "    TIME      AREA      SLACK     COST    RULE COST         ENDPOINT",
               "  --------- --------- --------- --------- --------- -------------------------"]
        self.out("  Beginning Mapping Optimizations  (%s effort)" % ("Ultra" if ultra else effort.capitalize()))
        self.out("  -------------------------------")
        for h in hdr:
            self.out(h)
        mid_area = est_area * 0.92 + area1 * 0.08
        mid_wns = wns0 * 0.6 + wns1 * 0.4
        self.progress_rows(d, 4, est_area, mid_area, wns0, mid_wns, tns0, (tns0 + tns1) / 2, endpoint)
        self.out("")
        self.out("  Beginning Delay Optimization Phase")
        self.out("  ----------------------------------")
        for h in hdr:
            self.out(h)
        self.progress_rows(d, 4, mid_area, area1 * 1.04, mid_wns, wns1, (tns0 + tns1) / 2, tns1, endpoint)
        self.out("")
        self.out("  Beginning Area-Recovery Phase  (cleanup)")
        self.out("  -----------------------------")
        for h in hdr:
            self.out(h)
        self.progress_rows(d, 3, area1 * 1.04, area1, wns1, wns1, tns1, tns1, endpoint)
        self.out("Loading db file '%s.db'" % lib["name"])
        self.out("")
        self.out("")
        self.out("  Optimization Complete")
        self.out("  ---------------------")
        self.info("State dependent leakage is now switched from on to off.", "PWR-102") if ultra else None
        self.out("")
        if per and wns1 < 0:
            self.hint("타이밍 위반이 남았습니다(WNS %.3f). report_timing 으로 경로를 보고, 클럭 완화/compile_ultra/"
                      "RTL 파이프라이닝을 고려하세요." % wns1)
        self.cpu += 1.5 + area1 / 2000.0
        return 1

    def c_compile(self, a):
        return self.run_compile(False, a)

    def c_compile_ultra(self, a):
        return self.run_compile(True, a)

    def c_ungroup(self, a):
        d = self.need_design()
        o, pos = self.opts("ungroup", a, flags=["-all", "-flatten", "-simple_names", "-small"])
        if "-all" in o:
            for x in self.all_designs_under(d):
                if x is not d:
                    pass
            n = len(d.subs)
            d.flat = True
            # 하위 셀을 현재 디자인으로 흡수(가상): 하위 모듈 ops/flop 합침
            self._flatten(d)
            self.out("Information: Ungrouping %d of %d hierarchies before Pass 1 (OPT-775)" % (n, n))
            return 1
        names = []
        for p in pos:
            names += self.names_of(p)
        hit = [s for s in d.subs if s[0] in names]
        if not hit:
            raise DCError("Can't find cell '%s'." % " ".join(names), "UID-109")
        self._flatten(d, only=set(names))
        self.out("Information: Ungrouping %d of %d hierarchies (OPT-775)" % (len(hit), len(d.subs) + len(hit)))
        return 1

    def _flatten(self, d, only=None):
        keep = []
        for inst, c in d.subs:
            if only is not None and inst not in only:
                keep.append((inst, c))
                continue
            cd = self.designs.get(c)
            if not cd:
                continue
            if cd.subs:
                self._flatten(cd)
            d.flops += [(inst + "/" + n, w) for n, w in cd.flops]
            d.mod = self._merge_mod(d.mod, cd.mod)
        d.subs = keep
        d.mapped = False
        d.st = None

    @staticmethod
    def _merge_mod(a, b):
        import copy
        m = copy.copy(a)
        m.ops = a.ops + b.ops
        m.async_reset = a.async_reset or b.async_reset
        return m

    # ======================================================================
    # Reports
    # ======================================================================
    def wire_header(self, lib):
        self.out("Operating Conditions: %s   Library: %s" % (lib["oc"], lib["name"]))
        self.out("Wire Load Model Mode: top")
        self.out("")

    def path_candidates(self, d, lib, mapped):
        """여러 경로 후보 (slack 순)"""
        per, unc = self.timing_targets(d)
        clk = list(d.clocks)[0] if d.clocks else None
        clk2q = self.ci("DFFARX1", lib)[1]
        setup = self.setup_t(lib)
        crit, prefix, cdes = self.crit(d)
        flops = self.all_flop_bits(d)
        rng = random.Random(stable_seed(d.name, "paths", crit))
        cands = []
        lat = d.latency.get(clk, 0.0) if clk else 0.0
        # reg -> reg
        if flops:
            ends = [b for b, x in flops if (prefix and b.startswith(prefix)) or not prefix] or [b for b, _ in flops]
            starts = [b for b, _ in flops if prefix and b.startswith(prefix)] or [b for b, _ in flops]
            for k in range(min(12, len(ends))):
                e = ends[-1 - k]
                s = starts[(k * 3) % len(starts)]
                dl = crit * (1 - 0.022 * k * rng.uniform(0.6, 1.4))
                cands.append(dict(kind="r2r", start=s, end=e, comb=dl, launch=clk2q, ext_in=0.0, ext_out=0.0))
        # in -> reg / in -> out
        inb = [b for b in d.port_bits("input") if re.sub(r"\[.*", "", b) not in
               set(p for c in d.clocks.values() for p in c["src"])]
        outb = d.port_bits("output")
        rst_like = [b for b in inb if re.search(r"rst|reset", b, re.I)]
        dat_in = [b for b in inb if b not in rst_like] or inb
        for k, b in enumerate(dat_in[:6]):
            idl = d.in_delay.get(re.sub(r"\[.*", "", b), (0.0, None))[0]
            dl = crit * (0.62 - 0.03 * k)
            if flops:
                cands.append(dict(kind="i2r", start=b, end=flops[(k * 5) % len(flops)][0], comb=dl, launch=0.0,
                                  ext_in=idl, ext_out=0.0))
            elif outb:
                odl = d.out_delay.get(re.sub(r"\[.*", "", outb[k % len(outb)]), (0.0, None))[0]
                cands.append(dict(kind="i2o", start=b, end=outb[k % len(outb)], comb=crit * (1 - 0.03 * k), launch=0.0,
                                  ext_in=idl, ext_out=odl))
        for k, b in enumerate(outb[:6]):
            odl = d.out_delay.get(re.sub(r"\[.*", "", b), (0.0, None))[0]
            if flops:
                cands.append(dict(kind="r2o", start=flops[(k * 7) % len(flops)][0], end=b, comb=0.12 * lib["scale"] + crit * 0.12,
                                  launch=clk2q, ext_in=0.0, ext_out=odl))
        for c in cands:
            arr = lat + c["launch"] + c["ext_in"] + c["comb"]
            c["arrival"] = arr
            port_start = c["kind"] in ("i2r", "i2o")
            port_end = c["kind"] in ("r2o", "i2o")
            constrained = bool(per) and (not port_start or re.sub(r"\[.*", "", c["start"]) in d.in_delay) and \
                (not port_end or re.sub(r"\[.*", "", c["end"]) in d.out_delay)
            fp = any(re.sub(r"\[.*", "", c["start"]) in f or c["start"] in f for f, t in d.false_paths if f)
            c["constrained"] = constrained and not fp
            if c["constrained"]:
                mult = 1
                for n, fr, to in d.mcp:
                    mult = max(mult, n)
                req = per * mult + lat - unc - (0 if port_end else setup) - c["ext_out"]
                c["required"] = req
                c["slack"] = req - arr
            else:
                c["required"] = None
                c["slack"] = 1e9
        cands.sort(key=lambda c: (not c["constrained"], c["slack"], -c["arrival"]))
        return cands, clk, per, unc, setup, lat

    def c_report_timing(self, a):
        d = self.need_design()
        o, pos = self.opts("report_timing", a,
                           flags=["-nets", "-capacitance", "-transition_time", "-input_pins", "-nosplit",
                                  "-sort_by", "-unique_pins", "-significant_digits"],
                           valued=["-max_paths", "-nworst", "-delay_type", "-path", "-from", "-to", "-through",
                                   "-group", "-significant_digits", "-sort_by"])
        lib = self.ensure_stats(d)
        mapped = d.mapped
        mp = int(self.fnum(o.get("-max_paths", 1)))
        dtype = self.to_str(o.get("-delay_type", "max"))
        ptype = self.to_str(o.get("-path", "full"))
        sig = int(self.fnum(o.get("-significant_digits", 2)))
        fmtv = "%%.%df" % sig
        self.header("timing", ["-path %s" % ptype, "-delay %s" % dtype, "-max_paths %d" % mp])
        if not mapped:
            self.out(" * Some/all delay information is back-annotated.  (GTECH 추정치)")
            self.hint("compile 전이라 GTECH 기준 추정 타이밍입니다. compile 후 다시 확인하세요.")
            self.out("")
        self.wire_header(lib)
        cands, clk, per, unc, setup, lat = self.path_candidates(d, lib, mapped)
        frm = self.names_of(o["-from"]) if "-from" in o else None
        to = self.names_of(o["-to"]) if "-to" in o else None

        def okp(c):
            def m(name, pats):
                def one(p):
                    p2 = p.replace("[", "_").replace("]", "_")
                    cands = [name, re.sub(r"\[.*", "", name), re.sub(r"(_reg).*", r"\1", name)]
                    if "/" not in p:
                        cands += [x.split("/")[-1] for x in cands]
                    return any(fnmatch.fnmatchcase(x, p) or fnmatch.fnmatchcase(x, p2) for x in cands)
                return any(one(p) for p in pats)
            return (frm is None or m(c["start"], frm)) and (to is None or m(c["end"], to))
        cands = [c for c in cands if okp(c)]
        if not cands:
            self.out("No paths.")
            self.out("")
            return 1
        if dtype == "min":
            for c in cands:
                c["comb"] = c["comb"] * 0.18
                c["arrival"] = lat + c["launch"] + c["ext_in"] + c["comb"]
                if c["constrained"]:
                    c["required"] = lat + unc + self.hold_t(lib) - (c["ext_out"] if c["kind"] in ("r2o", "i2o") else 0)
                    c["slack"] = c["arrival"] - c["required"]
            cands.sort(key=lambda c: (not c["constrained"], c["slack"]))
        for idx, c in enumerate(cands[:mp]):
            node = c["end"] if c["kind"] in ("r2r", "i2r") else c["start"] if c["kind"] == "r2o" else ""
            prefix = node.rsplit("/", 1)[0] + "/" if "/" in node else ""
            cdes = d
            for part in [x for x in prefix.split("/") if x]:
                nxt = dict(getattr(cdes, "subs_eff", cdes.subs)).get(part)
                if nxt and nxt in self.designs:
                    cdes = self.designs[nxt]
                else:
                    break
            self.print_path(d, c, clk, per, unc, setup, lat, lib, mapped, prefix,
                            cdes, dtype, fmtv, "-nets" in o or "-transition_time" in o, "-transition_time" in o, idx)
        return 1

    def print_path(self, d, c, clk, per, unc, setup, lat, lib, mapped, prefix, cdes, dtype, fmtv, nets, trans, idx):
        F = lambda v: fmtv % v
        rng = random.Random(stable_seed(d.name, c["start"], c["end"], idx))
        seqcell = "DFFARX1" if any(x.mod.async_reset for x in self.all_designs_under(d)) else "DFFX1"
        sq = self.ci(seqcell, lib)
        seqname = sq[5] if mapped else "**SEQGEN**"
        sp_d, sp_clk, sp_q = (sq[3][0], sq[3][1], sq[4]) if mapped else ("next_state", "clocked_on", "Q")

        def desc(n, kind):
            if kind == "reg":
                return "(rising edge-triggered flip-flop clocked by %s)" % clk if clk else "(rising edge-triggered flip-flop)"
            if c["constrained"]:
                return "(input port clocked by %s)" % clk if kind == "in" else "(output port clocked by %s)" % clk
            return "(input port)" if kind == "in" else "(output port)"
        sk = "in" if c["kind"] in ("i2r", "i2o") else "reg"
        ek = "out" if c["kind"] in ("r2o", "i2o") else "reg"
        self.out("  Startpoint: %s" % c["start"])
        self.out("              %s" % desc(c["start"], sk))
        self.out("  Endpoint: %s" % c["end"])
        self.out("            %s" % desc(c["end"], ek))
        self.out("  Path Group: %s" % (clk if c["constrained"] else "(none)"))
        self.out("  Path Type: %s" % dtype)
        self.out("")
        if c["constrained"] or True:
            pass
        W = 49
        if mapped and lib.get("cmap"):
            longest = max([len(v[5]) for v in lib["cmap"].values() if v] + [10])
            W = max(49, longest + len(prefix) + 22)
        if trans:
            self.out("  %-*s %8s %8s %10s" % (W - 9, "Point", "Trans", "Incr", "Path"))
            line = "  " + "-" * (W + 22)
        else:
            self.out("  %-*s %8s %10s" % (W, "Point", "Incr", "Path"))
            line = "  " + "-" * (W + 20)
        self.out(line)
        t = 0.0
        edge = ["r", "f"]

        def row(point, incr, path, e="", tr=None):
            if trans:
                self.out("  %-*s %8s %8s %10s %s" % (W - 9, point, "" if tr is None else F(tr),
                                                    "" if incr is None else F(incr), F(path), e))
            else:
                self.out("  %-*s %8s %10s %s" % (W, point, "" if incr is None else F(incr), F(path), e))
        if c["constrained"]:
            row("clock %s (rise edge)" % clk, 0.0, 0.0)
            row("clock network delay (ideal)", lat, lat)
            t = lat
        if sk == "in":
            if c["constrained"]:
                t += c["ext_in"]
                row("input external delay", c["ext_in"], t, "f")
            row("%s (in)" % c["start"], 0.0, t, "f", 0.0)
        else:
            row("%s/%s (%s)" % (c["start"], sp_clk, seqname), 0.0, t, "r", 0.0)
            t += c["launch"]
            row("%s/%s (%s)" % (c["start"], sp_q, seqname), c["launch"], t, "f", sq[6])
        # 조합 경로 셀
        st = self.stats(cdes) or self.stats(d)
        pool = [k for k, v in st["cells"].items() if CELLS[k][2] in ("comb", "inv", "buf") for _ in range(min(v, 40))]
        if not pool:
            pool = ["NAND2X1"]
        nst = max(2, min(28, int(round(c["comb"] / (0.075 * lib["scale"])))))
        picks = [rng.choice(pool) for _ in range(nst)]
        wts = [self.ci(p, lib)[1] * rng.uniform(0.7, 1.4) for p in picks]
        sw = sum(wts)
        ids = sorted(rng.sample(range(10, 9999), nst))
        for i, (p, wv) in enumerate(zip(picks, wts)):
            inc = c["comb"] * wv / sw
            t += inc
            cinf = self.ci(p, lib)
            name = "%sU%d" % (prefix if sk == "reg" or i > 0 else prefix, ids[i])
            cname = cinf[5] if mapped else GTECH.get(p, p)
            if nets:
                row("n%d (net)" % (ids[i] + 3), None, t - inc)
            row("%s/%s (%s)" % (name, cinf[4] if mapped else "Z", cname), inc, t, edge[(i + 1) % 2], cinf[6])
        if ek == "out":
            row("%s (out)" % c["end"], 0.0, t, edge[nst % 2], 0.05)
        else:
            row("%s/%s (%s)" % (c["end"], sp_d, seqname), 0.0, t, edge[nst % 2], 0.05)
        row("data arrival time", None, t)
        self.out("")
        if c["constrained"]:
            if dtype == "max":
                mult = max([n for n, _, _ in d.mcp] + [1])
                edge_t = per * mult
                row("clock %s (rise edge)" % clk, edge_t, edge_t)
                row("clock network delay (ideal)", lat, edge_t + lat)
                r = edge_t + lat
                if unc:
                    r -= unc
                    row("clock uncertainty", -unc, r)
                if ek == "out":
                    r -= c["ext_out"]
                    row("output external delay", -c["ext_out"], r)
                else:
                    row("%s/%s (%s)" % (c["end"], sp_clk, seqname), 0.0, r, "r")
                    r -= setup
                    row("library setup time", -setup, r)
            else:
                r = lat
                row("clock %s (rise edge)" % clk, 0.0, 0.0)
                row("clock network delay (ideal)", lat, lat)
                hu = d.unc.get(clk + ":hold", unc)
                r += hu
                row("clock uncertainty", hu, r)
                if ek == "out":
                    r -= c["ext_out"]
                    row("output external delay", -c["ext_out"], r)
                else:
                    row("%s/%s (%s)" % (c["end"], sp_clk, seqname), 0.0, r, "r")
                    r += self.hold_t(lib)
                    row("library hold time", self.hold_t(lib), r)
            row("data required time", None, r)
            self.out(line)
            row("data required time", None, r)
            row("data arrival time", None, -t)
            self.out(line)
            slack = (r - t) if dtype == "max" else (t - r)
            tag = "(MET)" if slack >= 0 else "(VIOLATED)"
            txt = "  %-*s %s" % (W + (9 if not trans else 18), "slack " + tag, ("%10s" % F(slack)))
            self.out(txt, "32" if slack >= 0 else "31")
        else:
            self.out("  (Path is unconstrained)", "33")
            if not d.clocks:
                self.hint("클럭이 없어서 unconstrained 입니다. create_clock 후 다시 해보세요.")
            elif sk == "in" and re.sub(r"\[.*", "", c["start"]) not in d.in_delay:
                self.hint("입력 포트에 set_input_delay 가 없어 unconstrained 입니다.")
            elif ek == "out" and re.sub(r"\[.*", "", c["end"]) not in d.out_delay:
                self.hint("출력 포트에 set_output_delay 가 없어 unconstrained 입니다.")
        self.out("")
        self.out("")

    def c_report_area(self, a):
        d = self.need_design()
        o, pos = self.opts("report_area", a, flags=["-hierarchy", "-nosplit", "-physical", "-designware"])
        lib = self.ensure_stats(d)
        tot = self.totals(d)
        self.header("area")
        self.out("Information: Updating design information... (UID-85)")
        if not d.mapped:
            self.warn("Design '%s' is not mapped; GTECH cells have no area. Run compile." % d.name, "RPT-1")
        self.out("Library(s) Used:")
        self.out("")
        self.out("    %s (File: %s)" % (lib["name"], self.lib_file(lib)))
        self.out("")
        nports = sum(1 for _ in d.ports)
        ncells = tot["ncomb"] + tot["nseq"] + tot["nhier"]
        nnets = int(ncells * 1.12 + nports + 3)
        def kv(label, val):
            v = ("%d" % val) if isinstance(val, int) else ("%.6f" % val if isinstance(val, float) else str(val))
            self.out(label + " " * max(1, 45 - len(label) - len(v)) + v)
        kv("Number of ports:", sum(w for _, _, w in d.ports))
        kv("Number of nets:", nnets)
        kv("Number of cells:", ncells)
        kv("Number of combinational cells:", tot["ncomb"])
        kv("Number of sequential cells:", tot["nseq"])
        kv("Number of macros/black boxes:", 0)
        kv("Number of buf/inv:", tot["nbuf"] + tot["ninv"])
        kv("Number of references:", len(tot["cells"]) + len(set(c for _, c in getattr(d, 'subs_eff', d.subs))))
        self.out("")
        total = tot["comb_area"] + tot["seq_area"]
        has_wl = bool(d.wlm) or not lib.get("tech") or bool(lib["tech"].wire_loads)
        net_area = (total * (0.162 + 0.012 * random.Random(stable_seed(d.name, "wl")).random())) if d.mapped else 0.0
        kv("Combinational area:", float(tot["comb_area"]))
        kv("Buf/Inv area:", float(tot["bi_area"]))
        kv("Noncombinational area:", float(tot["seq_area"]))
        kv("Macro/Black Box area:", 0.0)
        if has_wl:
            kv("Net Interconnect area:", float(net_area))
        else:
            self.out("Net Interconnect area:      undefined  (No wire load specified)")
        self.out("")
        kv("Total cell area:", float(total))
        if has_wl:
            kv("Total area:", float(total + net_area))
        else:
            self.out("Total area:                 undefined")
        if d.max_area is not None and d.mapped:
            self.out("")
            if total > d.max_area:
                self.out("  max_area constraint %.2f : VIOLATED by %.2f" % (d.max_area, total - d.max_area), "31")
            else:
                self.out("  max_area constraint %.2f : MET" % d.max_area, "32")
        if "-hierarchy" in o:
            self.out("")
            self.out("Hierarchical area distribution")
            self.out("------------------------------")
            self.out("")
            self.out("                                  Global      Local")
            self.out("                                  Cell Area   Cell Area")
            self.out("                                  ----------  ----------------------------")
            self.out("Hierarchical cell                 Absolute    Combi-     Noncombi-  Black-")
            self.out("                                  Total  %     national   national   boxes   Design")
            self.out("--------------------------------  ----------  ---------  ---------  ------  ---------")

            def walk(x, path):
                t = self.totals(x)
                s = self.stats(x)
                tt = t["comb_area"] + t["seq_area"]
                self.out("%-32s  %8.1f %5.1f %9.1f %10.1f  %6.1f  %s" % (
                    path, tt, 100.0 * tt / total if total else 0, s["comb_area"], s["seq_area"], 0, x.name))
                for inst, c in getattr(x, "subs_eff", x.subs):
                    if c in self.designs:
                        walk(self.designs[c], path + "/" + inst if path != d.name else inst)
            walk(d, d.name)
            self.out("--------------------------------  ----------  ---------  ---------  ------  ---------")
            self.out("Total                             %8.1f %5.1f %9.1f %10.1f  %6.1f" % (
                total, 100.0, tot["comb_area"], tot["seq_area"], 0))
        self.out("")
        self.out("1".replace("1", "") or "")
        return 1

    def freq(self, d):
        per, _ = self.timing_targets(d)
        return (1.0 / per) if per else 0.1

    def c_report_power(self, a):
        d = self.need_design()
        o, pos = self.opts("report_power", a, flags=["-hierarchy", "-nosplit", "-verbose", "-cell", "-net"],
                           valued=["-analysis_effort"])
        lib = self.ensure_stats(d)
        self.header("power", ["-analysis_effort low"])
        if not d.clocks:
            self.warn("Design has no clock; using default toggle rate with 100 MHz.", "PWR-246")
        self.out("Library(s) Used:")
        self.out("")
        self.out("    %s (File: %s)" % (lib["name"], self.lib_file(lib)))
        self.out("")
        self.wire_header(lib)
        self.out("Global Operating Voltage = %.2f" % lib["volt"])
        self.out("Power-specific unit information :")
        self.out("    Voltage Units = 1V")
        self.out("    Capacitance Units = 1.000000pf")
        self.out("    Time Units = 1ns")
        self.out("    Dynamic Power Units = 1uW    (derived from V,C,T units)")
        self.out("    Leakage Power Units = 1pW")
        self.out("")

        def pw(x, own_only=False):
            t = self.stats(x) if own_only else self.totals(x)
            f = self.freq(d)
            v2 = (lib["volt"] / 1.05) ** 2
            area_c, area_s = (t["comb_area"], t["seq_area"])
            if not x.mapped and not d.mapped:
                cc = sum(self.ci(k, lib)[0] * v for k, v in t["cells"].items() if CELLS[k][2] != "seq")
                cs = sum(self.ci(k, lib)[0] * v for k, v in t["cells"].items() if CELLS[k][2] == "seq")
                area_c, area_s = cc, cs
            internal = (area_c * 0.055 + area_s * 0.16 * self.gate_factor(x)) * f * v2 * 10
            switching = (area_c * 0.065 + area_s * 0.035) * f * v2 * 10
            tl = lib.get("tech")
            if tl:      # 실제 라이브러리: P = 1/2 * a * C * V^2 * f  (C 는 liberty 핀 용량 기반)
                cpin = tl.avg_input_cap() * 1e-12
                hz = f * 1e9
                vv = lib["volt"] ** 2
                ncomb = sum(v for k, v in t["cells"].items() if CELLS[k][2] != "seq")
                nseq = sum(v for k, v in t["cells"].items() if CELLS[k][2] == "seq")
                cnet = cpin * 2.5 + 0.0015e-12 * 4
                sw_c = 0.5 * 0.15 * ncomb * cnet * vv * hz
                sw_s = 0.5 * 0.10 * nseq * cnet * vv * hz
                clk = nseq * cpin * vv * hz * self.gate_factor(x)
                switching = (sw_c + sw_s + clk) * 1e6
                internal = (0.75 * sw_c + nseq * 3.0 * cpin * vv * hz * self.gate_factor(x)) * 1e6
            leak = self.leak_pw(t["cells"], lib, area_c + area_s)   # pW
            return internal, switching, leak
        i, s, l = pw(d)
        dyn = i + s
        self.out("")
        self.out("  Cell Internal Power  = %10.4f uW   (%3.0f%%)" % (i, 100 * i / dyn if dyn else 0))
        self.out("  Net Switching Power  = %10.4f uW   (%3.0f%%)" % (s, 100 * s / dyn if dyn else 0))
        self.out("                         ---------")
        self.out("Total Dynamic Power    = %10.4f uW  (100%%)" % dyn)
        self.out("")
        self.out("Cell Leakage Power     = %10.4f uW" % (l / 1e6))
        self.out("")
        self.out("")
        self.out("                 Internal         Switching           Leakage            Total")
        self.out("Power Group      Power            Power               Power              Power   (   %    )  Attrs")
        self.out("-" * 96)
        tot_all = dyn + l / 1e6
        groups = [("io_pad", 0, 0, 0), ("memory", 0, 0, 0), ("black_box", 0, 0, 0), ("clock_network", 0, 0, 0),
                  ("register", i * 0.55, s * 0.18, l * 0.3), ("sequential", 0, 0, 0),
                  ("combinational", i * 0.45, s * 0.82, l * 0.7)]
        for g, gi, gs, gl in groups:
            gt = gi + gs + gl / 1e6
            self.out("%-14s %10.4f %17.4f %17.4e %15.4f  (%6.2f%%)" % (g, gi, gs, gl, gt, 100 * gt / tot_all if tot_all else 0))
        self.out("-" * 96)
        self.out("Total          %10.4f uW %14.4f uW %14.4e pW %12.4f uW" % (i, s, l, tot_all))
        if "-hierarchy" in o:
            self.out("")
            self.out("Hierarchy                              Switch   Int      Leak      Total   %")
            self.out("-" * 80)

            def walk(x, name, dep):
                xi, xs, xl = pw(x)
                xt = xi + xs + xl / 1e6
                self.out("%-38s %7.3f %7.3f %9.3e %8.3f %5.1f" % ("  " * dep + name, xs, xi, xl, xt, 100 * xt / tot_all if tot_all else 0))
                for inst, c in getattr(x, "subs_eff", x.subs):
                    if c in self.designs:
                        walk(self.designs[c], "%s (%s)" % (inst, c), dep + 1)
            walk(d, d.name, 0)
        self.out("")
        return 1

    def c_report_qor(self, a):
        d = self.need_design()
        lib = self.ensure_stats(d)
        self.header("qor")
        cands, clk, per, unc, setup, lat = self.path_candidates(d, lib, d.mapped)
        crit, _, _ = self.crit(d)
        st = self.stats(d)
        tot = self.totals(d)
        groups = OrderedDict()
        if clk:
            groups[clk] = [c for c in cands if c["constrained"]]
        else:
            groups["**default**"] = []
        for g, cs in groups.items():
            wns = min([c["slack"] for c in cs] + [0.0]) if cs else 0.0
            worst = cs[0] if cs else None
            viol = [c for c in cs if c["slack"] < 0]
            nb = len(self.all_flop_bits(d))
            nviol = int(len(viol) * max(1, nb / max(len(cs), 1))) if viol else 0
            tns = sum(c["slack"] for c in viol) * max(1.0, nb / max(len(cs), 1)) if viol else 0.0
            self.out("")
            self.out("  Timing Path Group '%s'" % g)
            self.out("  -----------------------------------")
            self.out("  Levels of Logic:          %10.2f" % (st["levels"] + 2))
            self.out("  Critical Path Length:     %10.2f" % ((worst["arrival"] if worst else crit)))
            self.out("  Critical Path Slack:      %10s" % ("%.2f" % worst["slack"] if worst else "uninit"))
            self.out("  Critical Path Clk Period: %10s" % ("%.2f" % per if per else "n/a"))
            self.out("  Total Negative Slack:     %10.2f" % tns)
            self.out("  No. of Violating Paths:   %10.2f" % nviol)
            self.out("  Worst Hold Violation:     %10.2f" % 0.0)
            self.out("  Total Hold Violation:     %10.2f" % 0.0)
            self.out("  No. of Hold Violations:   %10.2f" % 0.0)
            self.out("  -----------------------------------")
        self.out("")
        self.out("")
        self.out("  Cell Count")
        self.out("  -----------------------------------")
        self.out("  Hierarchical Cell Count:  %10d" % tot["nhier"])
        self.out("  Hierarchical Port Count:  %10d" % (tot["nhier"] * 12))
        self.out("  Leaf Cell Count:          %10d" % (tot["ncomb"] + tot["nseq"]))
        self.out("  Buf/Inv Cell Count:       %10d" % (tot["nbuf"] + tot["ninv"]))
        self.out("  Buf Cell Count:           %10d" % tot["nbuf"])
        self.out("  Inv Cell Count:           %10d" % tot["ninv"])
        self.out("  CT Buf/Inv Cell Count:    %10d" % 0)
        self.out("  Combinational Cell Count: %10d" % tot["ncomb"])
        self.out("  Sequential Cell Count:    %10d" % tot["nseq"])
        self.out("  Macro Count:              %10d" % 0)
        self.out("  -----------------------------------")
        self.out("")
        self.out("")
        self.out("  Area")
        self.out("  -----------------------------------")
        self.out("  Combinational Area:     %12.6f" % tot["comb_area"])
        self.out("  Noncombinational Area:  %12.6f" % tot["seq_area"])
        self.out("  Buf/Inv Area:           %12.6f" % tot["bi_area"])
        bufa = tot["cells"].get("BUFX2", 0) * self.ci("BUFX2", lib)[0] * (1 if d.mapped else 0)
        self.out("  Total Buffer Area:      %12.2f" % bufa)
        self.out("  Total Inverter Area:    %12.2f" % (tot["bi_area"] - bufa))
        self.out("  Macro/Black Box Area:   %12.6f" % 0)
        self.out("  Net Area:               %12.6f" % 0)
        self.out("  -----------------------------------")
        self.out("  Cell Area:              %12.6f" % (tot["comb_area"] + tot["seq_area"]))
        self.out("  Design Area:            %12.6f" % (tot["comb_area"] + tot["seq_area"]))
        self.out("")
        self.out("")
        self.out("  Design Rules")
        self.out("  -----------------------------------")
        self.out("  Total Number of Nets:     %10d" % int((tot["ncomb"] + tot["nseq"]) * 1.12 + len(d.ports)))
        self.out("  Nets With Violations:     %10d" % 0)
        self.out("  Max Trans Violations:     %10d" % 0)
        self.out("  Max Cap Violations:       %10d" % 0)
        self.out("  -----------------------------------")
        self.out("")
        self.out("")
        self.out("  Hostname: dc-sim-host")
        self.out("")
        self.out("  Compile CPU Statistics")
        self.out("  -----------------------------------------")
        self.out("  Resource Sharing:         %10.2f" % 0.12)
        self.out("  Logic Optimization:       %10.2f" % (0.3 + tot["ncomb"] / 3000))
        self.out("  Mapping Optimization:     %10.2f" % (0.5 + tot["ncomb"] / 1500))
        self.out("  -----------------------------------------")
        self.out("  Overall Compile Time:     %10.2f" % (1.0 + tot["ncomb"] / 1000))
        self.out("  Overall Compile Wall Clock Time: %10.2f" % (1.2 + tot["ncomb"] / 900))
        self.out("")
        return 1

    def c_report_constraint(self, a):
        d = self.need_design()
        o, pos = self.opts("report_constraint", a, flags=["-all_violators", "-verbose", "-max_delay", "-min_delay",
                                                          "-max_area", "-nosplit", "-significant_digits"])
        lib = self.ensure_stats(d)
        cands, clk, per, unc, setup, lat = self.path_candidates(d, lib, d.mapped)
        self.header("constraint", ["-all_violators"] if "-all_violators" in o else [])
        viol = [c for c in cands if c["constrained"] and c["slack"] < 0]
        tot = self.totals(d)
        area = tot["comb_area"] + tot["seq_area"]
        if "-all_violators" in o:
            if not viol and not (d.max_area is not None and area > d.max_area):
                self.out("This design has no violated constraints.", "32")
                self.out("")
                return 1
            if viol:
                self.out("   max_delay/setup ('%s' group)" % clk)
                self.out("")
                self.out("   Endpoint                         Required      Actual")
                self.out("                                    Path Delay    Path Delay        Slack")
                self.out("   " + "-" * 72)
                for c in viol:
                    self.out("   %-32s %10.2f    %10.2f r  %10.2f  (VIOLATED)" % (c["end"], c["required"], c["arrival"], c["slack"]), "31")
                self.out("")
            if d.max_area is not None and area > d.max_area:
                self.out("   max_area")
                self.out("")
                self.out("                             Required        Actual")
                self.out("   Design                    Area            Area           Slack")
                self.out("   " + "-" * 64)
                self.out("   %-24s %12.2f   %12.2f   %12.2f  (VIOLATED)" % (d.name, d.max_area, area, d.max_area - area), "31")
            self.out("")
            return 1
        wns = min([c["slack"] for c in cands if c["constrained"]] + [0.0])
        self.out("")
        self.out("                                                   Weighted")
        self.out("    Group (max_delay/setup)      Cost     Weight     Cost")
        self.out("    -----------------------------------------------------")
        self.out("    %-24s %8.2f %8.2f %9.2f" % (clk or "default", max(-wns, 0), 1.0, max(-wns, 0)))
        self.out("    -----------------------------------------------------")
        self.out("    max_delay/setup              %20.2f" % max(-wns, 0))
        self.out("")
        self.out("    Constraint                                       Cost")
        self.out("    -----------------------------------------------------")
        self.out("    max_transition                               0.00 (MET)")
        self.out("    max_fanout                                   0.00 (MET)")
        self.out("    max_capacitance                              0.00 (MET)")
        self.out("    max_delay/setup                         %9.2f (%s)" % (max(-wns, 0), "VIOLATED" if wns < 0 else "MET"))
        if d.max_area is not None:
            self.out("    max_area                                %9.2f (%s)" % (max(area - d.max_area, 0),
                                                                                "VIOLATED" if area > d.max_area else "MET"))
        self.out("")
        return 1

    def c_report_clock(self, a):
        d = self.need_design()
        self.header("clock")
        self.out("Attributes:")
        self.out("    d - dont_touch_network")
        self.out("    f - fix_hold")
        self.out("    p - propagated_clock")
        self.out("    G - generated_clock")
        self.out("    g - lib_generated_clock")
        self.out("")
        self.out("Clock          Period   Waveform            Attrs     Sources")
        self.out("-" * 80)
        if not d.clocks:
            self.out("No clocks defined.")
        for n, c in d.clocks.items():
            self.out("%-14s %6.2f   {%s}%s %-9s {%s}" % (n, c["period"], " ".join("%g" % w for w in c["wave"]),
                                                        " " * max(1, 18 - len(" ".join("%g" % w for w in c["wave"]))),
                                                        "", " ".join(c["src"])))
        self.out("-" * 80)
        if d.unc or d.latency or d.transition:
            self.out("")
            self.out("Clock          Uncertainty   Latency   Transition")
            self.out("-" * 52)
            for n in d.clocks:
                self.out("%-14s %11.2f %9.2f %12.2f" % (n, d.unc.get(n, 0), d.latency.get(n, 0), d.transition.get(n, 0)))
        self.out("")
        return 1

    def c_report_port(self, a):
        d = self.need_design()
        self.header("port", ["-verbose"] if a else [])
        self.out("                       Pin      Wire     Max     Max     Connection")
        self.out("Port           Dir     Load     Load     Trans   Cap     Class      Attrs")
        self.out("-" * 76)
        for dr, n, w in d.ports:
            self.out("%-14s %-6s %6.2f %8.2f %7s %7s %-10s" % (n + ("[%d:0]" % (w - 1) if w > 1 else ""),
                                                              {"input": "in", "output": "out"}.get(dr, dr),
                                                              d.loads.get(n, 0.0), 0.0, "--", "--", "--"))
        self.out("")
        self.out("")
        self.out("               Input Delay           Output Delay")
        self.out("Port           Clock      Value      Clock      Value     Driving cell")
        self.out("-" * 76)
        for dr, n, w in d.ports:
            iv = d.in_delay.get(n)
            ov = d.out_delay.get(n)
            self.out("%-14s %-10s %-10s %-10s %-9s %s" % (
                n, iv[1] if iv else "--", "%.2f" % iv[0] if iv else "--",
                ov[1] if ov else "--", "%.2f" % ov[0] if ov else "--", d.driving.get(n, "--")))
        self.out("")
        return 1

    def c_report_design(self, a):
        d = self.need_design()
        lib = self.ensure_stats(d)
        self.header("design")
        self.out("Design allows ideal nets on clock nets.")
        self.out("")
        self.out("Library(s) Used:")
        self.out("")
        self.out("    %s (File: %s)" % (lib["name"], self.lib_file(lib)))
        self.out("")
        self.out("Local Link Library:")
        self.out("")
        self.out("    {%s}" % self.vars["link_library"])
        self.out("")
        self.out("Flip-Flop Types:")
        self.out("")
        self.out("    No flip-flop types specified.")
        self.out("")
        self.out("Latch Types:")
        self.out("")
        self.out("    No latch types specified.")
        self.out("")
        self.out("Operating Conditions:")
        self.out("")
        self.out("    Operating Condition Name : %s" % (d.oc or lib["oc"]))
        self.out("    Library : %s" % lib["name"])
        self.out("    Process :   1.00")
        tm = lib.get("temp")
        if tm is None:
            mm = re.search(r"v(n?\d+)c", lib["oc"])
            tm = mm.group(1).replace("n", "-") if mm else "25"
        self.out("    Temperature : %s" % tm)
        self.out("    Voltage :   %.2f" % lib["volt"])
        self.out("")
        self.out("Wire Loading Model:")
        self.out("")
        self.out("    %s" % (("Selected manually by the user: %s" % d.wlm) if d.wlm else "No wire loading specified."))
        self.out("")
        self.out("Design Parameters:")
        self.out("")
        if d.env:
            for k, v in d.env.items():
                self.out("    %s : %s" % (k, v))
        else:
            self.out("    None specified.")
        self.out("")
        return 1

    def c_report_hierarchy(self, a):
        d = self.need_design()
        lib = self.ensure_stats(d)
        self.header("hierarchy")

        def walk(x, dep):
            self.out("%s%s" % ("    " * dep, x.name))
            st = self.stats(x)
            for inst, c in getattr(x, "subs_eff", x.subs):
                if c in self.designs:
                    walk(self.designs[c], dep + 1)
            for inst, typ in x.unresolved:
                self.out("%s%s  (unresolved)" % ("    " * (dep + 1), typ))
            for k in sorted(st["cells"]):
                self.out("%s%-14s %s" % ("    " * (dep + 1), (k if x.mapped else GTECH.get(k, k)),
                                         (lib["name"] if x.mapped else "gtech")))
        walk(d, 0)
        self.out("")
        return 1

    def c_report_reference(self, a):
        d = self.need_design()
        lib = self.ensure_stats(d)
        st = self.stats(d)
        self.header("reference")
        self.out("Attributes:")
        self.out("    b - black box (unknown)")
        self.out("    h - hierarchical")
        self.out("    n - noncombinational")
        self.out("    r - removable")
        self.out("    u - contains unmapped logic")
        self.out("")
        self.out("Reference          Library       Unit Area   Count    Total Area   Attributes")
        self.out("-" * 79)
        total = 0.0
        cnt = 0
        rows = OrderedDict()
        for k, v in sorted(st["cells"].items()):
            ci = self.ci(k, lib)
            ua = ci[0] if d.mapped else 0.0
            nm = ci[5] if d.mapped else GTECH.get(k, k)
            r = rows.setdefault(nm, [ua, 0, "n" if CELLS[k][2] == "seq" else ""])
            r[1] += v
        w = max([18] + [len(n) + 1 for n in rows])
        ln = lib["name"] if d.mapped else "gtech"
        lw = max(12, len(ln))
        for nm, (ua, v, at) in sorted(rows.items()):
            total += ua * v
            cnt += v
            self.out("%-*s %-*s %10.6f %7d %13.6f    %s" % (w, nm, lw, ln, ua, v, ua * v, at))
        for inst, c in getattr(d, "subs_eff", d.subs):
            if c in self.designs:
                t = self.totals(self.designs[c])
                ar = t["comb_area"] + t["seq_area"]
                total += ar
                cnt += 1
                self.out("%-18s %-12s %10.6f %7d %13.6f    h%s" % (c, "", ar, 1, ar, "" if self.designs[c].mapped else ", u"))
        self.out("-" * 79)
        self.out("Total %d references                              %13.6f" % (len(st["cells"]) + len(d.subs), total))
        self.out("")
        return 1

    def c_report_cell(self, a):
        d = self.need_design()
        lib = self.ensure_stats(d)
        st = self.stats(d)
        self.header("cell")
        self.out("Cell                      Reference       Library             Area  Attributes")
        self.out("-" * 79)
        rng = random.Random(stable_seed(d.name, "cells"))
        i = 0
        shown = 0
        bits = iter(d.flop_bits())
        for k, v in st["cells"].items():
            for _ in range(v):
                i += 1
                nm = next(bits, None) if CELLS[k][2] == "seq" else None
                nm = nm or "U%d" % (i + 100)
                if shown < 60:
                    ci = self.ci(k, lib)
                    self.out("%-25s %-28s %-14s %9.6f  %s" % (nm, ci[5] if d.mapped else GTECH.get(k, k),
                                                             lib["name"][:14] if d.mapped else "gtech",
                                                             ci[0] if d.mapped else 0, "n" if CELLS[k][2] == "seq" else ""))
                    shown += 1
        for inst, c in getattr(d, "subs_eff", d.subs):
            self.out("%-25s %-15s %-15s %9s  h" % (inst, c, "", ""))
        if i > shown:
            self.out("... (%d more cells; simulator shows first %d)" % (i - shown, shown))
        self.out("-" * 79)
        self.out("Total %d cells" % (i + len(d.subs)))
        return 1

    # ======================================================================
    # Output
    # ======================================================================
    def c_write(self, a):
        d = self.need_design()
        o, pos = self.opts("write", a, flags=["-hierarchy", "-no_implicit"], valued=["-format", "-output", "-library"])
        fmt = self.to_str(o.get("-format", "ddc")).lower()
        if fmt not in ("verilog", "ddc", "svsim"):
            raise DCError("Invalid format '%s'." % fmt, "UIO-4")
        out = self.to_str(o.get("-output", "%s.%s" % (d.name, "v" if fmt == "verilog" else "ddc")))
        lib = self.ensure_stats(d)
        designs = self.all_designs_under(d) if "-hierarchy" in o else [d]
        f = self.safe_open(out)
        try:
            if fmt == "ddc":
                f.write("# %s simulated DDC database (텍스트 placeholder - 실제 ddc 아님)\n" % MARKER)
                f.write("design %s\nmapped %s\nlibrary %s\n" % (d.name, d.mapped, lib["name"]))
                for x in designs:
                    st = self.stats(x)
                    f.write("subdesign %s cells %s\n" % (x.name, dict(st["cells"])))
                self.out("Writing ddc file '%s'." % out)
            else:
                self.out("Writing verilog file '%s'." % self.abspath(out))
                if not d.mapped:
                    self.warn("Design '%s' is not mapped; writing GTECH netlist." % d.name, "VO-11")
                f.write("// %s : simulated gate-level netlist (교육용 가상 netlist)\n" % MARKER)
                f.write("// Created by Design Compiler %s on %s\n" % (REPORT_VERSION, dc_date()))
                f.write(self._netlist_meta(d) + "\n")
                for x in reversed(designs):
                    self.write_module(f, x, lib)
                if any(x.mapped for x in designs):
                    pass
        finally:
            f.close()
        return 1

    def write_module(self, f, x, lib):
        st = self.stats(x)
        ports = ", ".join(n for _, n, _ in x.ports)
        f.write("module %s ( %s );\n" % (x.name, ports))
        for dr, n, w in x.ports:
            f.write("  %s %s%s;\n" % (dr, "[%d:0] " % (w - 1) if w > 1 else "", n))
        ncell = sum(st["cells"].values())
        lim = min(ncell, 4000)
        nwire = int(lim * 1.1) + 2
        f.write("  wire " + ", ".join("n%d" % i for i in range(1, min(nwire, 60) + 1)) +
                (" /* ... n%d */" % nwire if nwire > 60 else "") + ";\n\n")
        rng = random.Random(stable_seed(x.name, "nl"))
        srcs = [("%s[%d]" % (n, i) if w > 1 else n) for dr, n, w in x.ports if dr == "input" for i in range(w)]
        bits = iter(x.flop_bits())
        k = 0
        for inst, c in getattr(x, "subs_eff", x.subs):
            cd = self.designs.get(c)
            if cd:
                conns = ", ".join(".%s(n%d)" % (pn, rng.randint(1, nwire)) for _, pn, _ in cd.ports)
                f.write("  %s %s ( %s );\n" % (c, inst, conns))
        for cell, n in st["cells"].items():
            for _ in range(n):
                k += 1
                if k > lim:
                    break
                ci = self.ci(cell, lib) if x.mapped else CELLS[cell][:5] + (cell, 0)
                ins, outp = ci[3], ci[4]
                seq = CELLS[cell][2] == "seq"
                nm = (next(bits, None) if seq else None) or "U%d" % (k + 10)
                pins = []
                for pi, p in enumerate(ins):
                    sig = rng.choice(srcs) if (srcs and rng.random() < 0.15) else "n%d" % rng.randint(1, nwire)
                    if seq and pi == 1:
                        sig = next((s for s in srcs if re.search(r"clk", s, re.I)), "clk")
                    if seq and pi == 2:
                        sig = next((s for s in srcs if re.search(r"rst|reset", s, re.I)), "n1")
                    pins.append(".%s(%s)" % (p, sig))
                pins.append(".%s(n%d)" % (outp, rng.randint(1, nwire)))
                f.write("  %s %s ( %s );\n" % (ci[5] if x.mapped else GTECH.get(cell, cell).strip("*"), nm, ", ".join(pins)))
        if ncell > lim:
            f.write("  // ... %d more cells omitted by simulator\n" % (ncell - lim))
        f.write("endmodule\n\n")

    def c_write_sdc(self, a):
        d = self.need_design()
        o, pos = self.opts("write_sdc", a, flags=["-nosplit"], valued=["-version"])
        if not pos:
            raise DCError("Required argument 'file_name' was not found", "CMD-007")
        fn = self.to_str(pos[0])
        f = self.safe_open(fn)
        try:
            f.write("###################################################################\n\n")
            f.write("# Created by write_sdc on %s  (%s)\n\n" % (dc_date(), MARKER))
            f.write("###################################################################\n")
            f.write("set sdc_version 2.1\n\n")
            f.write("set_units -time ns -resistance kOhm -capacitance pF -voltage V -current mA\n")
            for line in d.sdc:
                f.write(line + "\n")
        finally:
            f.close()
        return 1

    def c_write_sdf(self, a):
        d = self.need_design()
        if not pos_ok(a):
            raise DCError("Required argument 'file_name' was not found", "CMD-007")
        fn = self.to_str([x for x in a if not str(x).startswith("-")][-1])
        lib = self.ensure_stats(d)
        f = self.safe_open(fn)
        try:
            f.write("(DELAYFILE\n  (SDFVERSION \"OVI 2.1\")\n  (DESIGN \"%s\")\n  (DATE \"%s\")\n"
                    "  (VENDOR \"%s\")\n  (PROGRAM \"%s\")\n  (VERSION \"%s\")\n  (DIVIDER /)\n"
                    "  (TIMESCALE 1ns)\n" % (d.name, dc_date(), lib["name"], MARKER, VERSION))
            for c in CELLS:
                ci = self.ci(c, lib)
                ins, outp, v = ci[3], ci[4], ci[1]
                c = ci[5]
                f.write("  (CELL (CELLTYPE \"%s\") (INSTANCE *)\n    (DELAY (ABSOLUTE (IOPATH %s %s (%.3f:%.3f:%.3f))))\n  )\n"
                        % (c, ins[0], outp, v * 0.8, v, v * 1.2))
            f.write(")\n")
        finally:
            f.close()
        self.info("Writing timing information to file '%s'." % self.abspath(fn), "WT-3")
        return 1

    # ======================================================================
    # 실행 루프
    # ======================================================================
    def display(self, r):
        if isinstance(r, Coll):
            return r.display()
        return self.to_str(r)

    def print_error(self, e):
        self.out("Error: %s (%s)" % (e.msg, e.code), "31")
        if e.hint:
            self.hint(e.hint)

    def exec_line(self, line, from_script=False, quiet=False):
        self.cmd_count += 1
        try:
            r = self.eval(line)
            if r not in (None, "") and not quiet:
                self.out(self.display(r))
            return r
        except DCError as e:
            self.print_error(e)
            if e.ret0:
                self.out("0")
        except (TclBreak, TclContinue):
            pass
        except RecursionError:
            self.out("Error: too many nested evaluations (CMD-013)", "31")
        except SystemExit:
            raise
        except Exception as e:
            self.out("Error: internal simulator error: %s (SIM-99)" % e, "31")
        return ""

    def banner(self):
        lines = [
            "",
            "                       Design Compiler Graphical  (Simulated)",
            "                             DC Ultra (TM)          (Simulated)",
            "                              DFTMAX (TM)           (Simulated)",
            "                          Power Compiler (TM)       (Simulated)",
            "",
            "               Version %s  for  education  --  dc_shell_sim" % VERSION,
            "",
            "   ※ 이 프로그램은 Synopsys 사의 제품이 아니며 실제 합성을 수행하지 않습니다.",
            "     모든 메시지와 리포트 수치는 화면 흐름 학습을 위해 가상으로 생성된 값입니다.",
            "",
            "   처음이라면  tutorial  을 입력하세요.  명령 목록은  help ,  종료는  exit",
            "",
        ]
        for l in lines:
            self.out(l)
        setup = os.path.join(os.getcwd(), ".synopsys_dc.setup")
        if os.path.exists(setup):
            self.out("Initializing...")
            self.depth += 1
            try:
                self.run_script(open(setup, encoding="utf-8", errors="replace").read(), quiet=True)
            finally:
                self.depth -= 1
        else:
            self.out("Initializing...")

    def goodbye(self):
        el = time.time() - self.t0
        self.out("")
        self.out("Memory usage for this session %d Mbytes." % (96 + len(self.designs) * 3))
        self.out("Memory usage for this session including child processes %d Mbytes." % (96 + len(self.designs) * 3))
        self.out("CPU usage for this session %d seconds ( %.2f hours )." % (int(self.cpu + 2), (self.cpu + 2) / 3600))
        self.out("Elapsed time for this session %d seconds ( %.2f hours )." % (int(el), el / 3600))
        self.out("")
        self.out("Thank you...")

    def repl(self):
        try:
            import readline  # noqa: F401  (리눅스/맥: 방향키 히스토리)

            def comp(text, state):
                opts = [c for c in sorted(set(list(self.cmds) + list(self.procs))) if c.startswith(text)]
                return opts[state] if state < len(opts) else None
            readline.set_completer(comp)
            readline.parse_and_bind("tab: complete")
        except Exception:
            pass
        while True:
            try:
                pr = getattr(self, "prompt", "dc_shell> ")
                buf = self.read_input(pr)
                while not self.is_complete(buf):
                    buf += "\n" + self.read_input("? ")
            except EOFError:
                self.out("")
                break
            except KeyboardInterrupt:
                self.out("")
                self.out("Information: Process terminated by interrupt. (INT-4)")
                continue
            if not buf.strip():
                continue
            self.history.append(buf.strip())
            try:
                self.exec_line(buf)
            except SystemExit:
                break
        self.goodbye()


def pos_ok(a):
    return any(not str(x).startswith("-") for x in a)



# ==========================================================================
#  확장부: 가상 리눅스 파일시스템 연동 + 추가 DC 명령
# ==========================================================================
import json as _json

SYN_ROOT = "/tools/synopsys/syn/" + VERSION
LIB_DB_DIR = "/tools/lib/edu_lib/db"
HDL_EXT = (".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl", ".vg")
APP_VAR_PREFIXES = ("hdlin_", "compile_", "power_", "timing_", "sh_", "report_", "verilogout_", "case_analysis_",
                    "link_", "alib_", "dc_", "auto_", "bus_", "synlib_", "optimize_", "enable_", "write_", "html_",
                    "lib_", "test_", "upf_", "mv_", "access_", "hlo_", "ungroup_", "change_names_", "default_",
                    "suppress_", "dct_", "spg_", "placer_", "physopt_", "ignore_", "svf_", "fsm_", "seqmap_",
                    "rtl_", "query_", "collection_", "enable_", "vhdlout_", "template_", "uniquify_", "single_")

SYSTEM_SETUP = """# $SYNOPSYS/admin/setup/.synopsys_dc.setup  (simulated system-wide setup)
set_app_var search_path ". %(r)s/libraries/syn %(r)s/dw/syn_ver %(r)s/dw/sim_ver"
set_app_var target_library    "your_library.db"
set_app_var link_library      "* your_library.db"
set_app_var synthetic_library ""
set_app_var symbol_library    "your_library.sdb"
""" % {"r": SYN_ROOT}

LIB_README = """edu_lib : dc_shell_sim 가상 표준셀 라이브러리
  db/edu_lib_tt.db   typical  1.05V  25C
  db/edu_lib_ss.db   slow     0.95V 125C   (setup worst)
  db/edu_lib_ff.db   fast     1.16V -40C   (hold worst)
  lib/*.lib          liberty 형식 텍스트 (cat 으로 확인 가능, 내용은 가상)
사용 예:
  set_app_var search_path "$search_path /tools/lib/edu_lib/db"
  set_app_var target_library edu_lib_tt.db
  set_app_var link_library   "* edu_lib_tt.db"
"""

DONT_USE_SUBST = {
    "XOR2X1": [("XNOR2X1", 1), ("INVX1", 1)], "XNOR2X1": [("NAND2X1", 4), ("INVX1", 1)],
    "FADDX1": [("XOR2X1", 2), ("AOI21X1", 1), ("NAND2X1", 2)], "HADDX1": [("XOR2X1", 1), ("AND2X1", 1)],
    "MUX21X1": [("AO22X1", 1), ("INVX1", 1)], "AO22X1": [("NAND2X1", 3)], "AOI21X1": [("NAND2X1", 2), ("INVX1", 1)],
    "OAI21X1": [("NOR2X1", 2), ("INVX1", 1)], "AND2X1": [("NAND2X1", 1), ("INVX1", 1)],
    "OR2X1": [("NOR2X1", 1), ("INVX1", 1)], "NAND2X2": [("NAND2X1", 1)], "INVX4": [("INVX1", 1)],
    "BUFX2": [("INVX1", 2)], "NAND2X1": [("NOR2X1", 1), ("INVX1", 3)], "NOR2X1": [("NAND2X1", 1), ("INVX1", 3)],
    "INVX1": [("NAND2X1", 1)], "DFFX1": [("DFFARX1", 1)], "DFFARX1": [("DFFX1", 1), ("AND2X1", 1)],
}


def liberty_text(lib):
    lines = ["/* %s.lib  -  dc_shell_sim simulated liberty (내용은 가상) */" % lib["name"],
             "library (%s) {" % lib["name"],
             "  delay_model : table_lookup ;",
             "  time_unit : \"1ns\" ;  voltage_unit : \"1V\" ;  capacitive_load_unit (1,pf) ;",
             "  leakage_power_unit : \"1pW\" ;",
             "  nom_voltage : %.2f ;" % lib["volt"],
             "  operating_conditions (%s) { voltage : %.2f ; }" % (lib["oc"], lib["volt"]),
             "  default_operating_conditions : %s ;" % lib["oc"], ""]
    for c, (ar, dl, k, ins, outp) in CELLS.items():
        lines.append("  cell (%s) {" % c)
        lines.append("    area : %.3f ;" % ar)
        if k == "seq":
            lines.append("    ff (IQ, IQN) { clocked_on : \"CLK\" ; next_state : \"D\" ; }")
        for p in ins:
            lines.append("    pin (%s) { direction : input ; capacitance : 0.0015 ; }" % p)
        lines.append("    pin (%s) { direction : output ;" % outp)
        lines.append("      timing () { related_pin : \"%s\" ; cell_rise (scalar) { values (\"%.3f\") ; } }"
                     % (ins[0] if k != "seq" else "CLK", dl * lib["scale"]))
        lines.append("    }")
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


_h(G_FILE, "define_design_lib", "디자인 라이브러리(WORK) 경로 지정", "define_design_lib WORK -path ./WORK")
_h(G_FILE, "report_design_lib", "analyze 된 모듈 목록", "report_design_lib WORK")
_h(G_FILE, "read_ddc", "write -format ddc 로 저장한 디자인 읽기", "read_ddc <file.ddc>")
_h(G_FILE, "read_sdc", "SDC 제약 파일 읽기", "read_sdc <file.sdc>")
_h(G_FILE, "check_library", "라이브러리 점검", "check_library")
_h(G_FILE, "change_names", "출력용 이름 규칙 적용", "change_names -rules verilog -hierarchy")
_h(G_FILE, "define_name_rules", "이름 규칙 정의", "define_name_rules <name> -allowed {a-zA-Z0-9_} ...")
_h(G_FILE, "report_names", "이름 규칙 위반 리포트", "report_names -rules verilog")
_h(G_CONS, "create_generated_clock", "생성 클럭", "create_generated_clock -name clk_div2 -source [get_ports clk] -divide_by 2 [get_pins ...]")
_h(G_CONS, "set_clock_groups", "클럭 그룹(비동기 등)", "set_clock_groups -asynchronous -group {clkA} -group {clkB}")
_h(G_CONS, "remove_clock", "클럭 삭제", "remove_clock <clk> | -all")
_h(G_CONS, "remove_input_delay", "입력 지연 삭제", "remove_input_delay [ports]")
_h(G_CONS, "remove_output_delay", "출력 지연 삭제", "remove_output_delay [ports]")
_h(G_CONS, "set_max_delay", "최대 지연 제약", "set_max_delay 1.5 -from [get_ports a] -to [get_ports y]")
_h(G_CONS, "set_min_delay", "최소 지연 제약", "set_min_delay 0.1 -from .. -to ..")
_h(G_CONS, "set_input_transition", "입력 트랜지션", "set_input_transition 0.1 [all_inputs]")
_h(G_CONS, "set_max_capacitance", "최대 커패시턴스", "set_max_capacitance 0.2 [current_design]")
_h(G_CONS, "set_case_analysis", "상수 고정 (모드 설정)", "set_case_analysis 0 [get_ports test_mode]")
_h(G_CONS, "report_case_analysis", "case analysis 리포트", "report_case_analysis")
_h(G_CONS, "set_disable_timing", "타이밍 아크 비활성", "set_disable_timing [get_cells ..]")
_h(G_CONS, "group_path", "경로 그룹", "group_path -name INPUTS -from [all_inputs]")
_h(G_CONS, "report_path_group", "경로 그룹 리포트", "report_path_group")
_h(G_CONS, "report_timing_requirements", "예외(false/multicycle/max_delay) 리포트", "report_timing_requirements")
_h(G_OPT, "set_dont_use", "특정 라이브러리 셀 사용 금지", "set_dont_use [get_lib_cells edu_lib_tt/XOR2X1]")
_h(G_OPT, "remove_attribute", "속성 제거 (dont_use 해제 등)", "remove_attribute [get_lib_cells */XOR2X1] dont_use")
_h(G_OPT, "set_flatten", "flatten 지시", "set_flatten true")
_h(G_OPT, "set_structure", "structuring 지시", "set_structure true -timing true")
_h(G_OPT, "set_ungroup", "compile 시 계층 풀기 지정", "set_ungroup [get_cells u_cnt] true")
_h(G_OPT, "set_optimize_registers", "레지스터 리타이밍 지정", "set_optimize_registers true -design mac")
_h(G_OPT, "insert_clock_gating", "클럭 게이팅 삽입", "insert_clock_gating")
_h(G_OPT, "optimize_netlist", "netlist 추가 최적화", "optimize_netlist -area")
_h(G_OPT, "set_host_options", "멀티코어 설정", "set_host_options -max_cores 4")
_h(G_OPT, "report_host_options", "멀티코어 설정 보기", "report_host_options")
_h(G_OPT, "set_svf", "formality 용 SVF 파일 지정", "set_svf outputs/top.svf")
_h(G_REP, "report_resources", "DesignWare 연산자 구현 리포트", "report_resources [-hierarchy]")
_h(G_REP, "report_net", "넷 리포트", "report_net [-connections]")
_h(G_REP, "report_clock_gating", "클럭 게이팅 리포트", "report_clock_gating")
_h(G_REP, "report_compile_options", "compile 옵션 리포트", "report_compile_options")
_h(G_REP, "report_app_var", "app var 목록", "report_app_var [pattern]")
_h(G_TCL, "glob", "파일 패턴 → 목록", "glob [-nocomplain] ./rtl/*.v")
_h(G_TCL, "getenv", "환경변수 읽기", "getenv HOME")
_h(G_TCL, "setenv", "환경변수 설정", "setenv MY_VAR value")
_h(G_TCL, "get_attribute", "속성 읽기", "get_attribute [current_design] area")
_h(G_TCL, "set_attribute", "속성 설정", "set_attribute [get_lib_cells */XOR2X1] dont_use true")
_h(G_TCL, "get_pins", "핀 collection", "get_pins u_alu/*")
_h(G_TCL, "all_fanin", "팬인 (근사)", "all_fanin -to [get_pins result_reg_0_/D] -startpoints_only")
_h(G_TCL, "all_fanout", "팬아웃 (근사)", "all_fanout -from [get_ports a] -endpoints_only")
_h(G_TCL, "filter_collection", "collection 필터", "filter_collection [all_inputs] \"name=~a*\"")
_h(G_TCL, "sort_collection", "collection 정렬", "sort_collection [all_inputs] name")
_h(G_TCL, "index_collection", "collection 원소", "index_collection [all_inputs] 0")
_h(G_TCL, "append_to_collection", "collection 추가", "append_to_collection var [get_ports a]")
_h(G_TCL, "copy_collection", "collection 복사", "copy_collection [all_inputs]")
_h(G_SYS, "sh", "리눅스 명령 실행", "sh mkdir -p reports   |  sh ls -l")
_h(G_SYS, "list_licenses", "체크아웃된 라이선스(가상)", "list_licenses")
_h(G_SYS, "get_license", "라이선스 체크아웃(가상)", "get_license DC-Ultra-Opt")


_TECH = {}
_LIBD = {}


class _DCWriter:
    tty = False
    screen = False

    def __init__(self):
        self.buf = []

    def write(self, s):
        self.buf.append(s)

    def text(self):
        return "".join(self.buf)


class _Null:
    tty = False
    screen = False

    def write(self, s):
        pass


class DCExtra:
    # ------------------------------------------------------------------
    def attach(self, term, fs, cwd, env, topo=False, no_home=False, no_local=False, logname="command.log"):
        self.term, self.fs, self.cwd, self.env = term, fs, cwd, env
        self.launch_cwd = cwd
        self.topo, self.no_home, self.no_local = topo, no_home, no_local
        self.vars["search_path"] = ". %s/libraries/syn %s/dw/syn_ver %s/dw/sim_ver" % (SYN_ROOT, SYN_ROOT, SYN_ROOT)
        extra = OrderedDict([
            ("synopsys_root", SYN_ROOT), ("sh_launch_dir", cwd), ("sh_dev_null", "/dev/null"),
            ("sh_continue_on_error", "true"), ("hdlin_auto_save_templates", "false"),
            ("compile_ultra_ungroup_dw", "true"), ("power_enable_analysis", "false"),
            ("timing_enable_multiple_clocks_per_reg", "true"), ("verilogout_no_tri", "false"),
            ("alib_library_analysis_path", "./alib-52"), ("case_analysis_with_logic_constants", "false"),
            ("compile_enable_register_merging", "true"), ("hdlin_infer_multibit", "default_none"),
            ("report_default_significant_digits", "2"), ("sh_enable_page_mode", "false"),
        ])
        for k, v in extra.items():
            self.vars[k] = v
            self.app_vars.add(k)
        self.design_libs = OrderedDict([("WORK", None)])
        self.host_cores = 1
        self.svf = None
        self.dont_use = set()
        self.attrs = {}
        self.name_rules = {"verilog": {}}
        self.licenses = ["DC-Expert", "DC-Ultra-Features", "DC-Ultra-Opt", "Design-Compiler", "DesignWare", "HDL-Compiler"]
        self._sub_warn = set()
        self._opts = {}
        self.prompt = "dc_shell-topo> " if topo else "dc_shell> "
        self.cmds["read_file"] = self.c_read_file
        self.cmds["write_file"] = self.c_write
        self.cmds["man"] = self.c_help_man
        self.cmds["quit"] = self.c_exit
        self.cmdlog = None
        try:
            if not logname:
                raise ValueError("no log")
            ap = fs.abspath(logname, cwd)
            real = fs.real(ap)
            ok = True
            if real and os.path.exists(real):
                with open(real, encoding="utf-8", errors="replace") as f:
                    ok = MARKER in f.read(200)
            if ok:
                self.cmdlog = fs.open_write(ap)
                self.cmdlog.write("#### %s command.log  -  %s\n" % (MARKER, dc_date()))
        except Exception:
            self.cmdlog = None

    def close(self):
        if self.cmdlog:
            try:
                self.cmdlog.close()
            except Exception:
                pass
            self.cmdlog = None

    # ---------------- 파일 연동 ----------------
    def abspath(self, p):
        return self.fs.abspath(self.to_str(p), self.cwd)

    def read_text(self, p):
        try:
            return self.fs.read_text(self.abspath(p))
        except Exception as e:
            raise DCError("Cannot read file '%s': %s" % (p, e), "FILE-1")

    def resolve(self, path):
        path = self.to_str(path)
        ap = self.abspath(path)
        if path.startswith(("/", "~", "./", "../")):
            return ap if self.fs.isfile(ap) else None
        if self.fs.isfile(ap):
            return ap
        for d in self.to_str(self.vars.get("search_path", ".")).split():
            c = self.fs.abspath(d.rstrip("/") + "/" + path, self.cwd)
            if self.fs.isfile(c):
                return c
        return None

    def safe_open(self, path, append=False):
        ap = self.abspath(path)
        try:
            return self.fs.open_write(ap, append, protect_hdl=True)
        except Exception as e:
            msg = str(e)
            hint = None
            if "protected" in msg:
                return self._protect_err(path)
            if "Read-only" in msg:
                hint = "실제 PC 폴더(/mnt/..)와 시스템 폴더는 읽기 전용입니다. 홈(~) 아래에 쓰세요."
            elif "No such file" in msg:
                hint = "폴더가 없습니다. sh mkdir -p <폴더> 또는 file mkdir <폴더> 로 먼저 만드세요."
            raise DCError("Cannot open file '%s' for writing: %s" % (path, msg), "FILE-4", hint=hint)

    def _protect_err(self, path):
        raise DCError("File '%s' is an existing HDL source not created by %s; refusing to overwrite." % (path, MARKER),
                      "SIM-1", hint="RTL 원본 보호 기능입니다. 출력 파일 이름을 바꾸세요 (예: outputs/top_netlist.v).")

    def getvar(self, name):
        m = re.match(r"env\((.+)\)$", name)
        if m:
            k = m.group(1)
            if k in self.env:
                return self.env[k]
            raise DCError("can't read \"env(%s)\": no such element in array" % k, "CMD-013", ret0=False)
        return ShellCore.getvar(self, name)

    def find_file(self, name):
        if "/" in name or name.startswith("~"):
            ap = self.abspath(name)
            return ap if self.fs.isfile(ap) else None
        return self.resolve(name)

    def _lib_exists(self, name):
        return bool(self.find_file(name))

    def _liberty_for(self, path):
        """.db 가상 경로 -> 대응 .lib 경로 (같은 폴더 또는 ../lib/)"""
        if path.lower().endswith(".lib"):
            return path
        base = path.rsplit("/", 1)[-1][:-3]
        d = path.rsplit("/", 1)[0]
        for c in (d + "/" + base + ".lib", d.rsplit("/", 1)[0] + "/lib/" + base + ".lib"):
            if self.fs.isfile(c):
                return c
        return None

    def tech_lib(self, vpath):
        real = self.fs.real(vpath)
        if not real:
            raise DCError("Can't read library file '%s'." % vpath, "UID-3")
        if real not in _TECH:
            import libparse
            cache = self.fs.real(self.fs.home + "/.cache")
            try:
                _TECH[real] = libparse.load(real, cache)
            except Exception as e:
                raise DCError("Error reading liberty file '%s': %s" % (vpath, e), "LBR-10")
        return _TECH[real]

    def libdict(self, vpath):
        t = self.tech_lib(vpath)
        du = frozenset(x for x in self.dont_use if x in t.cells)
        key = (t.path, du)
        if key in _LIBD:
            return _LIBD[key]
        cmap = {}
        for k in CELLS:
            cands = [(n, o) for n, o in t.role_cands.get(k, []) if n not in du]
            if not cands:
                cmap[k] = None
                continue
            idx = {"INVX4": 2, "NAND2X2": 1, "BUFX2": 1}.get(k, 0)
            n, o = cands[min(idx, len(cands) - 1)]
            cell = t.cells[n]
            kind = CELLS[k][2]
            if kind == "seq":
                ns = cell.ff.get("next_state", "D").strip('"').strip()
                clk = cell.ff.get("clocked_on", "CLK").strip('"').strip()
                clr = cell.ff.get("clear", "").strip('"').strip().lstrip("!").rstrip("'")
                qn = cell.ff.get("_q", "IQ")
                out = next((p for p in cell.outputs if (cell.pins[p].func or "").strip('"').strip() == qn),
                           cell.outputs[0] if cell.outputs else "Q")
                ins = [ns, clk] + ([clr] if clr else [])
            else:
                ins = cell.inputs
                out = o or next((p for p in cell.outputs if cell.pins[p].func), cell.outputs[0])
            dl, tr = t.arc_delay(n, out, tran=0.06, load=t.fanout_load(3))
            cmap[k] = (cell.area, dl, kind, ins, out, n, tr)
        nand = cmap.get("NAND2X1") or cmap.get("INVX1")
        dff = cmap.get("DFFARX1") or cmap.get("DFFX1")
        icg = (t.role_cands.get("ICG") or [(None, None)])[0][0]
        d = dict(name=t.name, oc=t.oc, volt=t.volt, temp="%g" % t.temp, leak=1.0, tech=t, cmap=cmap, file=vpath,
                 scale=(nand[1] / CELLS["NAND2X1"][1]) if nand else 1.0,
                 setup=t.constraint(dff[5], "setup", 0.06, 0.1) if dff else None,
                 hold=t.constraint(dff[5], "hold", 0.06, 0.1) if dff else None,
                 icg=icg, desc="%s (%d cells, %.2fV %gC)" % (t.oc, len(t.cells), t.volt, t.temp))
        _LIBD[key] = d
        return d

    def lib(self):
        tl = self.to_str(self.vars.get("target_library", "")).split()
        if not tl:
            raise DCError("target_library variable is empty.", "UIO-3",
                          hint="set_app_var target_library edu_lib_tt.db")
        name = tl[0]
        base = os.path.basename(name)
        if base not in LIBS and (base.endswith(".db") or base.endswith(".lib")):
            path = self.find_file(name)
            if not path:
                raise DCError("Can't find target_library file '%s'." % name, "UID-3",
                              hint="search_path 에 라이브러리 폴더를 추가하세요. 예) set_app_var search_path "
                                   "\"$search_path /tools/lib/sky130_fd_sc_hd/db\"   (list_libs 로 목록 확인)")
            lp = self._liberty_for(path)
            if not lp:
                raise DCError("Can't read '%s': the simulator needs the liberty (.lib) source next to the .db "
                              "(same folder or ../lib/)." % path, "UID-3")
            return self.libdict(lp)
        if base not in LIBS:
            raise DCError("Can't read link_library file '%s'." % base, "UID-3",
                          hint="가상 라이브러리는 /tools/lib/edu_lib/db 에 있습니다 (list_libs). 예) set_app_var target_library edu_lib_tt.db")
        if not self.find_file(name):
            raise DCError("Can't find target_library file '%s'." % name, "UID-3",
                          hint="search_path 에 라이브러리 폴더를 추가하세요: set_app_var search_path \"$search_path %s\"" % LIB_DB_DIR)
        return LIBS[base]

    def load_setup(self):
        files = [SYN_ROOT + "/admin/setup/.synopsys_dc.setup"]
        if not self.no_home:
            files.append(self.fs.home + "/.synopsys_dc.setup")
        if not self.no_local:
            files.append(self.fs.abspath(".synopsys_dc.setup", self.cwd))
        seen = []
        for f in files:
            if f in seen or not self.fs.isfile(f):
                continue
            seen.append(f)
            self.depth += 1
            try:
                self.run_script(self.fs.read_text(f), quiet=True)
            finally:
                self.depth -= 1

    def banner(self):
        lines = [
            "",
            "                           Design Compiler Graphical   (Simulated)",
            "                                 DC Ultra (TM)          (Simulated)",
            "                                  DFTMAX (TM)           (Simulated)",
            "                              Power Compiler (TM)       (Simulated)",
            "                                 DesignWare (R)         (Simulated)",
            "                                 DC Expert (TM)         (Simulated)",
            "                               HDL Compiler (TM)        (Simulated)",
            "",
            "               Version %s for linux64" % REPORT_VERSION,
            "",
            "      dc_shell_sim : 교육용 모의 프로그램입니다. Synopsys 제품이 아니며 실제 합성을 하지 않습니다.",
            "      모든 메시지와 리포트 수치는 화면 흐름 학습을 위해 가상으로 생성된 값입니다.",
            "",
            "   tutorial : 추천 학습 흐름     help : 명령 목록     man <cmd> : 사용법     exit : 리눅스로",
            "",
        ]
        for l in lines:
            self.out(l)
        if self.topo:
            self.out("Information: dc_shell is running in topographical mode (simulated: wire-load estimates only).")
        self.out("Initializing...")
        self.load_setup()

    def exec_line(self, line, from_script=False, quiet=False):
        if self.cmdlog and self.depth == 0:
            try:
                self.cmdlog.write(line.rstrip("\n") + "\n")
                self.cmdlog.flush()
            except Exception:
                pass
        return ShellCore.exec_line(self, line, from_script, quiet)

    # ---------------- 시스템 / 셸 ----------------
    def c_sh(self, a):
        line = " ".join(self.to_str(x) for x in a)
        if not line.strip():
            return ""
        if re.match(r"\s*(dc_shell|dc_shell-t)\b", line):
            raise DCError("cannot launch dc_shell from inside dc_shell.", "SIM-5")
        w = _DCWriter()
        st = self.term.run_line(line, out=w, err=w, cwd=self.cwd)
        t = w.text()
        if t:
            self.out(t.rstrip("\n"))
        return "" if st == 0 else ""

    def c_exec(self, a):
        return self.c_sh(a)

    def c_ls(self, a):
        return self.c_sh(["ls"] + [self.to_str(x) for x in a])

    def c_pwd(self, a):
        return self.cwd

    def c_cd(self, a):
        d = self.to_str(a[0]) if a else self.fs.home
        ap = self.abspath(d)
        if not self.fs.isdir(ap):
            raise DCError("Can't change directory to '%s'." % d, "SIM-4")
        self.cwd = ap
        return ""

    def c_getenv(self, a):
        k = self.to_str(a[0]) if a else ""
        if k not in self.env:
            raise DCError("Variable '%s' not found in environment." % k, "CMD-104")
        return self.env[k]

    def c_setenv(self, a):
        if len(a) < 2:
            raise DCError("wrong # args: should be \"setenv var value\"", "CMD-012")
        self.env[self.to_str(a[0])] = self.to_str(a[1])
        return self.env[self.to_str(a[0])]

    def c_get_unix_variable(self, a):
        return self.c_getenv(a)

    def c_set_unix_variable(self, a):
        return self.c_setenv(a)

    def c_file(self, a):
        sub = self.to_str(a[0]) if a else ""
        args = [self.to_str(x) for x in a[1:]]
        try:
            if sub == "mkdir":
                for p in args:
                    self.fs.mkdir(self.abspath(p), parents=True)
                return ""
            if sub == "exists":
                return 1 if self.fs.exists(self.abspath(args[0])) else 0
            if sub == "isdirectory":
                return 1 if self.fs.isdir(self.abspath(args[0])) else 0
            if sub == "isfile":
                return 1 if self.fs.isfile(self.abspath(args[0])) else 0
            if sub == "delete":
                force = "-force" in args
                for p in [x for x in args if not x.startswith("-")]:
                    ap = self.abspath(p)
                    if self.fs.exists(ap):
                        self.fs.remove(ap, recursive=force)
                return ""
            if sub == "join":
                return "/".join(x.rstrip("/") for x in args)
            if sub == "normalize":
                return self.abspath(args[0])
        except DCError:
            raise
        except Exception as e:
            raise DCError("file %s: %s" % (sub, e), "FILE-4")
        if sub in ("tail", "rootname", "dirname", "extension"):
            p = args[0]
            return {"tail": p.rstrip("/").rsplit("/", 1)[-1], "rootname": os.path.splitext(p)[0],
                    "dirname": (p.rsplit("/", 1)[0] if "/" in p else "."), "extension": os.path.splitext(p)[1]}[sub]
        raise DCError("file %s: not supported (mkdir/exists/isdirectory/isfile/delete/join/normalize/tail/rootname/dirname/extension)" % sub, "SIM-3")

    def c_glob(self, a):
        nocomp = False
        types = None
        pats = []
        k = 0
        while k < len(a):
            x = self.to_str(a[k])
            if x == "-nocomplain":
                nocomp = True
            elif x == "-types" and k + 1 < len(a):
                types = self.to_str(a[k + 1])
                k += 1
            elif x == "--":
                pass
            else:
                pats += self.tcl_list(x) if " " in x and not self.fs.exists(self.abspath(x)) else [x]
            k += 1
        res = []
        saved = self.term.cwd
        self.term.cwd = self.cwd
        try:
            for p in pats:
                for r in (self.term.glob(p) if any(c in p for c in "*?[") else
                          ([p] if self.fs.exists(self.abspath(p)) else [])):
                    ap = self.abspath(r)
                    if types == "d" and not self.fs.isdir(ap):
                        continue
                    if types == "f" and not self.fs.isfile(ap):
                        continue
                    res.append(r)
        finally:
            self.term.cwd = saved
        if not res and not nocomp:
            raise DCError("no files matched glob pattern%s \"%s\"" % ("s" if len(pats) > 1 else "", " ".join(pats)),
                          "CMD-013", hint="경로를 확인하세요 (sh ls).  없을 수도 있으면 glob -nocomplain")
        return self.list_str(res)

    def c_list_licenses(self, a):
        self.out("Licenses in use:")
        for l in self.licenses:
            self.out("        %s" % l)
        return 1

    def c_get_license(self, a):
        for x in a:
            n = self.to_str(x)
            if n not in self.licenses:
                self.licenses.append(n)
        return 1

    def c_license_users(self, a):
        self.out("%s@%s  %s" % (self.env.get("USER", "user"), "dc-sim-host", " ".join(self.licenses)))
        return 1

    def c_set_host_options(self, a):
        o, pos = self.opts("set_host_options", a, valued=["-max_cores", "-name", "-num_processes"])
        if "-max_cores" in o:
            self.host_cores = int(self.fnum(o["-max_cores"], "-max_cores"))
            self.info("Running optimization using a maximum of %d cores." % self.host_cores, "OPT-1500")
        return 1

    def c_report_host_options(self, a):
        self.header("host_options")
        self.out("  Max Cores        : %d" % self.host_cores)
        self.out("  Host             : dc-sim-host (simulated)")
        self.out("")
        return 1

    def c_set_svf(self, a):
        o, pos = self.opts("set_svf", a, flags=["-append", "-off"])
        if "-off" in o:
            self.svf = None
            return 1
        if not pos:
            raise DCError("Required argument 'file_name' was not found", "CMD-007")
        fn = self.to_str(pos[0])
        f = self.safe_open(fn, "-append" in o)
        f.write("# %s simulated SVF (Formality guide)\nguide -name %s\n" % (MARKER, fn))
        f.close()
        self.svf = fn
        self.info("Setting svf file to '%s'." % fn, "SVF-?")
        return 1

    def c_report_app_var(self, a):
        pat = self.to_str(a[-1]) if a and not self.to_str(a[-1]).startswith("-") else "*"
        self.out("")
        self.out("%-40s %-12s %s" % ("Variable", "Type", "Value"))
        self.out("-" * 90)
        for k in sorted(self.app_vars):
            if fnmatch.fnmatchcase(k, pat):
                self.out("%-40s %-12s %s" % (k, "string", self.to_str(self.vars.get(k, ""))))
        self.out("")
        return 1

    def c_set_app_var(self, a):
        if len(a) == 2:
            name = self.to_str(a[0])
            if name not in self.app_vars and name.startswith(APP_VAR_PREFIXES):
                self.app_vars.add(name)
        return ShellCore.c_set_app_var(self, a)

    # ---------------- 읽기 ----------------
    def c_define_design_lib(self, a):
        o, pos = self.opts("define_design_lib", a, valued=["-path"])
        if not pos or "-path" not in o:
            raise DCError("wrong # args: should be \"define_design_lib WORK -path ./WORK\"", "CMD-012")
        name = self.to_str(pos[0])
        p = self.abspath(o["-path"])
        if not self.fs.isdir(p):
            try:
                self.fs.mkdir(p, parents=True)
            except Exception as e:
                raise DCError("Cannot create directory '%s' for design library: %s" % (self.to_str(o["-path"]), e),
                              "LBR-1", hint="현재 폴더가 읽기 전용(PC 폴더)이면 홈 아래 프로젝트에서 실행하세요.")
        self.design_libs[name] = p
        return 1

    def _collect_hdl(self, items, recursive):
        files = []
        for it in items:
            for p in self.tcl_list(it):
                ap = self.abspath(p)
                if self.fs.isdir(ap):
                    stack = [(ap, p.rstrip("/"))]
                    while stack:
                        d, shown = stack.pop(0)
                        try:
                            names = self.fs.listdir(d)
                        except Exception:
                            continue
                        for nm in names:
                            cap = self.fs.abspath(nm, d)
                            if self.fs.isdir(cap):
                                if recursive and nm not in ("WORK", "outputs", "reports") and not nm.startswith("."):
                                    stack.append((cap, shown + "/" + nm))
                            elif nm.lower().endswith((".v", ".sv")) and not nm.lower().endswith("_netlist.v"):
                                files.append(shown + "/" + nm)
                elif self.fs.isfile(ap) or self.resolve(p):
                    files.append(p)
                else:
                    self.out("Error: Cannot find the file '%s'. (FILE-1)" % p, "31")
        return files

    def c_analyze(self, a):
        o, pos = self.opts("analyze", a, flags=["-autoread", "-recursive", "-update"],
                           valued=["-format", "-library", "-define", "-work", "-top", "-exclude"])
        if "-autoread" in o:
            files = self._collect_hdl(pos, "-recursive" in o)
            if not files:
                raise DCError("No HDL files found for -autoread.", "AUTOREAD-1")
            fmt = "sverilog" if any(f.lower().endswith(".sv") for f in files) else "verilog"
            self.info("Autoread: %d source files found (%s)." % (len(files), fmt), "AUTOREAD-100")
        else:
            files = []
            for p in pos:
                files += self.tcl_list(p)
            fmt = self.to_str(o.get("-format", "verilog"))
        lib = self.to_str(o.get("-library", o.get("-work", "WORK")))
        if lib not in self.design_libs:
            self.warn("Design library '%s' is not defined; using WORK." % lib, "LBR-1")
        before = {k: v for k, v in self.modules.items()}
        r = ShellCore.c_analyze(self, ["-format", fmt] + ["{%s}" % f for f in files])
        wdir = self.design_libs.get("WORK")
        if wdir:
            for name, m in self.modules.items():
                if before.get(name) is m:
                    continue
                for ext in (".pvl", ".syn", ".mr"):
                    try:
                        f = self.fs.open_write(wdir + "/" + name.upper() + ext)
                        f.write("%s design library unit %s (%s)\nsource %s\n" % (MARKER, name, ext, m.path))
                        f.close()
                    except Exception:
                        pass
        return r

    def c_read_file(self, a):
        o, pos = self.opts("read_file", a, flags=["-autoread", "-recursive", "-rtl", "-netlist"],
                           valued=["-format", "-top", "-library"])
        fmt = self.to_str(o.get("-format", "verilog")).lower()
        if fmt == "ddc":
            return self.c_read_ddc(pos)
        if "-autoread" in o:
            before = set(self.modules)
            args = ["-autoread"] + (["-recursive"] if "-recursive" in o else []) + pos
            if self.c_analyze(args) != 1:
                return 0
            new = [m for m in self.modules if m not in before] or list(self.modules)
            top = self.to_str(o["-top"]) if "-top" in o else None
            if not top:
                inst = set(t[0] for m in new for t in self.modules[m].instances)
                cands = [m for m in new if m not in inst]
                if not cands:
                    raise DCError("Cannot determine top design.", "AUTOREAD-205", hint="-top <module> 을 지정하세요.")
                if len(cands) > 1:
                    self.warn("Multiple top candidates (%s); choosing '%s'." % (" ".join(cands), cands[-1]), "AUTOREAD-206")
                top = cands[-1]
            self.info("Top design is '%s'." % top, "AUTOREAD-102")
            return self.c_elaborate([top])
        return self.c_read_verilog(a)

    def c_report_design_lib(self, a):
        name = self.to_str(a[0]) if a else "WORK"
        p = self.design_libs.get(name)
        self.out("")
        self.out("Design units in library %s%s:" % (name, " (%s)" % p if p else " (in memory)"))
        self.out("")
        self.out("  %-24s %-10s %s" % ("Design Unit", "Format", "Source"))
        self.out("  " + "-" * 76)
        for m in self.modules.values():
            self.out("  %-24s %-10s %s" % (m.name.upper(), "sverilog" if m.path.endswith(".sv") else "verilog", m.path))
        self.out("")
        return 1

    def c_read_sdc(self, a):
        o, pos = self.opts("read_sdc", a, flags=["-echo"], valued=["-version"])
        if not pos:
            raise DCError("Required argument 'file_name' was not found", "CMD-007")
        self.need_design()
        p = self.resolve(self.to_str(pos[0]))
        if not p:
            raise DCError("Cannot find the file '%s'." % self.to_str(pos[0]), "CMD-015")
        self.out("Reading SDC version 2.1...")
        text = self.read_text(p)
        self.depth += 1
        try:
            self.run_script(text, echo="-echo" in o, quiet=True)
        finally:
            self.depth -= 1
        return 1

    def c_check_library(self, a):
        lib = self.lib()
        if lib.get("tech"):
            return self._check_tech_library(lib)
        self.out("")
        self.out("#BEGIN_LIBSCREEN_INFO")
        self.out("")
        self.out("Logic Library:   %s" % lib["name"])
        self.out("  Library file : %s" % (self.find_file(self.to_str(self.vars["target_library"]).split()[0]) or "?"))
        self.out("  Cells        : %d (comb %d, seq %d)" % (len(CELLS), sum(1 for c in CELLS.values() if c[2] != "seq"),
                                                         sum(1 for c in CELLS.values() if c[2] == "seq")))
        self.out("  Units        : time 1ns, cap 1pF, leakage 1pW")
        self.out("")
        self.out("#END_LIBSCREEN_INFO")
        self.out("")
        self.out("Check Summary : 0 Errors, 0 Warnings   (simulated)")
        return 1

    # ---------------- ddc 저장/읽기 ----------------
    def _meta(self, d, fmt="ddc"):
        designs = self.all_designs_under(d)
        hashes = OrderedDict((x.mod.name, hashlib.md5(x.mod.body.encode("utf-8", "replace")).hexdigest()[:16])
                             for x in designs)
        return {"marker": MARKER, "format": fmt, "version": VERSION, "top": d.name,
                "target_library": self.to_str(self.vars.get("target_library")),
                "link_library": self.to_str(self.vars.get("link_library")),
                "modules": OrderedDict((x.mod.name, x.mod.path) for x in designs),
                "hashes": hashes,
                "designs": [dict(name=x.name, module=x.mod.name, env=x.env, mapped=x.mapped, effort=x.effort,
                                 flat=x.flat, gated=bool(getattr(x, "gated", False))) for x in reversed(designs)],
                "dont_use": sorted(self.dont_use),
                "sdc": d.sdc}

    def _netlist_meta(self, d):
        return "// SIMMETA " + _json.dumps(self._meta(d, "verilog"), ensure_ascii=True)

    def restore_from_meta(self, data, apply_sdc=True, lib_override=None):
        """ddc / netlist 메타데이터로 디자인 복원 (조용히)"""
        null = _Null()
        self.streams.append(null)
        try:
            for mname, mpath in data["modules"].items():
                if mname not in self.modules:
                    try:
                        for m in parse_verilog_text(self.fs.read_text(mpath), mpath)[0]:
                            self.modules.setdefault(m.name, m)
                    except Exception:
                        pass
            for ent in data["designs"]:
                if ent["module"] in self.modules:
                    self.elab(ent["module"], ent.get("env") or {})
            top = data["top"]
            if top not in self.designs:
                raise DCError("RTL source for '%s' not found (%s); cannot restore design." % (
                    top, data["modules"].get(top, "?")), "DDC-3")
            self.cur = top
            td = self.designs[top]
            td.linked = True
            for ent in data["designs"]:
                x = self.designs.get(ent["name"])
                if x is not None:
                    x.gated = ent.get("gated", False)
            if apply_sdc:
                for line in data.get("sdc", []):
                    try:
                        self.eval(line)
                    except Exception:
                        pass
            for c in data.get("dont_use", []):
                self.dont_use.add(c)
            mapped = [e for e in data["designs"] if e.get("mapped")]
            if mapped:
                if lib_override is None:
                    self.vars["target_library"] = data["target_library"]
                    self.vars["link_library"] = data["link_library"]
                eff = mapped[-1].get("effort") or "medium"
                self.synth(td, "high" if eff == "ultra" else eff, eff == "ultra", self.lib())
            return top
        finally:
            self.streams.pop()

    def c_set_units(self, a):
        return 1

    def c_write(self, a):
        o, pos = self.opts("write", a, flags=["-hierarchy", "-no_implicit"], valued=["-format", "-output", "-library"])
        fmt = self.to_str(o.get("-format", "ddc")).lower()
        if fmt != "ddc":
            return ShellCore.c_write(self, a)
        d = self.need_design()
        out = self.to_str(o.get("-output", "%s.ddc" % d.name))
        data = self._meta(d, "ddc")
        f = self.safe_open(out)
        try:
            f.write(_json.dumps(data, ensure_ascii=False, indent=1))
        finally:
            f.close()
        self.out("Writing ddc file '%s'." % self.abspath(out))
        self.out("Writing ddc file '%s' done (simulated JSON container)." % out)
        return 1

    def c_read_ddc(self, a):
        files = []
        for p in a:
            files += self.tcl_list(p)
        if not files:
            raise DCError("Required argument 'file_names' was not found", "CMD-007")
        last = None
        for fn in files:
            p = self.resolve(fn)
            if not p:
                raise DCError("Cannot find the file '%s'." % fn, "CMD-015")
            try:
                data = _json.loads(self.read_text(p))
                assert data.get("marker") == MARKER
            except Exception:
                raise DCError("'%s' is not a valid ddc file (only ddc written by this simulator can be read)." % fn, "DDC-2")
            self.out("Reading ddc file '%s'." % p)
            top = self.restore_from_meta(data)
            self.out("Loaded %d designs." % len(data["designs"]))
            self.out("Current design is '%s'." % top)
            last = top
        return Coll("design", [last])

    # ---------------- 제약 추가 ----------------
    def _store(self, d, key, val):
        if not hasattr(d, "x_" + key):
            setattr(d, "x_" + key, [])
        getattr(d, "x_" + key).append(val)

    def c_create_generated_clock(self, a):
        d = self.need_design()
        o, pos = self.opts("create_generated_clock", a, flags=["-invert", "-add", "-combinational"],
                           valued=["-name", "-source", "-divide_by", "-multiply_by", "-master_clock", "-edges"])
        if "-source" not in o:
            raise DCError("Required argument '-source' was not found", "CMD-007")
        src = self.names_of(o["-source"])
        master = None
        for c, info in d.clocks.items():
            if any(s in info["src"] or s == c for s in src):
                master = c
        if self.to_str(o.get("-master_clock", "")):
            master = self.to_str(o["-master_clock"])
        if not master:
            raise DCError("Can't find master clock for source '%s'." % " ".join(src), "UID-?",
                          hint="create_clock 으로 마스터 클럭을 먼저 정의하세요.")
        div = self.fnum(o.get("-divide_by", 1))
        mul = self.fnum(o.get("-multiply_by", 1))
        per = d.clocks[master]["period"] * div / mul
        targets = []
        for p in pos:
            targets += self.names_of(p)
        name = self.to_str(o.get("-name", targets[0] if targets else "gclk"))
        d.clocks[name] = dict(period=per, wave=[0, per / 2], src=targets, gen=master)
        self.sdc(d, "create_generated_clock -name %s -source [get_ports {%s}] %s [get_pins {%s}]" % (
            name, " ".join(src), ("-divide_by %s" % self.to_str(div)) if "-divide_by" in o else ("-multiply_by %s" % self.to_str(mul)),
            " ".join(targets)))
        return 1

    def c_set_clock_groups(self, a):
        d = self.need_design()
        groups = []
        kind = "asynchronous"
        k = 0
        while k < len(a):
            x = self.to_str(a[k])
            if x == "-group" and k + 1 < len(a):
                groups.append(self.names_of(a[k + 1]))
                k += 2
                continue
            if x in ("-asynchronous", "-logically_exclusive", "-physically_exclusive"):
                kind = x[1:]
            k += 1
        self._store(d, "clock_groups", (kind, groups))
        self.sdc(d, "set_clock_groups -%s %s" % (kind, " ".join("-group {%s}" % " ".join(g) for g in groups)))
        return 1

    def c_remove_clock(self, a):
        d = self.need_design()
        if a and self.to_str(a[0]) == "-all":
            d.clocks.clear()
            self.sdc(d, "remove_clock -all")
            return 1
        for x in a:
            for n in self.names_of(x):
                if n in d.clocks:
                    del d.clocks[n]
                    self.sdc(d, "remove_clock %s" % n)
                else:
                    self.warn("Can't find clock '%s'." % n, "UID-95")
        return 1

    def _rm_delay(self, a, store, cmd):
        d = self.need_design()
        o, pos = self.opts(cmd, a, flags=["-max", "-min", "-rise", "-fall"], valued=["-clock"])
        ports = self.ports_from(d, pos) if pos else list(store)
        for p in ports:
            store.pop(p, None)
        self.sdc(d, "%s [get_ports {%s}]" % (cmd, " ".join(ports)))
        return 1

    def c_remove_input_delay(self, a):
        return self._rm_delay(a, self.need_design().in_delay, "remove_input_delay")

    def c_remove_output_delay(self, a):
        return self._rm_delay(a, self.need_design().out_delay, "remove_output_delay")

    def _minmax(self, a, cmd):
        d = self.need_design()
        o, pos = self.opts(cmd, a, flags=["-rise", "-fall", "-reset_path"], valued=["-from", "-to", "-through"])
        if not pos:
            raise DCError("Required argument 'delay_value' was not found", "CMD-007")
        v = self.fnum(pos[0])
        fr, to = self.names_of(o.get("-from", "")), self.names_of(o.get("-to", ""))
        self._store(d, "minmax", (cmd, v, fr, to))
        self.sdc(d, "%s %s%s%s" % (cmd, self.to_str(v), (" -from {%s}" % " ".join(fr)) if fr else "",
                                   (" -to {%s}" % " ".join(to)) if to else ""))
        return 1

    def c_set_max_delay(self, a):
        return self._minmax(a, "set_max_delay")

    def c_set_min_delay(self, a):
        return self._minmax(a, "set_min_delay")

    def c_set_input_transition(self, a):
        d = self.need_design()
        o, pos = self.opts("set_input_transition", a, flags=["-max", "-min", "-rise", "-fall"])
        v = self.fnum(pos[0])
        ports = self.ports_from(d, pos[1:])
        self.sdc(d, "set_input_transition %s [get_ports {%s}]" % (self.to_str(v), " ".join(ports)))
        return 1

    def c_set_max_capacitance(self, a):
        d = self.need_design()
        v = self.fnum(a[0]) if a else 0
        self.sdc(d, "set_max_capacitance %s [current_design]" % self.to_str(v))
        return 1

    def c_set_case_analysis(self, a):
        d = self.need_design()
        if len(a) < 2:
            raise DCError("wrong # args: should be \"set_case_analysis value port_or_pin_list\"", "CMD-012")
        v = self.to_str(a[0])
        if v not in ("0", "1", "rising", "falling", "zero", "one"):
            raise DCError("Invalid value '%s' (0|1|rising|falling)." % v, "CMD-019")
        ports = self.ports_from(d, a[1:])
        if not hasattr(d, "case"):
            d.case = OrderedDict()
        for p in ports:
            d.case[p] = v
        self.sdc(d, "set_case_analysis %s [get_ports {%s}]" % (v, " ".join(ports)))
        return 1

    def c_report_case_analysis(self, a):
        d = self.need_design()
        self.header("case_analysis")
        self.out("%-30s %s" % ("Pin name", "User case analysis value"))
        self.out("-" * 60)
        for p, v in getattr(d, "case", {}).items():
            self.out("%-30s %s" % (p, v))
        if not getattr(d, "case", None):
            self.out("No case analysis values set.")
        self.out("")
        return 1

    def c_set_disable_timing(self, a):
        d = self.need_design()
        o, pos = self.opts("set_disable_timing", a, valued=["-from", "-to"])
        objs = []
        for p in pos:
            objs += self.names_of(p)
        self._store(d, "disable", objs)
        self.sdc(d, "set_disable_timing [get_cells {%s}]" % " ".join(objs))
        return 1

    def c_group_path(self, a):
        d = self.need_design()
        o, pos = self.opts("group_path", a, flags=["-default"], valued=["-name", "-from", "-to", "-through", "-weight",
                                                                        "-critical_range"])
        name = self.to_str(o.get("-name", "default"))
        self._store(d, "groups", (name, self.names_of(o.get("-from", "")), self.names_of(o.get("-to", "")),
                                  self.to_str(o.get("-weight", "1"))))
        return 1

    def c_report_path_group(self, a):
        d = self.need_design()
        self.header("path_group")
        self.out("%-16s %-8s %-30s %s" % ("Group", "Weight", "From", "To"))
        self.out("-" * 76)
        for c in d.clocks:
            self.out("%-16s %-8s %-30s %s" % (c, "1.00", "(clock)", "(clock)"))
        for n, fr, to, w in getattr(d, "x_groups", []):
            self.out("%-16s %-8s %-30s %s" % (n, w, " ".join(fr)[:30] or "*", " ".join(to) or "*"))
        self.out("%-16s %-8s %-30s %s" % ("**default**", "1.00", "*", "*"))
        self.out("")
        return 1

    def c_report_timing_requirements(self, a):
        d = self.need_design()
        self.header("timing_requirements")
        self.out("%-14s %-30s %-30s %s" % ("Requirement", "From", "To", "Value"))
        self.out("-" * 86)
        n = 0
        for fr, to in d.false_paths:
            self.out("%-14s %-30s %-30s %s" % ("false_path", " ".join(fr) or "*", " ".join(to) or "*", "FALSE"))
            n += 1
        for m, fr, to in d.mcp:
            self.out("%-14s %-30s %-30s %s" % ("multicycle", " ".join(fr) or "*", " ".join(to) or "*", m))
            n += 1
        for cmd, v, fr, to in getattr(d, "x_minmax", []):
            self.out("%-14s %-30s %-30s %.2f" % (cmd.replace("set_", ""), " ".join(fr) or "*", " ".join(to) or "*", v))
            n += 1
        if not n:
            self.out("No timing exceptions.")
        self.out("")
        return 1

    # ---------------- 최적화 지시 ----------------
    def _cur_tech(self):
        try:
            return self.lib().get("tech")
        except DCError:
            return None

    def _lib_cell_names(self, items):
        names = []
        t = self._cur_tech()
        pool = list(t.cells) if t else list(CELLS)
        for it in items:
            for n in self.names_of(it):
                cell = n.split("/")[-1]
                hit = [c for c in pool if fnmatch.fnmatchcase(c, cell)]
                if not hit:
                    self.warn("Can't find lib_cell '%s'." % n, "UID-993")
                names += hit
        return names

    def c_set_dont_use(self, a):
        o, pos = self.opts("set_dont_use", a, flags=["-power"])
        val = True
        if len(pos) > 1 and self.to_str(pos[-1]) in ("true", "false"):
            val = self.to_str(pos[-1]) == "true"
            pos = pos[:-1]
        for c in self._lib_cell_names(pos):
            if val:
                self.dont_use.add(c)
            else:
                self.dont_use.discard(c)
        self._mark_stale()
        return 1

    def c_remove_attribute(self, a):
        if len(a) < 2:
            raise DCError("wrong # args: should be \"remove_attribute objects attr\"", "CMD-012")
        attr = self.to_str(a[1])
        if attr == "dont_use":
            for c in self._lib_cell_names([a[0]]):
                self.dont_use.discard(c)
            return 1
        for n in self.names_of(a[0]):
            for key in list(self.attrs):
                if key[1] == n and key[2] == attr:
                    del self.attrs[key]
            if attr == "dont_touch" and n in self.designs:
                self.designs[n].dont_touch = False
        return 1

    def _mark_stale(self):
        pass

    def _dflag(self, a, cmd, attr):
        o, pos = self.opts(cmd, a, valued=["-design", "-timing", "-minimize", "-effort", "-flatten"])
        val = True
        if pos and self.to_str(pos[0]) in ("true", "false"):
            val = self.to_str(pos[0]) == "true"
            pos = pos[1:]
        names = []
        for p in pos:
            names += self.names_of(p)
        if "-design" in o:
            names += self.names_of(o["-design"])
        if not names:
            names = [self.need_design().name]
        for n in names:
            if n in self.designs:
                setattr(self.designs[n], attr, val)
            else:
                d = self.need_design()
                if attr == "x_ungroup" and any(i == n for i, _ in d.subs):
                    d.__dict__.setdefault("x_ungroup_cells", set())
                    (d.x_ungroup_cells.add if val else d.x_ungroup_cells.discard)(n)
        return 1

    def c_set_flatten(self, a):
        return self._dflag(a, "set_flatten", "x_flatten")

    def c_set_structure(self, a):
        return self._dflag(a, "set_structure", "x_structure")

    def c_set_ungroup(self, a):
        return self._dflag(a, "set_ungroup", "x_ungroup")

    def c_set_optimize_registers(self, a):
        return self._dflag(a, "set_optimize_registers", "x_retime")

    def c_insert_clock_gating(self, a):
        d = self.need_design()
        for x in self.all_designs_under(d):
            x.gated = True
        g, n, groups = self._gating(d)
        self.info("Performing clock-gating on design %s" % d.name, "PWR-730")
        self.out("Clock gating: %d of %d registers gated using %d clock-gating cells." % (g, n, groups))
        return 1

    def c_optimize_netlist(self, a):
        o, pos = self.opts("optimize_netlist", a, flags=["-area", "-no_boundary_optimization"])
        d = self.need_design()
        if not d.mapped:
            raise DCError("Design '%s' is not mapped; run compile first." % d.name, "OPT-1206")
        before = self.totals(d)
        for x in self.all_designs_under(d):
            if x.st:
                for k in list(x.st["cells"]):
                    if CELLS[k][2] == "comb":
                        x.st["cells"][k] = int(round(x.st["cells"][k] * 0.965))
                self.finalize(x, x.st, x.st["lib"], True)
        after = self.totals(d)
        self.out("  Beginning netlist optimization (area)")
        self.out("  Area: %.2f -> %.2f" % (before["comb_area"] + before["seq_area"], after["comb_area"] + after["seq_area"]))
        self.out("  Optimization Complete")
        return 1

    def gate_factor(self, d):
        if not getattr(d, "gated", False):
            return 1.0
        g, n, _ = self._gating(d, own=True)
        return 1.0 - 0.6 * (g / float(n)) if n else 1.0

    def _gating(self, d, own=False):
        designs = [d] if own else self.all_designs_under(d)
        gated = total = groups = 0
        for x in designs:
            m = x.mod
            body = m.body
            enable_like = len(re.findall(r"\belse\s+if\b", body)) > 0
            for n, w in x.flops:
                total += w
                if getattr(x, "gated", False) and enable_like and w >= 3:
                    gated += w
                    groups += 1
        return gated, total, groups

    def c_report_clock_gating(self, a):
        d = self.need_design()
        self.header("clock_gating")
        g, n, groups = self._gating(d)
        bar = "-" * 61
        self.out("")
        self.out("                             Clock Gating Summary")
        self.out("          " + bar)
        self.out("          |    Number of Clock gating elements    |  %8d        |" % groups)
        self.out("          |                                       |                 |")
        self.out("          |    Number of Gated registers          |  %4d (%6.2f%%) |" % (g, 100.0 * g / n if n else 0))
        self.out("          |                                       |                 |")
        self.out("          |    Number of Ungated registers        |  %4d (%6.2f%%) |" % (n - g, 100.0 * (n - g) / n if n else 0))
        self.out("          |                                       |                 |")
        self.out("          |    Total number of registers          |  %8d        |" % n)
        self.out("          " + bar)
        try:
            icg = self.lib().get("icg")
        except DCError:
            icg = None
        if icg and g:
            self.out("          Clock gating cell : %s (library integrated clock gate)" % icg)
        if not g:
            self.hint("compile_ultra -gate_clock 또는 insert_clock_gating 을 사용하면 enable 이 있는 레지스터가 게이팅됩니다.")
        self.out("")
        return 1

    # ---------------- 합성 훅 ----------------
    def own_stats(self, d, effort, ultra, lib, mapped=True):
        st = ShellCore.own_stats(self, d, effort, ultra, lib, mapped)
        cells = st["cells"]
        if self.dont_use or lib.get("cmap"):
            for _ in range(4):
                changed = False
                for c in list(cells):
                    if self._unavail(c, lib) and cells[c] > 0:
                        sub = DONT_USE_SUBST.get(c)
                        if not sub or any(self._unavail(s, lib) for s, _ in sub):
                            self._sub_warn.add(c)
                            continue
                        n = cells.pop(c)
                        for s, k in sub:
                            cells[s] += n * k
                        st["delay"] *= 1.04
                        changed = True
                if not changed:
                    break
        if (self._opts.get("retime") or getattr(d, "x_retime", False)) and d.flops:  # noqa
            st["delay"] *= 0.92
            st["cells"]["DFFARX1" if d.mod.async_reset else "DFFX1"] += max(1, sum(w for _, w in d.flops) // 8)
        if getattr(d, "x_flatten", False):
            st["delay"] *= 0.97
        return st

    def _unavail(self, k, lib):
        cm = lib.get("cmap")
        if cm is not None:
            return cm.get(k) is None
        return k in self.dont_use

    def run_compile(self, ultra, a):
        args = [self.to_str(x) for x in a]
        self._opts = {"retime": "-retime" in args, "gate": "-gate_clock" in args}
        self._sub_warn = set()
        d = self.need_design()
        for inst in sorted(getattr(d, "x_ungroup_cells", set())):
            if any(i == inst for i, _ in d.subs):
                self._flatten(d, only={inst})
                self.info("Ungrouping cell '%s' (set_ungroup)." % inst, "OPT-776")
        for x in self.all_designs_under(d):
            if getattr(x, "x_ungroup", False) and x is d and d.subs:
                self._flatten(d)
        if self.host_cores > 1:
            self.info("Running optimization using a maximum of %d cores." % self.host_cores, "OPT-1500")
        if self.dont_use:
            self.info("Cells marked dont_use: %s" % " ".join(sorted(self.dont_use)), "OPT-1307")
        r = ShellCore.run_compile(self, ultra, a)
        for c in sorted(self._sub_warn):
            self.warn("Can't find a usable replacement for dont_use cell '%s'; it is still used." % c, "OPT-101")
        if self._opts["gate"]:
            for x in self.all_designs_under(d):
                x.gated = True
            g, n, groups = self._gating(d)
            self.info("Clock gating: %d of %d registers gated (%d ICG cells)." % (g, n, groups), "PWR-730")
        for x in self.all_designs_under(d):
            x.compile_opts = " ".join(["compile_ultra" if ultra else "compile"] + [y for y in args if y.startswith("-")])
        svf = self.svf or "default.svf"
        try:
            f = self.safe_open(svf, append=bool(self.svf))
            f.write("# %s simulated SVF\nguide_compile %s\n" % (MARKER, d.name))
            f.close()
        except DCError:
            pass
        return r

    def c_report_compile_options(self, a):
        d = self.need_design()
        self.header("compile_options")
        self.out("%-24s %-10s %-10s %-10s %s" % ("Design", "Mapped", "Flatten", "Structure", "Last compile"))
        self.out("-" * 86)
        for x in self.all_designs_under(d):
            self.out("%-24s %-10s %-10s %-10s %s" % (x.name, x.mapped, getattr(x, "x_flatten", False),
                                                     getattr(x, "x_structure", True), getattr(x, "compile_opts", "-")))
        self.out("")
        return 1

    # ---------------- 이름 규칙 ----------------
    def c_define_name_rules(self, a):
        if not a:
            raise DCError("Required argument 'name' was not found", "CMD-007")
        self.name_rules[self.to_str(a[0])] = {"args": [self.to_str(x) for x in a[1:]]}
        return 1

    def c_change_names(self, a):
        o, pos = self.opts("change_names", a, flags=["-hierarchy", "-verbose", "-log_changes"], valued=["-rules"])
        d = self.need_design()
        rule = self.to_str(o.get("-rules", "verilog"))
        if rule not in self.name_rules:
            raise DCError("Name rules '%s' not defined." % rule, "UCN-1", hint="define_name_rules 로 먼저 정의하세요.")
        targets = self.all_designs_under(d) if "-hierarchy" in o else [d]
        n = 0
        for x in targets:
            for nm, w in x.flops:
                n += w if w > 1 else 0
        self.info("Using name rules '%s'." % rule, "UCN-1")
        if "-verbose" in o and n:
            for x in targets:
                for nm, w in x.flops[:2]:
                    if w > 1:
                        self.out("%-12s %-12s %-28s %s" % (x.name, "cell", "%s_reg[0]" % nm, "%s_reg_0_" % nm))
        self.info("Changed %d object names in %d designs (bus bit -> _N_ format)." % (n, len(targets)), "UCN-2")
        return 1

    def c_report_names(self, a):
        self.out("")
        self.out("Information: No name rule violations found. (simulated)")
        return 1

    # ---------------- 리포트 추가 ----------------
    def _op_lines(self, m, pat):
        res = []
        for k, line in enumerate(strip_comments(m.body).splitlines()):
            code = re.sub(r"\[[^\]]*\]", "", line)
            code = re.sub(r"\d*'[sS]?[bdhoBDHO][0-9a-fA-F_xzXZ]+", "0", code)
            if re.search(pat, code) and not re.search(r"\b(input|output|wire|reg|parameter)\b", line):
                res.append(m.line + k)
        return res

    def c_report_resources(self, a):
        d = self.need_design()
        o, pos = self.opts("report_resources", a, flags=["-hierarchy", "-nosplit"])
        self.ensure_stats(d)
        self.header("resources")
        designs = self.all_designs_under(d) if "-hierarchy" in o or True else [d]
        for x in designs:
            m = x.mod
            w = min(x.datawidth, 64)
            rows = []
            for kind, pat, mod, implu, implc in (
                    ("add", r"(?<![+])\+(?![+:])", "DW01_add", "pparch (area,speed)", "rpl"),
                    ("sub", r"(?<![-])-(?![->:])", "DW01_sub", "pparch (area,speed)", "rpl"),
                    ("mult", r"(?<![*@(])\*(?![*)])", "DW02_mult", "pparch (area,speed)", "csa"),
                    ("gt", r"(?<![>=-])>(?![>=])", "DW01_cmp2", "apparch (area)", "rpl"),
                    ("eq", r"==", "DW01_cmp6", "apparch (area)", "rpl")):
                for k, ln in enumerate(self._op_lines(m, pat)[:6]):
                    impl = implu if x.effort == "ultra" else implc
                    rows.append(("r%d" % (100 + len(rows) * 7 + ln), mod, "width=%d" % (w if kind != "mult" else min(w, 32)),
                                 "%s_%d" % (kind, ln), impl, "%s_x_%d" % (kind, k + 1)))
            self.out("Resource Sharing Report for design %s in file" % x.name)
            self.out("        %s" % m.path)
            self.out("")
            bar = "=" * 79
            self.out(bar)
            self.out("|          |              |            | Contained     |                      |")
            self.out("| Resource | Module       | Parameters | Resources     | Contained Operations |")
            self.out(bar)
            for r, mod, par, op, impl, cell in rows:
                self.out("| %-8s | %-12s | %-10s |               | %-20s |" % (r, mod, par, op))
            if not rows:
                self.out("| (no arithmetic resources)                                                   |")
            self.out(bar)
            self.out("")
            if rows:
                self.out("")
                self.out("Implementation Report")
                self.out(bar)
                self.out("|                    |                  | Current             | Set           |")
                self.out("| Cell               | Module           | Implementation      | Implementation|")
                self.out(bar)
                for r, mod, par, op, impl, cell in rows:
                    self.out("| %-18s | %-16s | %-19s |               |" % (cell, mod, impl if x.mapped else "(not mapped)"))
                self.out(bar)
                self.out("")
        return 1

    def c_report_net(self, a):
        d = self.need_design()
        o, pos = self.opts("report_net", a, flags=["-connections", "-verbose", "-nosplit"])
        self.ensure_stats(d)
        self.header("net", ["-connections"] if "-connections" in o else [])
        rng = random.Random(stable_seed(d.name, "nets"))
        self.out("Operating Conditions: %s   Library: %s" % (self.stats(d)["lib"]["oc"], self.stats(d)["lib"]["name"]))
        self.out("")
        self.out("%-24s %6s %6s %8s %10s %5s  %s" % ("Net", "Fanout", "Fanin", "Load", "Resistance", "Pins", "Attributes"))
        self.out("-" * 80)
        nets = [p[1] for p in d.ports] + [s for s in d.mod.sigs if not d.port(s)]
        clk_ports = set(p for c in d.clocks.values() for p in c["src"])
        for n in nets:
            fo = len(self.all_flop_bits(d)) if n in clk_ports else rng.randint(1, 8)
            load = d.loads.get(n, 0.0) + fo * 0.0015
            self.out("%-24s %6d %6d %8.4f %10.2f %5d  %s" % (n, fo, 1, load, 0.0, fo + 1,
                                                            "d, i" if n in clk_ports else ""))
            if "-connections" in o:
                p = d.port(n)
                if p:
                    self.out("    %s %s (port)" % ("Driver:" if p[0] == "input" else "Load:  ", n))
                for inst, typ, pov, ppos, conn in [(t[1], t[0], t[2], t[3], t[4]) for t in d.mod.instances]:
                    for pm in re.finditer(r"\.\s*(\w+)\s*\(\s*%s\b" % re.escape(n), conn):
                        self.out("    Load:   %s/%s (%s)" % (inst, pm.group(1), typ))
        self.out("-" * 80)
        self.out("Total %d nets" % len(nets))
        self.out("")
        return 1

    # ---------------- 속성 / collection ----------------
    def _kind_of(self, obj, n, d):
        if isinstance(obj, Coll):
            return obj.kind
        if n in self.designs:
            return "design"
        if d and d.port(n):
            return "port"
        if d and n in d.clocks:
            return "clock"
        if "/" in n and n.split("/")[-1] in CELLS:
            return "lib_cell"
        t = self._cur_tech()
        if "/" in n and t and n.split("/")[-1] in t.cells:
            return "lib_cell"
        return "cell"

    def cell_ref(self, d, name):
        for inst, c in getattr(d, "subs_eff", d.subs):
            if inst == name:
                return c, True
        bits = d.flop_bits()
        st = self.stats(d) or {}
        lib = st.get("lib") or LIBS["edu_lib_tt.db"]
        if name in bits or name.endswith("_reg") or re.search(r"_reg(_\d+_)?$", name):
            k = "DFFARX1" if d.mod.async_reset else "DFFX1"
            return (self.ci(k, lib)[5] if d.mapped else "**SEQGEN**"), False
        m = re.match(r"U(\d+)$", name)
        pool = [k for k, v in (st.get("cells") or {}).items() if CELLS[k][2] != "seq" for _ in range(min(v, 50))]
        if m and pool:
            k = pool[int(m.group(1)) % len(pool)]
            return (self.ci(k, lib)[5] if d.mapped else GTECH.get(k, k)), False
        return None, False

    def attr_value(self, kind, n, attr, d):
        key = (kind, n, attr)
        if key in self.attrs:
            return self.attrs[key]
        if attr in ("name", "full_name"):
            return n
        if attr == "object_class":
            return kind
        if kind == "design":
            x = self.designs[n]
            self.ensure_stats(x)
            t = self.totals(x)
            vals = {"area": round(t["comb_area"] + t["seq_area"], 6), "is_mapped": "true" if x.mapped else "false",
                    "max_area": x.max_area if x.max_area is not None else "", "dont_touch": "true" if x.dont_touch else "false",
                    "is_hierarchical": "true" if x.subs else "false"}
            if attr in vals:
                return vals[attr]
        if kind == "port":
            p = d.port(n.split("[")[0])
            if p:
                vals = {"direction": {"input": "in", "output": "out"}.get(p[0], p[0]), "port_direction": p[0],
                        "load": d.loads.get(p[1], 0.0), "driving_cell_rise": d.driving.get(p[1], "")}
                if attr in vals:
                    return vals[attr]
        if kind == "clock":
            c = d.clocks.get(n)
            if c:
                vals = {"period": c["period"], "waveform": " ".join(self.to_str(w) for w in c["wave"]),
                        "sources": " ".join(c["src"]), "is_generated": "true" if c.get("gen") else "false"}
                if attr in vals:
                    return vals[attr]
        if kind == "cell":
            ref, hier = self.cell_ref(d, n)
            if ref:
                t = self._cur_tech()
                if hier:
                    ar = self.totals(self.designs[ref])["comb_area"] + self.totals(self.designs[ref])["seq_area"]
                elif t and ref in t.cells:
                    ar = t.cells[ref].area
                else:
                    ar = CELLS[ref][0] if ref in CELLS else 0.0
                seqc = (t and ref in t.cells and t.cells[ref].kind() == "seq") or ref in ("DFFX1", "DFFARX1", "**SEQGEN**")
                vals = {"ref_name": ref, "is_hierarchical": "true" if hier else "false",
                        "is_sequential": "true" if seqc else "false", "area": ar}
                if attr in vals:
                    return vals[attr]
        if kind == "lib_cell":
            c = n.split("/")[-1]
            t = self._cur_tech()
            if t and c in t.cells:
                tc = t.cells[c]
                vals = {"area": tc.area, "dont_use": "true" if (c in self.dont_use or tc.dont_use) else "false",
                        "dont_touch": "true" if tc.dont_touch else "false",
                        "is_sequential": "true" if tc.kind() == "seq" else "false",
                        "cell_leakage_power": tc.leak, "cell_footprint": tc.footprint,
                        "function": " ".join("%s=%s" % (p, tc.pins[p].func) for p in tc.outputs if tc.pins[p].func)}
                if attr in vals:
                    return vals[attr]
            if c in CELLS:
                vals = {"area": CELLS[c][0], "dont_use": "true" if c in self.dont_use else "false",
                        "dont_touch": "false", "is_sequential": "true" if CELLS[c][2] == "seq" else "false"}
                if attr in vals:
                    return vals[attr]
        raise DCError("Attribute '%s' does not exist on %s '%s'." % (attr, kind, n), "UID-101",
                      hint="예: get_attribute [current_design] area / [get_cells u_alu] ref_name / [get_clocks clk] period")

    def c_get_attribute(self, a):
        o, pos = self.opts("get_attribute", a, flags=["-quiet"], valued=["-class"])
        if len(pos) < 2:
            raise DCError("wrong # args: should be \"get_attribute object attr\"", "CMD-012")
        d = self.designs.get(self.cur)
        obj, attr = pos[0], self.to_str(pos[1])
        names = self.names_of(obj)
        if self.to_str(obj) in self.designs and not isinstance(obj, Coll):
            names = [self.to_str(obj)]
        res = []
        for n in names:
            kind = self.to_str(o["-class"]) if "-class" in o else self._kind_of(obj, n, d)
            try:
                res.append(self.attr_value(kind, n, attr, d))
            except DCError:
                if "-quiet" in o:
                    continue
                raise
        if len(res) == 1:
            return res[0]
        return self.list_str(res)

    def c_set_attribute(self, a):
        o, pos = self.opts("set_attribute", a, flags=["-quiet"], valued=["-type", "-class"])
        if len(pos) < 3:
            raise DCError("wrong # args: should be \"set_attribute objects attr value\"", "CMD-012")
        attr, val = self.to_str(pos[1]), self.to_str(pos[2])
        d = self.designs.get(self.cur)
        for n in self.names_of(pos[0]):
            kind = self._kind_of(pos[0], n, d)
            if attr == "dont_use" and kind == "lib_cell":
                c = n.split("/")[-1]
                (self.dont_use.add if val == "true" else self.dont_use.discard)(c)
            elif attr == "dont_touch" and n in self.designs:
                self.designs[n].dont_touch = val == "true"
            else:
                self.attrs[(kind, n, attr)] = val
        return Coll(pos[0].kind if isinstance(pos[0], Coll) else "design", self.names_of(pos[0]))

    def _filter(self, coll, expr):
        d = self.designs.get(self.cur)
        kind = coll.kind if isinstance(coll, Coll) else "port"
        expr = self.to_str(expr)
        parts = re.split(r"\s+(AND|OR|&&|\|\|)\s+", expr)
        res = []
        for n in self.names_of(coll):
            val = None
            op_next = None
            for p in parts:
                if p in ("AND", "OR", "&&", "||"):
                    op_next = "and" if p in ("AND", "&&") else "or"
                    continue
                m = re.match(r"\s*@?(\w+)\s*(==|!=|=~|!~|<=|>=|<|>)\s*\"?([^\"]*)\"?\s*$", p)
                if not m:
                    raise DCError("Invalid filter expression '%s'." % expr, "SEL-002")
                at, op, v = m.groups()
                try:
                    actual = self.to_str(self.attr_value(kind, n, at, d))
                except DCError:
                    actual = ""
                if op == "==":
                    r = actual == v
                elif op == "!=":
                    r = actual != v
                elif op == "=~":
                    r = fnmatch.fnmatchcase(actual, v)
                elif op == "!~":
                    r = not fnmatch.fnmatchcase(actual, v)
                else:
                    try:
                        r = eval("%f %s %f" % (float(actual), op, float(v)))
                    except ValueError:
                        r = False
                val = r if val is None else ((val and r) if op_next == "and" else (val or r))
            if val:
                res.append(n)
        return Coll(kind, res) if res else ""

    def c_filter_collection(self, a):
        o, pos = self.opts("filter_collection", a, flags=["-regexp", "-nocase"])
        if len(pos) < 2:
            raise DCError("wrong # args: should be \"filter_collection collection expression\"", "CMD-012")
        return self._filter(pos[0], pos[1])

    def c_get_ports(self, a):
        flt = None
        args = list(a)
        if "-filter" in [self.to_str(x) for x in args]:
            k = [self.to_str(x) for x in args].index("-filter")
            flt = self.to_str(args[k + 1]) if k + 1 < len(args) else None
            del args[k:k + 2]
        r = ShellCore.c_get_ports(self, args)
        return self._filter(r, flt) if (flt and r != "") else r

    def c_get_cells(self, a):
        flt = None
        args = list(a)
        if "-filter" in [self.to_str(x) for x in args]:
            k = [self.to_str(x) for x in args].index("-filter")
            flt = self.to_str(args[k + 1]) if k + 1 < len(args) else None
            del args[k:k + 2]
        r = ShellCore.c_get_cells(self, args)
        return self._filter(r, flt) if (flt and r != "") else r

    def c_sort_collection(self, a):
        o, pos = self.opts("sort_collection", a, flags=["-descending", "-dictionary"])
        coll = pos[0]
        attr = self.to_str(pos[1]) if len(pos) > 1 else "name"
        d = self.designs.get(self.cur)
        kind = coll.kind if isinstance(coll, Coll) else "port"

        def key(n):
            try:
                v = self.attr_value(kind, n, attr, d)
                return (0, float(v)) if re.match(r"^-?[\d.]+$", self.to_str(v)) else (1, self.to_str(v))
            except DCError:
                return (2, n)
        names = sorted(self.names_of(coll), key=key, reverse="-descending" in o)
        return Coll(kind, names)

    def c_index_collection(self, a):
        names = self.names_of(a[0])
        i = int(self.fnum(a[1])) if len(a) > 1 else 0
        if not 0 <= i < len(names):
            raise DCError("Index %d out of range." % i, "SEL-005")
        return Coll(a[0].kind if isinstance(a[0], Coll) else "port", [names[i]])

    def c_copy_collection(self, a):
        return Coll(a[0].kind if isinstance(a[0], Coll) else "port", self.names_of(a[0])) if a else ""

    def c_append_to_collection(self, a):
        o, pos = self.opts("append_to_collection", a, flags=["-unique"])
        var = self.to_str(pos[0])
        cur = self.vars.get(var, "")
        add = pos[1] if len(pos) > 1 else ""
        kind = add.kind if isinstance(add, Coll) else (cur.kind if isinstance(cur, Coll) else "port")
        names = self.names_of(cur) + self.names_of(add)
        if "-unique" in o:
            names = list(OrderedDict.fromkeys(names))
        self.vars[var] = Coll(kind, names)
        return self.vars[var]

    def c_get_pins(self, a):
        d = self.need_design()
        o, pos = self.opts("get_pins", a, flags=["-hierarchical", "-quiet", "-leaf"], valued=["-filter", "-of_objects"])
        pats = []
        for p in pos:
            pats += self.names_of(p)
        pats = pats or ["*"]
        cands = []
        for inst, c in getattr(d, "subs_eff", d.subs):
            cd = self.designs.get(c)
            if cd:
                cands += ["%s/%s" % (inst, pn) for _, pn, _ in cd.ports]
        for b, x in self.all_flop_bits(d):
            for pin in (["D", "CLK", "Q"] + (["RSTB"] if x.mod.async_reset else [])):
                cands.append("%s/%s" % (b, pin))
        res = [c for c in cands if any(fnmatch.fnmatchcase(c, p) or fnmatch.fnmatchcase(c, p.replace("[", "_").replace("]", "_"))
                                       for p in pats)]
        if not res:
            if "-quiet" not in o:
                self.warn("No pin objects matched '%s'" % " ".join(pats), "SEL-004")
            return ""
        return Coll("pin", res)

    def c_all_fanin(self, a):
        d = self.need_design()
        o, pos = self.opts("all_fanin", a, flags=["-flat", "-startpoints_only", "-only_cells"], valued=["-to", "-levels"])
        if "-to" not in o:
            raise DCError("Required argument '-to' was not found", "CMD-007")
        tgt = self.names_of(o["-to"])
        self.ensure_stats(d)
        res = []
        for t in tgt:
            pre = t.rsplit("/", 2)[0] + "/" if t.count("/") >= 2 else ""
            res += [b for b, _ in self.all_flop_bits(d) if b.startswith(pre) and (pre or "/" not in b)][:16]
            if not pre:
                clk = set(p for c in d.clocks.values() for p in c["src"])
                res += [p[1] for p in d.ports if p[0] == "input" and p[1] not in clk]
        if "-startpoints_only" not in o:
            res = res + tgt
        return Coll("pin", list(OrderedDict.fromkeys(res))) if res else ""

    def c_all_fanout(self, a):
        d = self.need_design()
        o, pos = self.opts("all_fanout", a, flags=["-flat", "-endpoints_only", "-only_cells", "-clock_tree"],
                           valued=["-from", "-levels"])
        self.ensure_stats(d)
        if "-clock_tree" in o:
            return Coll("pin", [b + "/CLK" for b, _ in self.all_flop_bits(d)])
        if "-from" not in o:
            raise DCError("Required argument '-from' was not found", "CMD-007")
        res = []
        for s in self.names_of(o["-from"]):
            base = s.split("[")[0]
            for typ, inst, pov, ppos, conn in d.mod.instances:
                if re.search(r"\b%s\b" % re.escape(base), conn):
                    res += [b for b, _ in self.all_flop_bits(d) if b.startswith(inst + "/")]
            if d.mod.uses(base) > 1:
                res += [b for b in d.flop_bits()]
                res += [p[1] for p in d.ports if p[0] == "output"][:4]
        if "-endpoints_only" not in o:
            res = self.names_of(o["-from"]) + res
        return Coll("pin", list(OrderedDict.fromkeys(res))) if res else ""

    def c_report_clock(self, a):
        r = ShellCore.c_report_clock(self, a)
        d = self.need_design()
        gens = [(n, c) for n, c in d.clocks.items() if c.get("gen")]
        if gens:
            self.out("Generated     Master         Generated      Master         Waveform")
            self.out("Clock         Source         Source         Clock          Modification")
            self.out("-" * 80)
            for n, c in gens:
                self.out("%-13s %-14s %-14s %-14s div/mul (period %.2f)" % (
                    n, " ".join(d.clocks[c["gen"]]["src"]), " ".join(c["src"]), c["gen"], c["period"]))
            self.out("-" * 80)
            self.out("")
        return r


    # ---------------- 탭 자동완성 ----------------
    def read_input(self, prompt):
        try:
            import lineedit
            if lineedit.available():
                return lineedit.read_line(prompt, self.complete, self.history)
        except ImportError:
            pass
        return input(prompt)

    def _cmd_options(self, cmd):
        opts = set()
        if cmd in HELP:
            opts |= set(re.findall(r"(?<![\w])-[A-Za-z_]\w*", HELP[cmd][2]))
        extra = {
            "report_timing": "-max_paths -nworst -delay_type -path -from -to -through -nets -transition_time "
                             "-capacitance -input_pins -significant_digits -nosplit -group",
            "compile_ultra": "-no_autoungroup -retime -gate_clock -incremental -timing_high_effort_script "
                             "-area_high_effort_script -exact_map -no_boundary_optimization",
            "compile": "-map_effort -area_effort -incremental_mapping -exact_map -ungroup_all -gate_clock",
            "create_clock": "-period -name -waveform -add", "write": "-format -hierarchy -output",
            "set_input_delay": "-clock -max -min -add_delay", "set_output_delay": "-clock -max -min -add_delay",
            "analyze": "-format -library -autoread -recursive -define", "elaborate": "-parameters -architecture -library",
            "report_area": "-hierarchy -nosplit", "report_power": "-hierarchy -nosplit -verbose",
            "read_file": "-format -autoread -recursive -top", "get_ports": "-filter -quiet -regexp",
            "get_cells": "-hierarchical -filter -quiet", "report_constraint": "-all_violators -verbose -max_delay",
            "read_verilog": "-r -i -libname -netlist", "read_db": "-technology_library -r -i",
            "report_analysis_coverage": "-status_details -check_type", "set_driving_cell": "-lib_cell -pin -library",
        }
        opts |= set(extra.get(cmd, "").split())
        return sorted(opts)

    def complete(self, line, b, e, word):
        before = line[:b]
        k = max(before.rfind("["), before.rfind(";"), before.rfind("{"))
        seg = before[k + 1:] if k >= 0 else before
        toks = seg.split()
        if word.startswith("$"):
            pre = word[1:].lstrip("{")
            return ["$" + v for v in self.vars if v.startswith(pre)]
        if not toks:
            names = set(self.cmds) | set(self.procs)
            return [n for n in names if n.startswith(word)]
        cmd = toks[0]
        if word.startswith("-"):
            return [o for o in self._cmd_options(cmd) if o.startswith(word)]
        if cmd in ("set_app_var", "get_app_var", "report_app_var", "printvar") and len(toks) == 1:
            return [v for v in self.app_vars if v.startswith(word)]
        if cmd in ("set", "unset") and len(toks) == 1:
            return [v for v in self.vars if v.startswith(word)]
        if cmd in ("man", "help"):
            return [n for n in self.cmds if n.startswith(word)]
        d = self.designs.get(self.cur) if self.cur else None
        if cmd == "get_ports" and d:
            return [p[1] for p in d.ports if p[1].startswith(word)]
        if cmd == "get_clocks" and d:
            return [c for c in d.clocks if c.startswith(word)]
        if cmd in ("current_design", "get_designs", "remove_design"):
            return [n for n in self.designs if n.startswith(word)]
        if cmd == "elaborate":
            return [n for n in self.modules if n.startswith(word)]
        if cmd in ("get_cells", "set_ungroup", "ungroup") and d:
            return [i for i, _ in d.subs if i.startswith(word)]
        if cmd in ("get_lib_cells", "set_dont_use", "set_driving_cell"):
            t = self._cur_tech()
            if t and "/" in word:
                lp, pre = word.split("/", 1)
                return ["%s/%s" % (lp, c) for c in t.cells if c.startswith(pre)][:400]
            if t and cmd == "set_driving_cell":
                return [c for c in t.cells if c.startswith(word)][:400]
        if self.term is not None:
            return self.term.complete_path(word, cwd=self.cwd)
        return []

    # ---------------- 실제 liberty 라이브러리 명령 ----------------
    def _discover_libs(self):
        res = []
        try:
            for nm in self.fs.listdir("/tools/lib"):
                d = "/tools/lib/" + nm + "/db"
                if self.fs.isdir(d):
                    for f in self.fs.listdir(d):
                        if f.endswith(".db"):
                            res.append((nm, d + "/" + f))
        except Exception:
            pass
        return res

    def c_list_libs(self, a):
        try:
            cur = self.lib()
        except DCError:
            cur = None
        self.out("")
        self.out("Library                              File                                  Path")
        self.out("-------                              ----                                  ----")
        for f, l in LIBS.items():
            mark = "*" if cur and cur.get("name") == l["name"] else " "
            self.out("%s%-36s %-37s %s" % (mark, l["name"], f, "/tools/lib/edu_lib/db  (가상 교육용)"))
        for nm, p in self._discover_libs():
            f = p.rsplit("/", 1)[1]
            if f in LIBS:
                continue
            base = f[:-3]
            mark = "*" if cur and cur.get("file", "").endswith("/" + base + ".lib") else " "
            self.out("%s%-36s %-37s %s" % (mark, base, f, p.rsplit("/", 1)[0]))
        self.out("gtech                                gtech.db                              %s/libraries/syn" % SYN_ROOT)
        self.out("")
        self.out("  (* = 현재 target_library)   예) set_app_var search_path \"$search_path /tools/lib/sky130_fd_sc_hd/db\"")
        self.out("                                  set_app_var target_library sky130_fd_sc_hd__tt_025C_1v80.db")
        return 1

    def c_report_lib(self, a):
        o, pos = self.opts("report_lib", a, flags=["-all", "-timing", "-table"])
        name = self.to_str(pos[0]) if pos else ""
        pat = self.to_str(pos[1]) if len(pos) > 1 else "*"
        try:
            cur = self.lib()
        except DCError:
            cur = None
        lib = None
        if cur and cur.get("tech") and name in (cur["name"], "", os.path.basename(cur.get("file", "")),
                                                 os.path.basename(cur.get("file", ""))[:-4]):
            lib = cur
        if lib is None:
            for nm, p in self._discover_libs():
                base = p.rsplit("/", 1)[1][:-3]
                if name in (base, base + ".db"):
                    lp = self._liberty_for(p)
                    if lp:
                        lib = self.libdict(lp)
        if lib is None:
            return ShellCore.c_report_lib(self, [name])
        t = lib["tech"]
        self.out("")
        self.out("*" * 40)
        self.out("Report : library")
        self.out("Library: %s" % t.name)
        self.out("Source : %s" % lib["file"])
        self.out("Version: %s" % REPORT_VERSION)
        self.out("Date   : %s" % dc_date())
        self.out("*" * 40)
        self.out("")
        self.out("Time Unit          : %gns" % t.time_scale)
        self.out("Capacitive Unit    : %gpF" % t.cap_scale)
        self.out("Leakage Power Unit : %s" % t.leak_unit)
        self.out("Nominal Process    : %g" % t.process)
        self.out("Nominal Voltage    : %g" % t.volt)
        self.out("Nominal Temperature: %g" % t.temp)
        self.out("Operating Condition: %s" % t.oc)
        if t.wire_loads:
            self.out("Wire Load Models   : %s" % " ".join(t.wire_loads[:8]))
        self.out("")
        self.out("%-34s %10s %12s  %-5s %s" % ("Cell", "Area", "Leakage", "Type", "Function / Pins"))
        self.out("-" * 110)
        n = 0
        for cn in sorted(t.cells):
            if not (fnmatch.fnmatchcase(cn, pat) or fnmatch.fnmatchcase(cn.split("__")[-1], pat)):
                continue
            c = t.cells[cn]
            fn = "; ".join("%s=%s" % (p, c.pins[p].func) for p in c.outputs if c.pins[p].func)
            if c.ff:
                fn = "ff(clk=%s, D=%s%s)" % (c.ff.get("clocked_on"), c.ff.get("next_state"),
                                             (", clr=%s" % c.ff["clear"]) if c.ff.get("clear") else "")
            elif c.latch:
                fn = "latch(en=%s)" % c.latch.get("enable")
            attrs = (" [dont_use]" if (c.dont_use or cn in self.dont_use) else "") + (" [ICG]" if c.icg else "")
            self.out("%-34s %10.3f %12.5g  %-5s %s%s" % (cn, c.area, c.leak, c.kind(), fn[:60], attrs))
            n += 1
        self.out("-" * 110)
        self.out("%d cells" % n)
        if "-all" in o or pat == "*":
            self.out("")
            self.out("dc_shell_sim cell mapping (function-matched):")
            for k in CELLS:
                ci = lib["cmap"].get(k)
                self.out("  %-8s -> %-32s %s" % (k, ci[5] if ci else "(none: decomposed)",
                                                 ("delay@FO3 %.3fns  area %.3f" % (ci[1], ci[0])) if ci else ""))
            self.out("  %-8s -> %s" % ("ICG", lib.get("icg") or "(none)"))
            self.out("  setup %.3fns  hold %.3fns  (from %s)" % (lib["setup"] or 0, lib["hold"] or 0,
                                                              (lib["cmap"].get("DFFARX1") or lib["cmap"].get("DFFX1") or [""] * 6)[5]))
        self.out("")
        return 1

    def c_get_lib_cells(self, a):
        t = self._cur_tech()
        if not t:
            return ShellCore.c_get_lib_cells(self, a)
        o, pos = self.opts("get_lib_cells", a, flags=["-quiet", "-regexp"], valued=["-filter", "-of_objects"])
        pats = []
        for p in pos:
            pats += self.names_of(p)
        pats = pats or ["*/*"]
        res = []
        for cn in sorted(t.cells):
            full = "%s/%s" % (t.name, cn)
            if any(fnmatch.fnmatchcase(full, p) or fnmatch.fnmatchcase(cn, p.split("/")[-1]) for p in pats):
                res.append(full)
        if not res:
            if "-quiet" not in o:
                self.warn("No lib_cell objects matched '%s'" % " ".join(pats), "SEL-004")
            return ""
        coll = Coll("lib_cell", res)
        if "-filter" in o:
            return self._filter(coll, o["-filter"])
        return coll

    def c_get_libs(self, a):
        t = self._cur_tech()
        if t:
            return Coll("lib", [t.name])
        return ShellCore.c_get_libs(self, a)

    def c_set_driving_cell(self, a):
        t = self._cur_tech()
        if not t:
            return ShellCore.c_set_driving_cell(self, a)
        d = self.need_design()
        o, pos = self.opts("set_driving_cell", a, flags=["-no_design_rule", "-max", "-min", "-dont_scale"],
                           valued=["-lib_cell", "-library", "-pin", "-from_pin", "-input_transition_rise",
                                   "-input_transition_fall"])
        cell = self.to_str(o.get("-lib_cell", "")).split("/")[-1]
        if cell not in t.cells:
            raise DCError("Can't find lib_cell '%s' in library '%s'." % (cell, t.name), "UID-993",
                          hint="get_lib_cells */*buf* 로 셀 이름을 확인하세요.")
        ports = self.ports_from(d, pos)
        for p in ports:
            d.driving[p] = cell
        self.sdc(d, "set_driving_cell -lib_cell %s [get_ports {%s}]" % (cell, " ".join(ports)))
        return 1

    def _check_tech_library(self, lib):
        t = lib["tech"]
        self.out("")
        self.out("#BEGIN_LIBSCREEN_INFO")
        self.out("")
        self.out("Logic Library:   %s" % t.name)
        self.out("  Library file : %s" % lib["file"])
        kinds = Counter(c.kind() for c in t.cells.values())
        self.out("  Cells        : %d (comb %d, seq %d, icg %d)" % (len(t.cells), kinds["comb"], kinds["seq"], kinds["icg"]))
        self.out("  dont_use     : %d cells" % sum(1 for c in t.cells.values() if c.dont_use))
        self.out("  Units        : time %gns, cap %gpF, leakage %s" % (t.time_scale, t.cap_scale, t.leak_unit))
        self.out("  Op condition : %s (%.2fV, %gC)" % (t.oc, t.volt, t.temp))
        self.out("")
        self.out("#END_LIBSCREEN_INFO")
        miss = [k for k in CELLS if not lib["cmap"].get(k)]
        no_area = [c.name for c in t.cells.values() if c.area <= 0 and c.kind() != "icg"]
        self.out("")
        if no_area:
            self.warn("%d cells have no area attribute (e.g. %s)." % (len(no_area), " ".join(no_area[:3])), "LIBCHK-212")
        if miss:
            self.warn("No library cell matches simulator function(s): %s (will be decomposed)." % " ".join(miss), "LIBCHK-300")
        self.out("Check Summary : 0 Errors, %d Warnings" % (bool(no_area) + bool(miss)))
        return 1

class Shell(DCExtra, ShellCore):
    pass


def main(argv):
    global USE_COLOR
    import vterm
    color = True
    for a in argv:
        if a in ("-no_color", "--no-color"):
            color = False
    if os.name == "nt":
        os.system("")
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stdin.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if not sys.stdout.isatty():
        color = False
    USE_COLOR = color
    global HL
    HL = vterm.highlight
    sim_root = os.path.dirname(os.path.abspath(__file__))
    vterm.run_terminal(sys.modules[__name__], sim_root, color)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
