# fmultiplier — FP32 Multiplier (Handshake, Multi-Cycle, IEEE-754)

## Overview
`fmultiplier` is a **multi-cycle** single-precision floating-point multiplier that accepts one operation at a time using a **valid/out_valid** handshake. Internally it runs a staged pipeline controlled by a small FSM (`counter`) and produces a 32-bit IEEE-754 binary32 result.

This design targets:
- **Round-to-nearest-even (RNE)** for the normal multiply path,
- Deterministic latency (fixed number of cycles from `valid` to `out_valid`),
- Operand classification and dedicated handling for **NaN, infinity, zero, subnormal, and normal** inputs,
- Behavior: `z = a * b` where `a`, `b`, and `z` are single-precision IEEE-754 binary32 values.

The normal-number path is intended to be bit-accurate against IEEE-754 binary32 multiply with RNE. Special-value results follow the rules in [Special-case results](#special-case-results) below (not full IEEE payload propagation).

---

## Critical Implementation Notes

Before writing any logic, read these — they are the most common causes of test failure:

1. **`out_valid` must always be driven.** Declare it as `reg` and assign `out_valid <= 0` as the default in every clock cycle. Only set `out_valid <= 1` at stage 7 (counter == 7). Leaving `out_valid` unassigned causes it to stay `X`, which fails the test immediately.
2. **`z` must also be driven every cycle.** Use a default `z <= z` or `z <= 0` before the case statement.
3. **Test your implementation yourself.** Run `cat tests/test_multiply_fp32.py` to see the testbench, then write your own Icarus Verilog test to confirm `out_valid` pulses at cycle 7 before considering the task complete.
4. **Minimal working skeleton:**
   ```verilog
   always @(posedge clk) begin
     out_valid <= 0;  // default every cycle — REQUIRED
     if (rst) begin
       busy <= 0; counter <= 0; z <= 0;
     end else if (!busy && valid) begin
       busy <= 1; counter <= 1; a_r <= a; b_r <= b;
     end else if (busy) begin
       counter <= counter + 1;
       case (counter)
         // stages 1–6: computation here
         7: begin z <= packed_result; out_valid <= 1; busy <= 0; counter <= 0; end
       endcase
     end
   end
   ```

---

## Interface

### Ports
| Port | Dir | Width | Description |
|------|-----|-------|-------------|
| `clk`   | in | 1 | Clock |
| `rst`   | in | 1 | Async reset (posedge) |
| `valid` | in | 1 | **1-cycle start pulse**; accepted only when not busy |
| `a`     | in | 32 | Operand A (FP32 bits) |
| `b`     | in | 32 | Operand B (FP32 bits) |
| `z`         | out | 32 | Result (FP32 bits) |
| `out_valid` | out | 1 | **1-cycle pulse** when `z` is updated/valid |

### Reset behavior
On `rst`:
- `busy = 0`, `counter = 0`, `out_valid = 0`, `z = 0`
- Internal operand/result registers are cleared

### Handshake contract
- When `busy == 0`, a high `valid` on a rising edge **starts** an operation:
  - `a` and `b` are **registered** into internal regs `a_r` and `b_r`.
  - The FSM begins at `counter = 1`.
- While `busy == 1`, new `valid` pulses are **ignored**.
- When the operation completes:
  - `z` is updated,
  - `out_valid` pulses high for 1 clock cycle,
  - `busy` is cleared.
- `out_valid` is held low on all cycles except the completion cycle.

---

## Latency and Throughput

### Latency
- Fixed latency of **7 stages**.
- In this implementation the operation begins at stage `counter = 1` and completes at `counter = 7`.
- `out_valid` asserts on the cycle where stage 7 packing finishes.

A safe expectation for system-level timing is:
- **`out_valid` occurs 7 clock cycles after the start edge** (the clock edge where `valid` was sampled when idle).

### Throughput
- **Not pipelined** (single-issue).
- Max throughput is **1 result per 7 cycles** (assuming `valid` is asserted only when idle).

---

## Internal Data Model (IEEE-754 binary32)

### Operand field layout
For each operand:
- `sign` = bit 31
- `exp`  = bits 30:23 (biased exponent)
- `mant` = bits 22:0 (fraction)

### Operand classification (from registered operands `a_r`, `b_r`)
| Predicate | Condition |
|-----------|-----------|
| `a_is_nan` / `b_is_nan` | `exp == 8'hFF` and `mant != 0` |
| `a_is_inf` / `b_is_inf` | `exp == 8'hFF` and `mant == 0` |
| `a_is_zero` / `b_is_zero` | `exp == 8'h00` and `mant == 0` |
| Subnormal (implicit) | `exp == 8'h00` and `mant != 0` |

### Internal signals
- `busy`: operation in progress
- `counter`: FSM stage number (1..7 while busy)
- `a_r`, `b_r`: registered input operands
- `a_s`, `b_s`, `z_s`: sign bits
- `a_e`, `b_e`, `z_e`: signed exponent in *unbiased* domain (10-bit regs, used with `$signed`)
- `a_m`, `b_m`, `z_m`: 24-bit mantissa path (hidden bit inserted when applicable)
- `product`: 50-bit scaled mantissa product
- `guard_bit`, `round_bit`, `sticky`: rounding support bits for RNE
- `special_case`: latch indicating the result is taken from the special path
- `special_z`: precomputed special-case result

---

## FSM / Pipeline Stages

The FSM is controlled by:
- `busy` (operation in progress)
- `counter` (stage number 1..7)

All stage actions are performed inside a single sequential always block using `case(counter)`.

### Stage 1 — Unpack
- Extract mantissas into 24-bit regs (initially `{1'b0, frac}`).
- Convert biased exponent into unbiased form: `exp - 127`.
- Capture signs.

### Stage 2 — Special classification + denormal setup
Checks operand classes using `a_is_nan`, `a_is_inf`, `a_is_zero`, etc. (derived from `a_r` / `b_r`).

If a special case applies, sets `special_case = 1` and latches `special_z` (see [Special-case results](#special-case-results)). The normal multiply path is skipped for later stages.

For the normal path (no special case):
- If exponent is nonzero: set implicit leading 1 (`a_m[23] = 1` / `b_m[23] = 1`).
- If exponent is zero (subnormal): force unbiased exponent to `-126` (subnormal exponent baseline) and leave the hidden bit clear.

### Stage 3 — Input normalization (lightweight)
- Skipped when `special_case` is set.
- If mantissa MSB is not set, shift left once and decrement exponent.
- Used to align subnormal mantissas before multiply; typically a no-op for normal operands.

### Stage 4 — Multiply core
- Skipped when `special_case` is set.
- Compute result sign: `z_s = a_s ^ b_s`
- Exponent add: `z_e = a_e + b_e + 1`
- Mantissa product: `product = a_m * b_m * 4`
  - The `*4` scaling aligns the product for extraction into `{z_m, G, R, S}`.

### Stage 5 — Extract mantissa + rounding bits
- Skipped when `special_case` is set.
- `z_m = product[49:26]`
- `guard_bit = product[25]`
- `round_bit = product[24]`
- `sticky = OR(product[23:0])`

### Stage 6 — Normalize + Round-to-Nearest-Even (RNE)
Skipped when `special_case` is set. Otherwise performs:
1. **Underflow alignment** toward exponent `-126`:
   - Computes shift amount `sh = (-126 - z_e)` when `z_e < -126`.
   - Shifts mantissa right and accumulates shifted-out bits into `sticky`.
2. **Normalize** if MSB missing:
   - Left-shifts mantissa while adjusting exponent, carrying guard into LSB.
3. **RNE rounding**:
   - If `G == 1` and `(R || S || LSB)` then increment mantissa.
   - Handles carry-out from rounding:
     - If rounding overflows mantissa, set mantissa to `0x800000` and increment exponent.

### Stage 7 — Pack
- If `special_case`: `z <= special_z`.
- Otherwise (normal path):
  - Pack sign, biased exponent (`z_e + 127`), and fraction `z_m[22:0]`.
  - If `z_e == -126` and `z_m[23] == 0`: force exponent field to `0` (denormal/zero encoding).
  - If `z_e > 127`: output signed infinity (`sign = z_s`, `exp = 0xFF`, `mant = 0`).
- Asserts `out_valid` for one cycle and clears `busy`.

---

## Special-case results

Evaluated in stage 2 (priority order):

| Condition | Result (`special_z`) |
|-----------|----------------------|
| `a` or `b` is NaN | `32'h7FC0_0000` (canonical quiet NaN, sign = 0) |
| `a` is Inf and `b` is zero, or `b` is Inf and `a` is zero | `32'h7FC0_0000` (invalid, quiet NaN) |
| `a` is Inf (and `b` is not zero/NaN) | `{a_s ^ b_s, 8'hFF, 23'd0}` (signed infinity) |
| `b` is Inf (and `a` is not zero/NaN) | `{a_s ^ b_s, 8'hFF, 23'd0}` (signed infinity) |
| `a` or `b` is zero (and not caught above) | `{a_s ^ b_s, 8'd0, 23'd0}` (signed zero) |

Notes:
- NaN payload bits from operands are **not** propagated; all NaN results use `0x7FC00000`.
- Signaling NaN is not distinguished from quiet NaN.
- `Inf × Inf` yields signed infinity (sign = `a_s ^ b_s`).

---

## Supported input classes

| Class | Supported | Path |
|-------|-----------|------|
| Normal (`exp` in `1..254`) | Yes | Stages 1–7 normal multiply + RNE |
| Subnormal (`exp = 0`, `mant ≠ 0`) | Yes | Hidden-bit setup, input normalize, normal multiply path |
| Zero (`exp = 0`, `mant = 0`) | Yes | Special-case signed zero |
| Infinity (`exp = 0xFF`, `mant = 0`) | Yes | Special-case signed infinity (or NaN if multiplied by zero) |
| NaN (`exp = 0xFF`, `mant ≠ 0`) | Yes | Special-case canonical quiet NaN |

---

## Assumptions & Limitations
- Single outstanding operation (`busy` ignores back-to-back `valid` while busy).
- Not a pipelined multiplier; one multiply every 7+ cycles.
- Special NaN handling is simplified (fixed quiet NaN output, no payload preservation).
- Subnormal **results** rely on the stage 6 underflow shift and stage 7 denormal pack logic; extreme tininess may differ from a reference soft-float in corner cases.

---

## Verification Notes
Recommended testbench behavior for this handshake design:
- Drive `a` / `b` and pulse `valid` **synchronously** on clock edges.
- Wait for `out_valid` before sampling `z`.
- Compare normal operands against a reference model (e.g. Python `struct` / `float` or soft-float) with RNE.
- Add directed tests for NaN, ±Inf, ±0, subnormals, overflow-to-inf, and `Inf × 0 → NaN`.
