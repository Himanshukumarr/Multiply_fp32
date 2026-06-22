# fmultiplier — FP32 Multiplier (Handshake, Multi-Cycle)

## What you are building

Complete module **`fmultiplier`** in **`sources/multiply_fp32.sv`**.

- Multi-cycle IEEE-754 **binary32** multiplier: `z = a * b`
- **Valid / out_valid** handshake (one operation at a time)
- **7-cycle** fixed latency from accepted `valid` to `out_valid`
- **Round-to-nearest-even (RNE)** on the normal-number path
- **Synthesizable** SystemVerilog; graded with **Icarus Verilog** + cocotb

**Do not change the module port list.**

---

## What grading checks

The hidden testbench runs **100 random multiplies** and checks **bit-exact** agreement with Python IEEE-754 float math (RNE).

| Required | Not required for pass |
|----------|----------------------|
| Normal operands only (biased exponent **1..254**) | NaN, infinity, zero operands |
| Bit-exact `z` vs reference | Subnormal **results** (those cases are skipped) |
| `out_valid` pulses once when `z` is valid | Back-to-back pipelined throughput |
| Async active-high reset | |

If `out_valid` or `z` are ever undriven (`X` in simulation), grading fails immediately — usually on the first multiply.

---

## Ports

| Port | Dir | Width | Description |
|------|-----|-------|-------------|
| `clk` | in | 1 | Clock |
| `rst` | in | 1 | Async reset (**posedge**, active high) |
| `valid` | in | 1 | 1-cycle start pulse (accepted only when idle) |
| `a` | in | 32 | Operand A (FP32 bits) |
| `b` | in | 32 | Operand B (FP32 bits) |
| `z` | out | 32 | Result (FP32 bits) |
| `out_valid` | out | 1 | 1-cycle pulse when `z` is updated |

---

## Critical rules (read before coding)

1. **Drive outputs every cycle.** After reset, assign `out_valid <= 0` by default each clock. Only pulse `out_valid <= 1` on the completion cycle. Never leave `z` or `out_valid` undriven.

2. **One FSM always block** — `always @(posedge clk or posedge rst)` with a `case (counter)` for stages 1..7.

3. **Start only when idle.** If `!busy && valid` on a posedge: latch `a`/`b` into `a_r`/`b_r`, set `busy <= 1`, set `counter <= 1`. Ignore `valid` while `busy`.

4. **Finish on stage 7.** Update `z`, assert `out_valid` for one cycle, clear `busy`.

5. **Use registered operands** (`a_r`, `b_r`) for all unpack/math — not the live `a`/`b` inputs after the start cycle.

### Recommended always-block shape

```verilog
always @(posedge clk or posedge rst) begin
    if (rst) begin
        // reset busy, counter, out_valid, z, internal regs
    end else begin
        out_valid <= 1'b0;          // DEFAULT every cycle — do not skip this
        if (!busy) begin
            if (valid) begin /* latch a,b; busy<=1; counter<=1 */ end
        end else begin
            case (counter)
                3'd1: /* stage 1 */ ;
                // ... stages 2..7 ...
            endcase
        end
    end
end
```

---

## FSM timing (do not get this wrong)

Let **cycle 0** = the posedge where `valid==1` and `busy==0` (operands latched, `counter` becomes 1, `busy` becomes 1). **Stage 1 does not run on cycle 0** — it runs on the next posedge.

| Cycle | What happens |
|-------|----------------|
| 0 | Latch `a`,`b`; `busy=1`; `counter=1` |
| 1 | Run stage 1 → `counter=2` |
| 2 | Run stage 2 → `counter=3` |
| 3 | Run stage 3 → `counter=4` |
| 4 | Run stage 4 → `counter=5` |
| 5 | Run stage 5 → `counter=6` |
| 6 | Run stage 6 → `counter=7` |
| 7 | Run stage 7: update `z`, `out_valid=1`, `busy=0` |

**`out_valid` must go high on cycle 7** (7 posedges after the start edge).

---

## Suggested internal state

| Signal | Role |
|--------|------|
| `busy` | Operation in progress |
| `counter` | Stage index 1..7 |
| `a_r`, `b_r` | Registered operands |
| `a_s`, `b_s`, `z_s` | Sign bits |
| `a_e`, `b_e`, `z_e` | **Unbiased** signed exponents (10-bit; use `$signed` when comparing) |
| `a_m`, `b_m`, `z_m` | 24-bit mantissa path (hidden bit at bit 23) |
| `product` | Wide mantissa product (50 bits is enough) |
| `guard_bit`, `round_bit`, `sticky` | RNE helper bits |

---

## FP32 field layout

For a 32-bit word: `sign = [31]`, `exp = [30:23]` (biased), `frac = [22:0]`.

