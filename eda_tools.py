# -*- coding: utf-8 -*-
"""
eda_tools.py  -  dc_shell_sim 의 PrimeTime(pt_shell) / Formality(fm_shell) 모의 셸

  * DC 엔진(dc_shell_sim.Shell)을 상속해서 같은 Tcl 해석기, 타이밍 모델, 라이브러리 리더를 씁니다.
  * DC 가 write -format verilog 로 만든 netlist 안의 "// SIMMETA" 정보를 읽어서 디자인을 복원합니다.
  * 출력 형식은 실제 툴 화면(report_analysis_coverage, check_timing, verify 결과)을 따릅니다.
"""
import os
import re
import json
import math
import random
import hashlib
from collections import OrderedDict

_CACHE = {}


def make_classes(dcm):
    if id(dcm) in _CACHE:
        return _CACHE[id(dcm)]
    DCError = dcm.DCError
    Coll = dcm.Coll
    ShellBase = dcm.Shell
    RV = dcm.REPORT_VERSION

    def read_meta(sh, path):
        txt = sh.read_text(path)
        m = re.search(r"^// SIMMETA (\{.*\})\s*$", txt, re.M)
        if not m:
            return None, txt
        try:
            return json.loads(m.group(1)), txt
        except ValueError:
            return None, txt

    # ======================================================================
    #  PrimeTime
    # ======================================================================
    class PTShell(ShellBase):
        TOOL = "pt"
        DC_ONLY = ("compile", "compile_ultra", "analyze", "elaborate", "uniquify", "ungroup", "set_dont_use",
                   "insert_clock_gating", "optimize_netlist", "change_names", "define_design_lib",
                   "report_design_lib", "set_max_area", "set_flatten", "set_structure", "set_ungroup",
                   "set_optimize_registers", "report_resources", "report_compile_options", "set_svf",
                   "read_file", "check_design", "tutorial", "report_area", "report_reference")

        def attach(self, term, fs, cwd, env, **kw):
            ShellBase.attach(self, term, fs, cwd, env, **kw)
            self.prompt = "pt_shell> "
            for k in self.DC_ONLY:
                self.cmds.pop(k, None)
            for k, v in (("link_path", "*"), ("timing_remove_clock_reconvergence_pessimism", "false"),
                         ("timing_input_port_default_clock", "false"), ("si_enable_analysis", "false"),
                         ("power_enable_analysis", "false"), ("timing_save_pin_arrival_and_slack", "false"),
                         ("report_default_significant_digits", "2")):
                self.vars[k] = v
                self.app_vars.add(k)
            self.cmds["link"] = self.c_link_design
            self.pt_pending = None
            self.pt_linked = False
            self.timing_updated = False

        def load_setup(self):
            files = []
            if not self.no_home:
                files.append(self.fs.home + "/.synopsys_pt.setup")
            if not self.no_local:
                files.append(self.fs.abspath(".synopsys_pt.setup", self.cwd))
            for f in OrderedDict.fromkeys(files):
                if self.fs.isfile(f):
                    self.depth += 1
                    try:
                        self.run_script(self.fs.read_text(f), quiet=True)
                    finally:
                        self.depth -= 1

        def banner(self):
            for l in ["",
                      "                                 PrimeTime (R)          (Simulated)",
                      "                               PrimeTime (R) SI         (Simulated)",
                      "                               PrimeTime (R) PX         (Simulated)",
                      "                            PrimeTime (R) ECO Option    (Simulated)",
                      "",
                      "                Version %s for linux64" % RV,
                      "",
                      "      dc_shell_sim : 교육용 모의 PrimeTime 입니다. 실제 STA 를 수행하지 않습니다.",
                      ""]:
                self.out(l)
            self.load_setup()

        def goodbye(self):
            self.out("")
            self.out("Timing updates: %d (%d implicit, %d explicit) (%d incremental, %d full, 0 logical)." % (
                1 if self.timing_updated else 0, 0, 1 if self.timing_updated else 0, 0, 1 if self.timing_updated else 0))
            self.out("Noise updates: 0 (0 implicit, 0 explicit) (0 incremental, 0 full).")
            self.out("Maximum memory usage for this session: %.2f MB" % (230.0 + len(self.designs) * 4))
            self.out("CPU usage for this session:   %d seconds ( %.2f hours )" % (int(self.cpu + 1), (self.cpu + 1) / 3600))
            self.out("Elapsed time for this session:   %d seconds ( %.2f hours )" % (
                int(dcm.time.time() - self.t0), (dcm.time.time() - self.t0) / 3600))
            self.out("")
            self.out("Thank you for using pt_shell!")

        # PT 는 link_path 로 라이브러리를 찾음
        def lib(self):
            lp = [x for x in self.to_str(self.vars.get("link_path", "*")).split() if x != "*"]
            if lp:
                saved = self.vars.get("target_library")
                self.vars["target_library"] = lp[0]
                try:
                    return ShellBase.lib(self)
                finally:
                    self.vars["target_library"] = saved
            raise DCError("No library in link_path.", "PT-001",
                          hint="set link_path \"* sky130_fd_sc_hd__tt_025C_1v80.db\"  (search_path 도 확인)")

        def c_read_verilog(self, a):
            o, pos = self.opts("read_verilog", a, flags=["-hdl_compiler", "-netlist"])
            files = []
            for p in pos:
                files += self.tcl_list(p)
            if not files:
                raise DCError("Required argument 'files' was not found", "CMD-007")
            for fn in files:
                p = self.resolve(fn)
                if not p:
                    raise DCError("Cannot open file '%s'." % fn, "PT-FILE")
                self.out("Loading verilog file '%s'" % p)
                meta, txt = read_meta(self, p)
                if meta is None:
                    if re.search(r"\balways\b|\bassign\b.*[+\-*]", txt):
                        raise DCError("'%s' looks like RTL. PrimeTime reads gate-level netlists only." % fn, "PT-RTL",
                                      hint="DC 에서 write -format verilog -hierarchy -output ..._netlist.v 로 만든 netlist 를 읽으세요.")
                    raise DCError("Netlist '%s' was not written by dc_shell_sim (no SIMMETA)." % fn, "PT-META")
                self.pt_pending = (meta, p)
                self.pt_linked = False
            self.out("Information: Reading Verilog into new design registry.")
            return 1

        def c_read_ddc(self, a):
            r = ShellBase.c_read_ddc(self, a)
            self.pt_linked = True
            return r

        def c_current_design(self, a):
            if a and self.pt_pending and not self.pt_linked:
                name = self.to_str(self.names_of(a[0])[0])
                if name != self.pt_pending[0]["top"]:
                    raise DCError("Can't find design '%s'." % name, "UID-109")
                self.cur_pending_name = name
                return Coll("design", [name])
            return ShellBase.c_current_design(self, a)

        def c_link_design(self, a):
            if not self.pt_pending:
                if self.cur and self.cur in self.designs:
                    return 1
                raise DCError("No design to link. Use read_verilog first.", "PT-LINK")
            meta, path = self.pt_pending
            lib = self.lib()
            self.out("Linking design '%s'..." % meta["top"])
            top = self.restore_from_meta(meta, apply_sdc=False, lib_override=True)
            self.out("Using the following designs and libraries:")
            self.out("--------------------------------------------------------------------------")
            self.out("  %-30s %s" % (top, path))
            self.out("  %-30s %s" % ("%s (library)" % lib["name"], self.lib_file(lib)))
            nb = len(self.all_flop_bits(self.designs[top]))
            tot = self.totals(self.designs[top])
            self.out("Information: Module '%s' in file '%s' successfully linked." % (top, path))
            self.out("Information: %d leaf cells, %d registers. (LNK-045)" % (tot["ncomb"] + tot["nseq"], nb))
            self.pt_linked = True
            self.timing_updated = False
            return 1

        def need_design(self):
            if self.pt_pending and not self.pt_linked:
                self.c_link_design([])
            return ShellBase.need_design(self)

        def c_update_timing(self, a):
            d = self.need_design()
            self.ensure_stats(d)
            self.out("Information: Updating design information... (UID-85)")
            self.timing_updated = True
            return 1

        def c_check_timing(self, a):
            d = self.need_design()
            self.ensure_stats(d)
            checks = ["no_input_delay", "no_driving_cell", "unconstrained_endpoints", "unexpandable_clocks",
                      "latch_fanout", "no_clock", "partial_input_delay", "generic", "loops", "generated_clocks",
                      "pulse_clock_non_pulse_clock_merge", "pll_configuration", "voltage_level"]
            clk_ports = set(p for c in d.clocks.values() for p in c["src"])
            fp_from = set(n for f, t in d.false_paths for n in f)
            noin = [p[1] for p in d.ports if p[0] == "input" and p[1] not in d.in_delay and p[1] not in clk_ports
                    and p[1] not in fp_from]
            nodrv = [p[1] for p in d.ports if p[0] == "input" and p[1] not in d.driving and p[1] not in clk_ports]
            noout = [p[1] for p in d.ports if p[0] == "output" and p[1] not in d.out_delay]
            problems = 0
            for c in checks:
                self.out("Information: Checking '%s'." % c)
                if c == "no_input_delay" and noin:
                    problems += 1
                    self.warn("There are %d ports with no clock-relative input delay specified.\n"
                              " Since the variable 'timing_input_port_default_clock' is 'false',\n"
                              " no default clock will be assumed for these ports." % len(noin), "PTE-???")
                    for p in noin:
                        self.out("    %s" % p)
                if c == "unconstrained_endpoints" and noout:
                    problems += 1
                    self.warn("There are %d endpoints which are not constrained for maximum delay." % len(noout),
                              "PTE-???")
                    for p in noout:
                        self.out("    %s" % p)
                if c == "no_clock" and not d.clocks:
                    problems += 1
                    nb = len(self.all_flop_bits(d))
                    self.warn("There are %d register clock pins with no clock." % nb, "PTE-???")
            if problems:
                self.out("check_timing failed.")
                self.hint("위 경고는 SDC 에 set_input_delay / set_output_delay / create_clock 이 빠졌다는 뜻입니다.")
                return 0
            self.out("check_timing succeeded.")
            return 1

        # ---- 커버리지 계산 ----
        def _coverage(self, d):
            lib = self.ensure_stats(d)
            cands, clk, per, unc, setup, lat = self.path_candidates(d, lib, d.mapped)
            bits = self.all_flop_bits(d)
            n = len(bits)
            async_ = any(x.mod.async_reset for x in self.all_designs_under(d))
            outs = sum(w for dr, nm, w in d.ports if dr == "output")
            out_c = sum(w for dr, nm, w in d.ports if dr == "output" and nm in d.out_delay)
            rows = OrderedDict()
            rng = random.Random(dcm.stable_seed(d.name, "cov", per))

            def spread(worst, best, count):
                """endpoint slack 분포 -> 위반 개수"""
                if count == 0:
                    return 0
                if worst >= 0:
                    return 0
                viol = 0
                for i in range(count):
                    fr = (i / float(max(count - 1, 1))) ** 0.6
                    s = worst + (best - worst) * fr
                    if s < 0:
                        viol += 1
                return viol
            if not clk:
                rows["setup"] = (n, 0, 0, n)
                rows["hold"] = (n, 0, 0, n)
                if async_:
                    rows["recovery"] = (n, 0, 0, n)
                    rows["removal"] = (n, 0, 0, n)
                rows["min_pulse_width"] = (n * (3 if async_ else 2), 0, 0, n * (3 if async_ else 2))
                rows["out_setup"] = (outs, 0, 0, outs)
                rows["out_hold"] = (outs, 0, 0, outs)
                return rows
            r2r = [c for c in cands if c["constrained"] and c["kind"] in ("r2r", "i2r")]
            worst = min([c["slack"] for c in r2r] + [per])
            sv = spread(worst, per * 0.7, n)
            rows["setup"] = (n, n - sv, sv, 0)
            hold_s = self.hold_check_slack(d, lib)
            hv = spread(hold_s, per * 0.2, n) if hold_s < 0 else 0
            rows["hold"] = (n, n - hv, hv, 0)
            t = lib.get("tech")
            dff = (lib.get("cmap") or {}).get("DFFARX1") if lib.get("cmap") else None
            if async_:
                rst = [p[1] for p in d.ports if p[0] == "input" and re.search(r"rst|reset", p[1], re.I)]
                rstp = rst[0] if rst else None
                fp = any(rstp in f for f, _ in d.false_paths) if rstp else True
                if not rstp or fp or rstp not in d.in_delay:
                    rows["recovery"] = (n, 0, 0, n)
                    rows["removal"] = (n, 0, 0, n)
                else:
                    rec = t.constraint(dff[5], "recovery") if (t and dff) else 0.1 * lib["scale"]
                    buf = self.ci("BUFX2", lib)[1]
                    levels = max(1, int(math.ceil(math.log(max(n, 2), 4))))
                    arr = d.in_delay[rstp][0] + lat + buf * levels
                    req = per + lat - unc - rec
                    worst_r = req - arr - buf * 1.5
                    rv = spread(worst_r, worst_r + buf * levels + per * 0.05, n)
                    rows["recovery"] = (n, n - rv, rv, 0)
                    rows["removal"] = (n, n, 0, 0)
            mpw = t.constraint(dff[5], "min_pulse_width") if (t and dff) else 0.12 * lib["scale"]
            high = per / 2.0
            k = 3 if async_ else 2
            mv = 2 * n if high < mpw else 0
            rows["min_pulse_width"] = (n * k, n * k - mv, mv, 0)
            osv = 0
            out_c_list = [c for c in cands if c["constrained"] and c["kind"] in ("r2o", "i2o")]
            if out_c_list and min(c["slack"] for c in out_c_list) < 0:
                osv = spread(min(c["slack"] for c in out_c_list), per * 0.4, out_c)
            rows["out_setup"] = (outs, out_c - osv, osv, outs - out_c)
            rows["out_hold"] = (outs, out_c, 0, outs - out_c)
            return rows

        def hold_check_slack(self, d, lib):
            hold = self.hold_t(lib)
            clk2q = self.ci("DFFARX1", lib)[1]
            unc_h = 0.0
            if d.clocks:
                c = list(d.clocks)[0]
                unc_h = d.unc.get(c + ":hold", d.unc.get(c, 0.0))
            return clk2q + self.ci("INVX1", lib)[1] * 0.5 - hold - unc_h

        def c_report_analysis_coverage(self, a):
            d = self.need_design()
            o, pos = self.opts("report_analysis_coverage", a, flags=["-status_details", "-nosplit", "-sort_by"],
                               valued=["-check_type", "-exclude_untested"])
            rows = self._coverage(d)
            self.header("analysis_coverage")
            self.out("%-20s%7s%17s%17s%17s" % ("Type of Check", "Total", "Met", "Violated", "Untested"))
            self.out("-" * 78)
            T = [0, 0, 0, 0]

            def pct(x, tot):
                return int(round(100.0 * x / tot)) if tot else 0
            for name, (tot, met, vio, unt) in rows.items():
                if tot == 0:
                    continue
                T = [T[0] + tot, T[1] + met, T[2] + vio, T[3] + unt]
                self.out("%-20s%7d%11d (%3d%%)%10d (%3d%%)%10d (%3d%%)" % (
                    name, tot, met, pct(met, tot), vio, pct(vio, tot), unt, pct(unt, tot)))
            self.out("-" * 78)
            self.out("%-20s%7d%11d (%3d%%)%10d (%3d%%)%10d (%3d%%)" % (
                "All Checks", T[0], T[1], pct(T[1], T[0]), T[2], pct(T[2], T[0]), T[3], pct(T[3], T[0])))
            if "-status_details" in o:
                self.out("")
                for name, (tot, met, vio, unt) in rows.items():
                    if unt:
                        self.out("  %s : %d untested (no constraint / false path / unclocked)" % (name, unt))
            self.out("")
            return 1

        def c_report_global_timing(self, a):
            d = self.need_design()
            rows = self._coverage(d)
            lib = self.ensure_stats(d)
            cands, clk, per, unc, setup, lat = self.path_candidates(d, lib, d.mapped)
            cs = [c for c in cands if c["constrained"]]
            wns = min([c["slack"] for c in cs] + [0.0])
            nv = rows.get("setup", (0, 0, 0, 0))[2]
            hs = self.hold_check_slack(d, lib)
            nh = rows.get("hold", (0, 0, 0, 0))[2]
            self.header("global_timing")
            self.out("Setup violations")
            self.out("---------------------------------------------------------")
            self.out("             Total      reg->reg     in->reg    reg->out    in->out")
            self.out("---------------------------------------------------------")
            self.out("WNS      %9.2f %12.2f %11.2f %11.2f %10.2f" % (min(wns, 0), min(wns, 0), 0, 0, 0))
            self.out("TNS      %9.2f %12.2f %11.2f %11.2f %10.2f" % (min(wns, 0) * nv, min(wns, 0) * nv, 0, 0, 0))
            self.out("NUM      %9d %12d %11d %11d %10d" % (nv, nv, 0, 0, 0))
            self.out("---------------------------------------------------------")
            if nh:
                self.out("")
                self.out("Hold violations")
                self.out("WNS      %9.2f   TNS %9.2f   NUM %d" % (hs, hs * nh, nh))
            if not nv and not nh:
                self.out("")
                self.out("No setup violations found.")
                self.out("No hold violations found.")
            self.out("")
            return 1

        def c_report_timing(self, a):
            self.timing_updated = True
            return ShellBase.c_report_timing(self, a)

        def c_read_parasitics(self, a):
            self.out("Information: dc_shell_sim does not model parasitics; read_parasitics ignored.")
            return 1

        def c_set_operating_conditions(self, a):
            return 1

        def c_report_qor(self, a):
            return ShellBase.c_report_qor(self, a)

        def c_save_session(self, a):
            raise DCError("save_session is not supported in the simulator.", "SIM-7")

    # ======================================================================
    #  Formality
    # ======================================================================
    class FMShell(ShellBase):
        TOOL = "fm"

        def attach(self, term, fs, cwd, env, **kw):
            ShellBase.attach(self, term, fs, cwd, env, **kw)
            self.mode = "setup"
            self.prompt_fn = lambda: "fm_shell (%s)> " % self.mode
            self.prompt = self.prompt_fn()
            keep = set(("set", "unset", "echo", "puts", "expr", "if", "foreach", "for", "while", "incr", "proc",
                        "list", "llength", "lindex", "lappend", "concat", "format", "string", "glob", "source",
                        "redirect", "file", "history", "ls", "cd", "pwd", "sh", "exec", "exit", "quit", "date",
                        "clear", "getenv", "setenv", "printvar", "set_app_var", "get_app_var", "break", "continue",
                        "report_app_var", "help", "man"))
            for k in list(self.cmds):
                if k not in keep:
                    del self.cmds[k]
            for name in dir(self):
                if name.startswith("fm_"):
                    self.cmds[name[3:]] = getattr(self, name)
            for k, v in (("synopsys_auto_setup", "false"), ("hdlin_ignore_parallel_case", "false"),
                         ("hdlin_ignore_full_case", "false"), ("hdlin_error_on_mismatch_message", "false"),
                         ("hdlin_ignore_embedded_configuration", "true"),
                         ("verification_set_undriven_signals", "synthesis"),
                         ("verification_verify_directly_undriven_output", "false"),
                         ("verification_failing_point_limit", "20")):
                self.vars[k] = v
                self.app_vars.add(k)
            self.svf = None
            self.fm_libs = []
            self.ref = dict(files=[], modules=OrderedDict(), top=None)
            self.impl = dict(files=[], meta=None, top=None)
            self.result = None

        def _set_mode(self, m):
            self.mode = m
            self.prompt = "fm_shell (%s)> " % m

        def load_setup(self):
            for f in (self.fs.home + "/.synopsys_fm.setup", self.fs.abspath(".synopsys_fm.setup", self.cwd)):
                if self.fs.isfile(f):
                    self.depth += 1
                    try:
                        self.run_script(self.fs.read_text(f), quiet=True)
                    finally:
                        self.depth -= 1

        def banner(self):
            for l in ["",
                      "                               Formality (R)            (Simulated)",
                      "",
                      "                Version %s for linux64" % RV,
                      "",
                      "      dc_shell_sim : 교육용 모의 Formality 입니다. 실제 등가성 검증을 수행하지 않습니다.",
                      "      (RTL 이 합성 후 바뀌었는지 등을 비교해서 결과를 만듭니다.)",
                      ""]:
                self.out(l)
            self.load_setup()

        def goodbye(self):
            self.out("")
            self.out("Maximum memory usage for this session: %.0f MB" % 412)
            self.out("CPU usage for this session: %d seconds" % int(self.cpu + 2))
            self.out("Current time: %s" % dcm.dc_date())
            self.out("Elapsed time: %d seconds" % int(dcm.time.time() - self.t0))
            self.out("")
            self.out("Thank you for using Formality (R)!")

        # ---- setup ----
        def fm_set_svf(self, a):
            o, pos = self.opts("set_svf", a, flags=["-append", "-ordered", "-extended_checking"])
            if not pos:
                self.svf = None
                return 1
            p = self.resolve(self.to_str(pos[0]))
            if not p:
                raise DCError("SVF file '%s' not found." % self.to_str(pos[0]), "FM-SVF",
                              hint="DC 에서 set_svf 로 지정했거나 compile 때 생긴 default.svf 를 지정하세요.")
            self.svf = p
            self.out("SVF set to '%s'." % p)
            return 1

        def fm_read_db(self, a):
            o, pos = self.opts("read_db", a, flags=["-technology_library", "-r", "-i", "-merge"], valued=["-libname"])
            files = []
            for p in pos:
                files += self.tcl_list(p)
            for fn in files:
                p = self.find_file(fn)
                if not p:
                    raise DCError("Cannot find file '%s'." % fn, "FM-016",
                                  hint="search_path 를 확인하거나 전체 경로를 쓰세요.")
                self.out("Loading db file '%s'" % p)
                self.fm_libs.append(p)
            return 1

        def _read(self, a, cmd):
            o, pos = self.opts(cmd, a, flags=["-r", "-i", "-container", "-technology_library", "-netlist", "-01",
                                              "-05", "-09", "-12", "-17"], valued=["-libname", "-define", "-vcs"])
            side = "r" if "-r" in o else ("i" if "-i" in o else None)
            if not side:
                raise DCError("Must specify -r (reference) or -i (implementation).", "FM-SIDE")
            files = []
            for p in pos:
                files += self.tcl_list(p)
            if not files:
                raise DCError("Required argument 'files' was not found", "CMD-007")
            lib = self.to_str(o.get("-libname", "WORK"))
            for fn in files:
                p = self.resolve(fn)
                if not p:
                    raise DCError("Cannot open file '%s'." % fn, "FM-FILE")
                self.out("Loading verilog file '%s'" % p)
                if side == "r":
                    meta, txt = read_meta(self, p)
                    if meta is not None:
                        self.warn("Reference design '%s' is a gate-level netlist." % fn, "FM-REF")
                    mods, _ = dcm.parse_verilog_text(txt, p)
                    for m in mods:
                        self.ref["modules"][m.name] = m
                    self.ref["files"].append(p)
                else:
                    meta, txt = read_meta(self, p)
                    if meta is None:
                        raise DCError("Implementation file '%s' was not written by dc_shell_sim DC "
                                      "(no SIMMETA)." % fn, "FM-IMPL")
                    self.impl["meta"] = meta
                    self.impl["files"].append(p)
            self.out("Current container set to '%s'" % side)
            self.fm_lib = lib
            return 1

        def fm_read_verilog(self, a):
            return self._read(a, "read_verilog")

        def fm_read_sverilog(self, a):
            return self._read(a, "read_sverilog")

        def fm_read_ddc(self, a):
            o, pos = self.opts("read_ddc", a, flags=["-r", "-i"], valued=["-libname"])
            if "-i" not in o:
                raise DCError("Simulator supports read_ddc -i only.", "FM-DDC")
            p = self.resolve(self.to_str(pos[0]))
            if not p:
                raise DCError("Cannot open file '%s'." % self.to_str(pos[0]), "FM-FILE")
            data = json.loads(self.read_text(p))
            self.impl["meta"] = data
            self.impl["files"].append(p)
            self.out("Loading ddc file '%s'" % p)
            return 1

        def fm_set_top(self, a):
            o, pos = self.opts("set_top", a, flags=["-auto"], valued=["-parameters"])
            if not pos and "-auto" not in o:
                raise DCError("Required argument 'design' was not found", "CMD-007")
            spec = self.to_str(pos[0]) if pos else ""
            m = re.match(r"([ri]):/(\w+)/(\w+)$", spec)
            if not m:
                raise DCError("Invalid design specification '%s' (use r:/WORK/<top> or i:/WORK/<top>)." % spec,
                              "FM-TOP")
            side, lib, top = m.groups()
            if side == "r":
                if top not in self.ref["modules"]:
                    raise DCError("Design '%s' not found in reference container." % top, "FM-001")
                self.ref["top"] = top
                self.out("Setting top design to 'r:/%s/%s'" % (lib, top))
                self.out("Status:   Elaborating design %s   ...  " % top)
                self.out("Status:  Implementing inferred operators...")
                self.out("Top design successfully set to 'r:/%s/%s'" % (lib, top))
                self.out("Reference design set to 'r:/%s/%s'" % (lib, top))
            else:
                meta = self.impl["meta"]
                if not meta:
                    raise DCError("No implementation design read.", "FM-002")
                if top != meta["top"]:
                    raise DCError("Design '%s' not found in implementation container." % top, "FM-001")
                if not self.fm_libs:
                    raise DCError("Unresolved references: technology library cells are not loaded.", "FM-234",
                                  hint="read_db -technology_library <라이브러리.db> 를 먼저 실행하세요.")
                self.impl["top"] = top
                self.out("Setting top design to 'i:/%s/%s'" % (lib, top))
                self.out("Top design successfully set to 'i:/%s/%s'" % (lib, top))
                self.out("Implementation design set to 'i:/%s/%s'" % (lib, top))
            return 1

        # ---- 비교 ----
        def _analyze(self):
            if not self.ref["top"] or not self.impl["top"]:
                raise DCError("Reference and implementation designs must be set (set_top).", "FM-003")
            meta = self.impl["meta"]
            eng = ShellBase()
            eng.attach(self.term, self.fs, self.cwd, self.env, no_home=True, no_local=True, logname=None)
            eng.streams.append(_NullW())
            for n, m in self.ref["modules"].items():
                eng.modules[n] = m
            env = {}
            for ent in meta["designs"]:
                if ent["module"] == self.ref["top"]:
                    env = ent.get("env") or {}
            try:
                top = eng.elab(self.ref["top"], {})
            except Exception as e:
                raise DCError("Elaboration of reference failed: %s" % e, "FM-ELAB")
            td = eng.designs[top]
            refbits = [b for b, _ in eng.all_flop_bits(td)]
            outs = sum(w for dr, nm, w in td.ports if dr == "output")
            ins = sum(w for dr, nm, w in td.ports if dr == "input")
            changed = []
            for mname, h in meta.get("hashes", {}).items():
                rm = self.ref["modules"].get(mname)
                if rm is None:
                    changed.append(mname)
                    continue
                if hashlib.md5(rm.body.encode("utf-8", "replace")).hexdigest()[:16] != h:
                    changed.append(mname)
            # 변경된 모듈에 속한 compare point
            fail = []
            if changed:
                for b, x in eng.all_flop_bits(td):
                    if x.mod.name in changed:
                        fail.append(b)
                if self.ref["top"] in changed:
                    fail += ["%s[%d]" % (nm, i) if w > 1 else nm for dr, nm, w in td.ports if dr == "output"
                             for i in range(w)][:max(1, outs // 2)]
            return dict(dff=len(refbits), port=outs, inputs=ins, fail=fail, changed=changed, refbits=refbits)

        def fm_match(self, a):
            r = self._analyze()
            self.out("Reference design is 'r:/WORK/%s'" % self.ref["top"])
            self.out("Implementation design is 'i:/WORK/%s'" % self.impl["top"])
            self.out("Status:  Checking designs...")
            self.out("Status:  Building verification models...")
            if self.svf:
                self.out("Status:  Processing Guide Commands...")
            self.out("Status:  Matching...")
            self.out("    Matching by name...")
            self.out("    Matching by signature analysis...")
            self.out("")
            self.out("*********************************** Matching Results ***********************************")
            tot = r["dff"] + r["port"]
            self.out(" %d Compare points matched by name" % tot)
            self.out(" 0 Compare points matched by signature analysis")
            self.out(" 0 Compare points matched by topology")
            self.out(" %d Matched primary inputs, black-box outputs" % r["inputs"])
            self.out(" 0(0) Unmatched reference(implementation) compare points")
            self.out(" 0(0) Unmatched reference(implementation) primary inputs, black-box outputs")
            self.out(" 0(%d) Unmatched reference(implementation) unread points" % (0 if self.svf else r["dff"] // 16))
            self.out("****************************************************************************************")
            self._set_mode("match")
            self.match = r
            return 1

        def _auto_setup_summary(self):
            g, rd, mg, yl = "32", "31", "35", "33"
            star = "*"
            self.out(star * 29 + " Synopsys Auto Setup Summary " + star * 30)
            self.out("")
            self.out("!!! Synopsys Auto Setup Mode was enabled. !!!", g)
            self.out("!!! Verification results are valid assuming the following setup constraints: !!!", g)
            self.out("")
            self.out("### RTL Interpretation Setup", g)
            for k in ("hdlin_ignore_parallel_case", "hdlin_ignore_full_case", "hdlin_error_on_mismatch_message",
                      "hdlin_ignore_embedded_configuration"):
                self.out("   set %s %s" % (k, self.vars[k]))
            self.out("")
            self.out("### Undriven Signal Handling Setup", g)
            self.out("   set verification_set_undriven_signals %s" % self.vars["verification_set_undriven_signals"])
            self.out("")
            self.out("### Test Logic Setup", g)
            self.out("   set verification_verify_directly_undriven_output %s" %
                     self.vars["verification_verify_directly_undriven_output"])
            self.out("   For details see report_dont_verify_points and report_constants")
            self.out("")
            self.out("For further details on Synopsys Auto Setup Mode: Type man synopsys_auto_setup")
            self.out(star * 88)
            self.out("")
            self.out("")

        def fm_verify(self, a):
            if self.mode == "setup":
                self.fm_match([])
            r = self.match
            auto = self.to_str(self.vars.get("synopsys_auto_setup", "false")) in ("true", "1")
            self.out("Status:  Verifying...")
            self.out("")
            if auto:
                for k, v in (("hdlin_ignore_parallel_case", "false"), ("hdlin_ignore_full_case", "false"),
                             ("hdlin_error_on_mismatch_message", "false"),
                             ("hdlin_ignore_embedded_configuration", "true")):
                    self.vars[k] = v
                self._auto_setup_summary()
            ok = not r["fail"]
            nf = len(r["fail"])
            ndff_f = sum(1 for f in r["fail"] if f in r["refbits"])
            nport_f = nf - ndff_f
            self.out("*" * 32 + " Verification Results " + "*" * 34)
            if ok:
                self.out("Verification SUCCEEDED")
            else:
                self.out("Verification FAILED")
            if auto:
                self.out("   ATTENTION: synopsys_auto_setup mode was enabled.")
                self.out("              See Synopsys Auto Setup Summary for details.")
            self.out("-" * 59)
            self.out(" Reference design: r:/WORK/%s" % self.ref["top"])
            self.out(" Implementation design: i:/WORK/%s" % self.impl["top"])
            tot = r["dff"] + r["port"]
            if ok:
                self.out(" %d Passing compare points" % tot)
            else:
                self.out(" %d Failing compare points" % nf)
                self.out(" %d Passing compare points" % (tot - nf))
                self.out(" 0 Aborted compare points")
                self.out(" 0 Unverified compare points")
            self.out("-" * 88)
            self.out("Matched Compare Points     BBPin    Loop   BBNet     Cut    Port     DFF     LAT   TOTAL")
            self.out("-" * 88)
            self.out("Passing (equivalent)    %8d%8d%8d%8d%8d%8d%8d%8d" % (0, 0, 0, 0, r["port"] - nport_f,
                                                                        r["dff"] - ndff_f, 0, tot - nf))
            self.out("Failing (not equivalent)%8d%8d%8d%8d%8d%8d%8d%8d" % (0, 0, 0, 0, nport_f, ndff_f, 0, nf))
            self.out("*" * 88)
            self._set_mode("verify")
            self.result = r
            if not ok:
                self.hint("합성 후 RTL(%s)이 바뀌었습니다. 다시 합성하거나 report_failing_points 로 확인하세요."
                          % ", ".join(r["changed"]))
            return 1 if ok else 0

        def fm_report_failing_points(self, a):
            r = self.result
            if r is None:
                raise DCError("No verification results. Run verify first.", "FM-RES")
            self.out("")
            self.out("**************************************************")
            self.out("Report         : failing_points")
            self.out("Reference      : r:/WORK/%s" % self.ref["top"])
            self.out("Implementation : i:/WORK/%s" % self.impl["top"])
            self.out("Version        : %s" % RV)
            self.out("Date           : %s" % dcm.dc_date())
            self.out("**************************************************")
            self.out("")
            if not r["fail"]:
                self.out("No failing compare points.")
                return 1
            self.out("%d Failing compare points:" % len(r["fail"]))
            self.out("")
            for f in r["fail"][:int(self.vars.get("verification_failing_point_limit", 20))]:
                kind = "DFF" if f in r["refbits"] else "Port"
                rn = re.sub(r"_reg_(\d+)_$", r"_reg[\1]", f)
                self.out("  Ref  %-5s r:/WORK/%s/%s" % (kind, self.ref["top"], rn))
                self.out("  Impl %-5s i:/WORK/%s/%s" % (kind, self.impl["top"], f))
                self.out("")
            return 1

        def fm_report_unmatched_points(self, a):
            self.out("")
            self.out("0 Unmatched reference compare points")
            self.out("0 Unmatched implementation compare points")
            return 1

        def fm_report_passing_points(self, a):
            r = self.result or {}
            self.out("%d Passing compare points" % (r.get("dff", 0) + r.get("port", 0) - len(r.get("fail", []))))
            return 1

        def fm_report_status(self, a):
            self.out("Current mode: %s" % self.mode)
            self.out("Reference      : %s" % ("r:/WORK/%s" % self.ref["top"] if self.ref["top"] else "(not set)"))
            self.out("Implementation : %s" % ("i:/WORK/%s" % self.impl["top"] if self.impl["top"] else "(not set)"))
            if self.result is not None:
                self.out("Verification   : %s" % ("SUCCEEDED" if not self.result["fail"] else "FAILED"))
            return 1

        def fm_setup(self, a):
            self._set_mode("setup")
            return 1

        def fm_diagnose(self, a):
            r = self.result
            if not r or not r["fail"]:
                self.out("No failing points to diagnose.")
                return 1
            self.out("Status:  Diagnosing failing points...")
            self.out("Changed RTL module(s) since synthesis: %s" % ", ".join(r["changed"]))
            return 1

        def fm_report_guidance(self, a):
            self.out("SVF: %s" % (self.svf or "(none)"))
            return 1

        def fm_report_constants(self, a):
            self.out("No user-specified constants.")
            return 1

        def fm_report_dont_verify_points(self, a):
            self.out("No dont_verify points.")
            return 1

        def fm_save_session(self, a):
            raise DCError("save_session is not supported in the simulator.", "SIM-7")

    class _NullW:
        tty = False
        screen = False

        def write(self, s):
            pass

    _CACHE[id(dcm)] = (PTShell, FMShell)
    return PTShell, FMShell
