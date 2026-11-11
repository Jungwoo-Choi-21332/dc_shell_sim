# sky130_fd_sc_hd (SkyWater SKY130 high-density standard cells)

- Copyright 2020 The SkyWater PDK Authors. Licensed under the **Apache License, Version 2.0** (see `LICENSE`).
- Original: https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd
- Liberty files obtained from: https://github.com/The-OpenROAD-Project/OpenROAD (`test/sky130hd/`)

## Modifications (dc_shell_sim)

To reduce file size, the following groups were removed from the three `.lib` files:
`internal_power`, `leakage_power` (state-dependent), CCS (`ccsn_*`, `output_current_*`, `receiver_capacitance*`),
and noise/OCV tables. NLDM timing tables, constraints, area, pin, function and `cell_leakage_power`
data are unchanged. The `db/*.db` files under `/tools/lib/sky130_fd_sc_hd/db` inside the simulator are
virtual placeholders; the simulator reads the `.lib` text.

| file | corner |
|---|---|
| `lib/sky130_fd_sc_hd__tt_025C_1v80.lib` | typical, 25°C, 1.80 V |
| `lib/sky130_fd_sc_hd__ss_n40C_1v40.lib` | slow, −40°C, 1.40 V |
| `lib/sky130_fd_sc_hd__ff_n40C_1v95.lib` | fast, −40°C, 1.95 V |