**Normal number** (what grading uses): `exp` is 1..254. Value = `(-1)^sign × 1.fraction × 2^(exp-127)`.

---

## Seven pipeline stages (normal path only)

You may **ignore NaN / Inf / zero / subnormal operands** — grading uses normal numbers only.

### Stage 1 — Unpack
From `a_r`, `b_r`:
- `a_s`, `b_s` ← sign bits
- `a_e`, `b_e` ← unbiased exponent: `{exp} - 127` (signed)
- `a_m`, `b_m` ← `{1'b0, frac}` (24 bits; hidden bit **not** set yet)

### Stage 2 — Hidden one
For normal inputs: `a_m[23] <= 1`, `b_m[23] <= 1`.

### Stage 3 — Input align
For normal inputs: **no-op** (mantissa MSB is already 1). Advance counter.

### Stage 4 — Multiply
- `z_s <= a_s ^ b_s`
- `z_e <= a_e + b_e + 1`  (the `+1` accounts for the hidden ones)
- `product <= a_m * b_m * 4`  (scale by 4 so later bit slices line up)

### Stage 5 — Split product into mantissa + G/R/S
After the `×4` scaling, extract:
- `z_m`       ← `product[49:26]`  (24-bit working mantissa)
- `guard_bit` ← `product[25]`
- `round_bit` ← `product[24]`
- `sticky`    ← OR of `product[23:0]` (any remaining low bits)

### Stage 6 — Normalize (if needed) + RNE
**Normalize:** if `z_m[23]==0`, left-shift mantissa once, decrement `z_e`, shift G into LSB of mantissa, advance G←R, R←0.

**RNE increment** when `guard_bit==1` **and** `(round_bit | sticky | z_m[0])`:
- Add 1 to mantissa
- If carry overflows bit 23: set mantissa to `24'h800000` and increment `z_e`

Stage 6 is easiest with **blocking temporaries** inside the clocked always block (Icarus-friendly).

### Stage 7 — Pack + handshake
- `z[31] <= z_s`
- `z[30:23] <= z_e[7:0] + 127`  (re-bias exponent)
- `z[22:0] <= z_m[22:0]`  (drop hidden bit)
- If `$signed(z_e) > 127`: output signed infinity (`exp=8'hFF`, `frac=0`)
- `out_valid <= 1`, `busy <= 0`

---

## Walkthrough: `1.5 × 2.0 = 3.0`

Use this to sanity-check your pipeline (normal-path only):

| Step | Value |
|------|-------|
| `a = 0x3FC00000` | 1.5 — sign 0, biased exp 127, frac = 0.5 |
| `b = 0x40000000` | 2.0 — sign 0, biased exp 128, frac = 0 |
| After unpack | `a_e=0`, `b_e=1`; `a_m=1.1₂`, `b_m=1.0₂` (with hidden bit at [23]) |
| After multiply stage | `z_e = 0+1+1 = 2`; mantissa product scaled by 4 |
| After pack | `z = 0x40400000` (3.0) |

If your design does not produce `0x40400000` for these inputs (after 7 cycles), debug unpack, hidden bit, exponent add, or pack first.

---

## Reset

On `posedge rst`: `busy=0`, `counter=0`, `out_valid=0`, `z=0`, clear internal regs.

---

## Common mistakes

- [ ] `out_valid` or `z` left at `X` — only assigned in stage 7, no default each cycle
- [ ] Latency off by one (6 stages, or `out_valid` on cycle 6)
- [ ] Forgot hidden bit before multiply (`a_m[23]` and `b_m[23]` must be 1)
- [ ] Used live `a`/`b` instead of `a_r`/`b_r` after the start cycle
- [ ] Wrong exponent: subtract 127 on unpack, add 127 on pack
- [ ] Missing `* 4` on product or wrong G/R/S bit indices
- [ ] RNE ties broken wrong (need LSB check for round-to-even)
- [ ] Wasted effort on NaN/Inf/zero — not needed for grading
- [ ] SVA or `#delay` in RTL — Icarus will reject or mis-simulate

---

## Self-check

Write your own cocotb/pytest testbench. Compare `z` against Python:

```python
import struct
def mul_bits(a, b):
    fa = struct.unpack('<f', struct.pack('<I', a))[0]
    fb = struct.unpack('<f', struct.pack('<I', b))[0]
    return struct.unpack('<I', struct.pack('<f', fa * fb))[0]
```

Test normal operands with biased exponent 1..254. Pulse `valid` when idle; wait for `out_valid`; sample `z` on that cycle.

---

## Tooling constraints

- Simulator: **Icarus Verilog** (`iverilog` / `vvp`)
- No SVA (property/sequence) syntax
- No `#delay` in synthesizable logic
- Module name: **`fmultiplier`**; file: **`sources/multiply_fp32.sv`**
