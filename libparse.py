# -*- coding: utf-8 -*-
"""
libparse.py  -  dc_shell_sim 용 Liberty(.lib) 파서

  * 셀 면적, 핀(방향/용량/function/clock), ff/latch/ICG, NLDM 지연·트랜지션 테이블,
    setup/hold 테이블, cell_leakage_power, 단위, 동작 조건을 읽습니다.
  * 셀 function 의 진리표를 계산해서 시뮬레이터 내부의 일반 셀 역할(INV, NAND2, FA, DFF ...)에
    실제 라이브러리 셀을 자동으로 대응시킵니다. (sky130, Nangate45, SAED 등 일반적인 .lib 지원)
  * 파싱 결과는 pickle 로 캐시합니다.
"""
import os
import re
import bisect
import pickle
import itertools

SKIP_GROUPS = {"internal_power", "ccsn_first_stage", "ccsn_last_stage", "output_current_rise", "output_current_fall",
               "receiver_capacitance", "receiver_capacitance1_rise", "receiver_capacitance1_fall",
               "receiver_capacitance2_rise", "receiver_capacitance2_fall", "leakage_power", "dynamic_current",
               "normalized_driver_waveform", "output_ccb", "input_ccb", "pg_pin", "power_lut_template",
               "output_current_template", "noise_lut_template", "propagated_noise_high", "propagated_noise_low",
               "steady_state_current_high", "steady_state_current_low", "steady_state_current_tristate",
               "noise_immunity_above_high", "noise_immunity_below_low", "noise_immunity_high", "noise_immunity_low",
               "intrinsic_parasitic", "ocv_sigma_cell_rise", "ocv_sigma_cell_fall", "ocv_sigma_rise_transition",
               "ocv_sigma_fall_transition", "ocv_sigma_rise_constraint", "ocv_sigma_fall_constraint",
               "dc_current", "pg_current", "user_parameters", "test_cell", "statetable", "bundle", "lut_values",
               "base_curves", "compact_lut_template", "wire_load_selection", "power_supply", "voltage_map",
               "retention_condition", "char_config", "leakage_current", "gate_leakage", "pin_based_power"}

TOK = re.compile(r'"(?:\\.|[^"\\])*"|[(){}:;,]|[^\s(){}:;,"]+')


class Group:
    __slots__ = ("type", "args", "attrs", "subs")

    def __init__(self, typ, args):
        self.type = typ
        self.args = args
        self.attrs = {}
        self.subs = []

    def get(self, k, d=None):
        return self.attrs.get(k, d)

    def groups(self, typ):
        return [g for g in self.subs if g.type == typ]


def _unq(s):
    return s[1:-1] if len(s) >= 2 and s[0] == '"' and s[-1] == '"' else s


def parse_liberty_text(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    toks = TOK.findall(text)
    n = len(toks)
    root = Group("root", [])
    stack = [root]
    i = 0
    while i < n:
        t = toks[i]
        if t == "}":
            if len(stack) > 1:
                stack.pop()
            i += 1
            continue
        if t in (";", ","):
            i += 1
            continue
        nxt = toks[i + 1] if i + 1 < n else ""
        if nxt == ":":
            j = i + 2
            val = []
            while j < n and toks[j] not in (";", "}"):
                val.append(toks[j])
                j += 1
            stack[-1].attrs[t] = _unq(" ".join(val)) if len(val) != 1 else _unq(val[0])
            i = j + 1 if j < n and toks[j] == ";" else j
            continue
        if nxt == "(":
            j = i + 2
            args = []
            while j < n and toks[j] != ")":
                if toks[j] != ",":
                    args.append(_unq(toks[j]))
                j += 1
            j += 1
            if j < n and toks[j] == "{":
                if t in SKIP_GROUPS:
                    depth = 1
                    j += 1
                    while j < n and depth:
                        if toks[j] == "{":
                            depth += 1
                        elif toks[j] == "}":
                            depth -= 1
                        j += 1
                    i = j
                    continue
                g = Group(t, args)
                stack[-1].subs.append(g)
                stack.append(g)
                i = j + 1
                continue
            # complex attribute
            if t in stack[-1].attrs and t in ("values",):
                stack[-1].attrs[t] = stack[-1].attrs[t] + args
            else:
                stack[-1].attrs[t] = args
            i = j + 1 if j < n and toks[j] == ";" else j
            continue
        i += 1
    libs = root.groups("library")
    return libs[0] if libs else None


# --------------------------------------------------------------------------
# 테이블
# --------------------------------------------------------------------------
def _floats(x):
    if isinstance(x, list):
        out = []
        for s in x:
            out += [float(v) for v in re.split(r"[\s,]+", s.strip()) if v]
        return out
    return [float(v) for v in re.split(r"[\s,]+", str(x).strip()) if v]


class Table:
    __slots__ = ("i1", "i2", "vals", "v1", "v2")

    def __init__(self, g, templates):
        tmpl = templates.get(g.args[0] if g.args else "", ("", ""))
        self.v1, self.v2 = tmpl
        self.i1 = _floats(g.get("index_1", [])) if "index_1" in g.attrs else []
        self.i2 = _floats(g.get("index_2", [])) if "index_2" in g.attrs else []
        rows = g.get("values", [])
        if not isinstance(rows, list):
            rows = [rows]
        self.vals = [_floats(r) for r in rows] if rows else [[0.0]]
        if not self.i1 and self.vals and len(self.vals[0]) > 1:
            self.i1 = list(range(len(self.vals[0])))
        if len(self.vals) == 1 and self.i2 and len(self.vals[0]) == len(self.i1) * len(self.i2):
            flat = self.vals[0]
            self.vals = [flat[k * len(self.i2):(k + 1) * len(self.i2)] for k in range(len(self.i1))]

    @staticmethod
    def _lerp1(xs, ys, x):
        if len(xs) <= 1 or len(ys) <= 1:
            return ys[0] if ys else 0.0
        k = bisect.bisect_left(xs, x)
        k = min(max(k, 1), len(xs) - 1)
        x0, x1 = xs[k - 1], xs[k]
        y0, y1 = ys[k - 1], ys[k]
        if x1 == x0:
            return y0
        return y0 + (y1 - y0) * (x - x0) / (x1 - x0)

    def lookup(self, tran, load):
        """tran: 입력 트랜지션, load: 출력 부하 (템플릿 변수 순서를 따름)"""
        a, b = tran, load
        if "capacitance" in self.v1 or "constrained_pin_transition" in self.v2:
            a, b = load, tran
        if "related_pin_transition" in self.v1 and "constrained" in self.v2:
            a, b = tran, load
        if len(self.vals) == 1:
            row = self.vals[0]
            xs = self.i1 if len(self.i1) == len(row) else self.i2
            return self._lerp1(xs, row, a if self.i1 and len(self.i1) == len(row) else b)
        if not self.i2:
            col = [r[0] for r in self.vals]
            return self._lerp1(self.i1, col, a)
        col = [self._lerp1(self.i2, r, b) for r in self.vals]
        return self._lerp1(self.i1, col, a)


# --------------------------------------------------------------------------
# 셀
# --------------------------------------------------------------------------
class Pin:
    __slots__ = ("name", "dir", "cap", "func", "clock", "max_cap", "max_tran")

    def __init__(self, name):
        self.name = name
        self.dir = "input"
        self.cap = 0.0
        self.func = None
        self.clock = False
        self.max_cap = None
        self.max_tran = None


class Arc:
    __slots__ = ("out", "rel", "type", "sense", "tables")

    def __init__(self, out, rel, typ, sense):
        self.out, self.rel, self.type, self.sense = out, rel, typ, sense
        self.tables = {}


class Cell:
    def __init__(self, name):
        self.name = name
        self.area = 0.0
        self.leak = 0.0
        self.pins = {}
        self.ff = None
        self.latch = None
        self.icg = False
        self.dont_use = False
        self.dont_touch = False
        self.arcs = []
        self.footprint = ""
        self.role = None

    @property
    def inputs(self):
        return [p.name for p in self.pins.values() if p.dir == "input"]

    @property
    def outputs(self):
        return [p.name for p in self.pins.values() if p.dir in ("output", "inout")]

    def kind(self):
        if self.ff or self.latch:
            return "seq"
        if self.icg:
            return "icg"
        return "comb"


class TechLib:
    def __init__(self, path):
        self.path = path
        self.name = ""
        self.time_scale = 1.0       # -> ns
        self.cap_scale = 1.0        # -> pF
        self.leak_scale = 1.0       # -> W
        self.leak_unit = "1nW"
        self.volt = 1.0
        self.temp = 25.0
        self.process = 1.0
        self.oc = ""
        self.templates = {}
        self.cells = {}
        self.default_max_tran = None
        self.wire_loads = []
        self.roles = {}

    # 지연 계산 ---------------------------------------------------------
    def typical_tran(self):
        return 0.08 if self.time_scale >= 1.0 else 0.08

    def arc_delay(self, cell, out=None, tran=0.08, load=None, rel=None):
        c = self.cells[cell]
        if load is None:
            load = self.fanout_load(4)
        best = None
        btran = 0.0
        for a in c.arcs:
            if out and a.out != out:
                continue
            if rel and a.rel != rel:
                continue
            if a.type in ("setup_rising", "setup_falling", "hold_rising", "hold_falling", "min_pulse_width",
                          "recovery_rising", "removal_rising", "recovery_falling", "removal_falling",
                          "minimum_period", "skew_rising", "skew_falling", "non_seq_setup_rising",
                          "non_seq_hold_rising", "clear", "preset", "three_state_enable", "three_state_disable"):
                continue
            ds = [a.tables[k].lookup(tran, load) * self.time_scale for k in ("cell_rise", "cell_fall") if k in a.tables]
            ts = [a.tables[k].lookup(tran, load) * self.time_scale for k in ("rise_transition", "fall_transition")
                  if k in a.tables]
            if not ds:
                continue
            d = max(ds)
            if best is None or d > best:
                best = d
                btran = max(ts) if ts else tran
        return (best or 0.0), btran

    def constraint(self, cell, kind="setup", clk_tran=0.08, data_tran=0.08):
        c = self.cells[cell]
        vals = []
        for a in c.arcs:
            if a.type and a.type.startswith(kind):
                for k in ("rise_constraint", "fall_constraint"):
                    if k in a.tables:
                        t = a.tables[k]
                        if "related_pin_transition" in t.v1:
                            v = t.lookup(clk_tran, data_tran)
                        elif "constrained_pin_transition" in t.v1:
                            v = t.lookup(data_tran, clk_tran)
                        else:
                            v = t.lookup(data_tran, clk_tran)
                        vals.append(v * self.time_scale)
        if not vals:
            return 0.05 if kind == "setup" else 0.02
        return max(vals)

    def avg_input_cap(self):
        caps = []
        for r in ("NAND2X1", "INVX1", "NOR2X1", "AOI21X1", "DFFX1"):
            cn = self.roles.get(r)
            if cn:
                c = self.cells[cn]
                for p in c.inputs:
                    caps.append(c.pins[p].cap * self.cap_scale)
        return (sum(caps) / len(caps)) if caps else 0.002

    def fanout_load(self, fo):
        cap = self.avg_input_cap()
        return (cap * fo * 1.25) / self.cap_scale

    def leak_w(self, cell):
        return self.cells[cell].leak * self.leak_scale


# --------------------------------------------------------------------------
# 진리표 기반 셀 분류
# --------------------------------------------------------------------------
def _bool_eval(expr, env):
    s = expr.replace("\"", "").strip()
    toks = re.findall(r"[A-Za-z_][\w\[\]\.]*|[01]|[!'&*|+^()]", s)
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def take():
        t = toks[pos[0]]
        pos[0] += 1
        return t

    def p_or():
        v = p_xor()
        while peek() in ("|", "+"):
            take()
            v = v | p_xor()
        return v

    def p_xor():
        v = p_and()
        while peek() == "^":
            take()
            v = v ^ p_and()
        return v

    def p_and():
        v = p_not()
        while True:
            t = peek()
            if t in ("&", "*"):
                take()
                v = v & p_not()
            elif t is not None and (t in ("!", "(", "0", "1") or re.match(r"[A-Za-z_]", t)):
                v = v & p_not()
            else:
                break
        return v

    def p_not():
        if peek() == "!":
            take()
            return 1 - p_not()
        v = p_atom()
        while peek() == "'":
            take()
            v = 1 - v
        return v

    def p_atom():
        t = take()
        if t == "(":
            v = p_or()
            if peek() == ")":
                take()
            return v
        if t in ("0", "1"):
            return int(t)
        return env.get(t, 0)
    return p_or() & 1


def truth_table(expr, inputs):
    tt = 0
    for k in range(1 << len(inputs)):
        env = {p: (k >> i) & 1 for i, p in enumerate(inputs)}
        try:
            if _bool_eval(expr, env):
                tt |= 1 << k
        except (IndexError, RecursionError):
            return None
    return tt


def _tt_of(fn, n):
    tt = 0
    for k in range(1 << n):
        bits = [(k >> i) & 1 for i in range(n)]
        if fn(*bits):
            tt |= 1 << k
    return tt


ROLE_FUNCS = {   # role: (n_inputs, function)
    "INVX1": (1, lambda a: not a),
    "BUFX2": (1, lambda a: a),
    "NAND2X1": (2, lambda a, b: not (a and b)),
    "NOR2X1": (2, lambda a, b: not (a or b)),
    "AND2X1": (2, lambda a, b: a and b),
    "OR2X1": (2, lambda a, b: a or b),
    "XOR2X1": (2, lambda a, b: a != b),
    "XNOR2X1": (2, lambda a, b: a == b),
    "AOI21X1": (3, lambda a, b, c: not ((a and b) or c)),
    "OAI21X1": (3, lambda a, b, c: not ((a or b) and c)),
    "AO22X1": (4, lambda a, b, c, d: (a and b) or (c and d)),
    "MUX21X1": (3, lambda a, b, s: b if s else a),
}
ROLE_TT = {r: (n, _tt_of(f, n)) for r, (n, f) in ROLE_FUNCS.items()}
MAJ3 = lambda a, b, c: (a + b + c) >= 2
EXCLUDE = ("dly", "clkbuf", "clkinv", "probe", "lpflow", "lsbuf", "decap", "fill", "tap", "conb", "diode",
           "ebufn", "einv", "tbuf", "antenna", "bufbuf", "clkdlybuf", "macro", "sram", "ram", "dummy", "_lp",
           "clkgate", "clkinvlp", "buf_16", "inv_16", "hold", "scan", "mux2i", "probec")


def _perm_match(tt, inputs, role_n, role_tt):
    if tt is None or len(inputs) != role_n:
        return False
    for perm in itertools.permutations(range(role_n)):
        t2 = 0
        for k in range(1 << role_n):
            k2 = 0
            for i in range(role_n):
                if (k >> i) & 1:
                    k2 |= 1 << perm[i]
            if (tt >> k2) & 1:
                t2 |= 1 << k
        if t2 == role_tt:
            return True
    return False


def classify(lib):
    cands = {r: [] for r in list(ROLE_TT) + ["INVX4", "NAND2X2", "HADDX1", "FADDX1", "DFFX1", "DFFARX1", "ICG"]}
    for c in lib.cells.values():
        lname = c.name.lower()
        if c.icg:
            cands["ICG"].append(c)
            continue
        if any(x in lname for x in EXCLUDE) or c.dont_use or c.area <= 0:
            continue
        if c.ff:
            ns, clk = c.ff.get("next_state", ""), c.ff.get("clocked_on", "")
            ins = c.inputs
            if clk.startswith("!") or clk.endswith("'") or not re.fullmatch(r"\w+", ns.replace('"', "")):
                continue
            if c.ff.get("preset"):
                continue
            outs = [p for p in c.outputs if c.pins[p].func and c.pins[p].func.replace('"', "").strip() in
                    (c.ff.get("_q", "IQ"), "IQ", c.ff.get("_q", ""))]
            if not outs:
                continue
            if c.ff.get("clear") and len(ins) == 3:
                cands["DFFARX1"].append(c)
            elif not c.ff.get("clear") and len(ins) == 2:
                cands["DFFX1"].append(c)
            continue
        if c.latch:
            continue
        outs = [p for p in c.outputs if c.pins[p].func]
        ins = c.inputs
        if len(outs) == 2 and len(ins) in (2, 3):
            tts = {o: truth_table(c.pins[o].func, ins) for o in outs}
            n = len(ins)
            xor = _tt_of(lambda *b: sum(b) % 2 == 1, n)
            car = _tt_of((lambda a, b: a and b) if n == 2 else MAJ3, n)
            s = [o for o, t in tts.items() if t == xor]
            co = [o for o, t in tts.items() if t == car]
            if s and co:
                cands["HADDX1" if n == 2 else "FADDX1"].append((c, s[0]))
            continue
        if len(outs) != 1:
            continue
        tt = truth_table(c.pins[outs[0]].func, ins)
        for r, (n, rtt) in ROLE_TT.items():
            if _perm_match(tt, ins, n, rtt):
                cands[r].append(c)
                if r == "INVX1":
                    cands["INVX4"].append(c)
                if r == "NAND2X1":
                    cands["NAND2X2"].append(c)
    roles = {}
    outpin = {}
    lib.role_cands = {}
    for r, lst in cands.items():
        if not lst:
            continue
        items = [(x if isinstance(x, tuple) else (x, None)) for x in lst]
        std = lambda nm: bool(re.search(r"(_1|X1|_x1)$", nm))
        weak = lambda nm: bool(re.search(r"(_0|X0|_x0|_lp\d*)$", nm))
        items.sort(key=lambda t: (weak(t[0].name), t[0].area, t[0].name))
        if r not in ("INVX4", "NAND2X2", "BUFX2"):
            items.sort(key=lambda t: (not std(t[0].name), weak(t[0].name), t[0].area, t[0].name))
        lib.role_cands[r] = [(t[0].name, t[1]) for t in items]
        if r in ("INVX4",):
            pick = items[min(2, len(items) - 1)]
        elif r in ("NAND2X2", "BUFX2"):
            pick = items[min(1, len(items) - 1)]
        else:
            pick = items[0]
        roles[r] = pick[0].name
        pick[0].role = pick[0].role or r
        if pick[1]:
            outpin[r] = pick[1]
    lib.roles = roles
    lib.role_out = outpin
    return roles


# --------------------------------------------------------------------------
# 로드
# --------------------------------------------------------------------------
def _unit(s, base):
    """'1ns' / '1ps' / '1nW' ... -> 배율"""
    m = re.match(r"\s*([\d.]+)\s*([a-zA-Z]+)", str(s))
    if not m:
        return 1.0
    v = float(m.group(1))
    u = m.group(2).lower()
    table = {"ns": 1.0, "ps": 1e-3, "us": 1e3, "fs": 1e-6,
             "w": 1.0, "mw": 1e-3, "uw": 1e-6, "nw": 1e-9, "pw": 1e-12, "fw": 1e-15}
    return v * table.get(u, 1.0)


def build(path, text=None):
    if text is None:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    g = parse_liberty_text(text)
    if g is None:
        raise ValueError("no library group found")
    lib = TechLib(path)
    lib.name = g.args[0] if g.args else os.path.splitext(os.path.basename(path))[0]
    lib.time_scale = _unit(g.get("time_unit", "1ns"), "ns")
    cl = g.get("capacitive_load_unit", ["1", "pf"])
    if isinstance(cl, list) and len(cl) >= 2:
        lib.cap_scale = float(cl[0]) * {"pf": 1.0, "ff": 1e-3, "nf": 1e3}.get(cl[1].lower(), 1.0)
    lib.leak_unit = g.get("leakage_power_unit", "1nW")
    lib.leak_scale = _unit(lib.leak_unit, "W")
    try:
        lib.volt = float(g.get("nom_voltage", 1.0))
        lib.temp = float(g.get("nom_temperature", 25.0))
        lib.process = float(g.get("nom_process", 1.0))
    except ValueError:
        pass
    ocs = g.groups("operating_conditions")
    lib.oc = g.get("default_operating_conditions", ocs[0].args[0] if ocs and ocs[0].args else lib.name)
    for oc in ocs:
        if oc.args and oc.args[0] == lib.oc:
            try:
                lib.volt = float(oc.get("voltage", lib.volt))
                lib.temp = float(oc.get("temperature", lib.temp))
            except ValueError:
                pass
    try:
        lib.default_max_tran = float(g.get("default_max_transition")) if g.get("default_max_transition") else None
    except ValueError:
        pass
    lib.wire_loads = [w.args[0] for w in g.groups("wire_load") if w.args]
    for t in g.groups("lu_table_template"):
        if t.args:
            lib.templates[t.args[0]] = (t.get("variable_1", ""), t.get("variable_2", ""))
    for cg in g.groups("cell"):
        if not cg.args:
            continue
        c = Cell(cg.args[0])
        try:
            c.area = float(cg.get("area", 0) or 0)
        except ValueError:
            c.area = 0.0
        try:
            c.leak = float(cg.get("cell_leakage_power", 0) or 0)
        except ValueError:
            c.leak = 0.0
        c.dont_use = str(cg.get("dont_use", "false")).lower() == "true"
        c.dont_touch = str(cg.get("dont_touch", "false")).lower() == "true"
        c.footprint = cg.get("cell_footprint", "")
        c.icg = bool(cg.get("clock_gating_integrated_cell"))
        for ff in cg.groups("ff"):
            c.ff = dict(ff.attrs)
            c.ff["_q"] = ff.args[0] if ff.args else "IQ"
        for la in cg.groups("latch"):
            c.latch = dict(la.attrs)
        pin_groups = list(cg.groups("pin"))
        for bus in cg.groups("bus"):
            pin_groups += bus.groups("pin")
        for pg in pin_groups:
            for pname in pg.args:
                p = Pin(pname)
                p.dir = pg.get("direction", "input")
                try:
                    p.cap = float(pg.get("capacitance", 0) or 0)
                except ValueError:
                    p.cap = 0.0
                f = pg.get("function")
                p.func = f if f else None
                p.clock = str(pg.get("clock", "false")).lower() == "true" or bool(pg.get("clock_gate_clock_pin"))
                try:
                    p.max_cap = float(pg.get("max_capacitance")) if pg.get("max_capacitance") else None
                except ValueError:
                    pass
                c.pins[pname] = p
                for tg in pg.groups("timing"):
                    a = Arc(pname, (tg.get("related_pin", "") or "").split()[0] if tg.get("related_pin") else "",
                            tg.get("timing_type", "combinational"), tg.get("timing_sense", ""))
                    for tb in tg.subs:
                        if tb.type in ("cell_rise", "cell_fall", "rise_transition", "fall_transition",
                                       "rise_constraint", "fall_constraint"):
                            try:
                                a.tables[tb.type] = Table(tb, lib.templates)
                            except (ValueError, IndexError):
                                pass
                    c.arcs.append(a)
        lib.cells[c.name] = c
    classify(lib)
    return lib


CACHE_VERSION = 4


def load(path, cache_dir=None):
    st = os.stat(path)
    key = None
    if cache_dir:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.abspath(path))[-120:]
        key = os.path.join(cache_dir, "%s.v%d.pkl" % (safe, CACHE_VERSION))
        try:
            with open(key, "rb") as f:
                meta, lib = pickle.load(f)
            if meta == (st.st_size, int(st.st_mtime)):
                return lib
        except Exception:
            pass
    lib = build(path)
    if key:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(key, "wb") as f:
                pickle.dump(((st.st_size, int(st.st_mtime)), lib), f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            pass
    return lib
