# fmultiplier — FP32 Multiplier (Handshake, Multi-Cycle, IEEE-754)

## Overview

`fmultiplier` is a **multi-cycle** single-precision floating-point multiplier that accepts one operation at a time using a **valid/out_valid** handshake. It runs a 7-stage FSM pipeline and produces a 32-bit IEEE-754 binary32 result.

Behavior: `z = a * b` where `a`, `b`, and `z` are single-precision IEEE-754 binary32 values.

---

## What The Test Actually Checks — Read First

The hidden testbench runs **100 random normal FP32 multiplications** and checks for bit-exact results.

- Operands are **normal numbers only**: exponent field in `[1, 254]`, random mantissa.
- **NaN and Infinity operands are NOT generated** by default.
- If the expected result would be subnormal, that test case is skipped.
- Tolerance: **zero** — your result must match Python's `float` multiply bit-for-bit.

**Implication:** The test does not exercise NaN, Inf, or zero inputs. You must get the **normal-number path** (stages 1–7) 100% correct. Getting special-case handling wrong will not cause failures, but getting the normal path wrong will fail every test.

---

## Critical Implementation Notes

Read these before writing any code. They are the most common causes of failure:

### 1. out_valid default — REQUIRED
```verilog
always @(posedge clk or posedge rst) begin
    if (rst) begin
        ...
        out_valid <= 1'b0;
    end else begin
        out_valid <= 1'b0;   // ← MUST be first line in the else block
        ...
    end
end
```
`out_valid` must be driven `0` every cycle by default. Only raise it to `1` in stage 7. If it is ever left unassigned the test crashes immediately with `Logic('X')`.

### 2. Async reset syntax
```verilog
always @(posedge clk or posedge rst) begin
    if (rst) begin
        // reset all registers here
    end else begin
        // normal clock logic
    end
end
```

### 3. Stage 6 uses local variables
Inside `case(counter)`, stage 6 is complex enough that you should use local procedural variables (declared with `reg` inside the `begin...end` block). This is valid Verilog and avoids accidentally latching intermediate values:
```verilog
3'd6: begin
    if (!special_case) begin
        reg [23:0] zm_tmp;
        reg  [9:0] ze_tmp;
        reg        g_tmp, r_tmp, s_tmp;
        // ... compute using zm_tmp, ze_tmp
        z_m <= zm_tmp;
        z_e <= ze_tmp;
    end
    counter <= 3'd7;
end
```

### 4. Self-test before finishing
After writing the RTL, run:
```
cd /home/ubuntu/example-verilog-codebase
python -m pytest tests/ -v 2>&1 | tail -30
```
Fix all failures. Only stop when you see `1 passed`.

---

## Interface

### Ports
| Port | Dir | Width | Description |
|------|-----|-------|-------------|
| `clk`   | in | 1 | Clock |
| `rst`   | in | 1 | Async reset (posedge, active-high) |
| `valid` | in | 1 | 1-cycle start pulse; accepted only when not busy |
| `a`     | in | 32 | Operand A (FP32 bits) |
| `b`     | in | 32 | Operand B (FP32 bits) |
| `z`         | out | 32 | Result (FP32 bits) |
| `out_valid` | out | 1 | 1-cycle pulse when `z` is valid |

### Handshake
- When `busy == 0`: a high `valid` starts an operation. `a` and `b` are registered into `a_r`, `b_r`. The FSM begins at `counter = 1`.
- While `busy == 1`: new `valid` pulses are ignored.
- When the operation completes at stage 7: `z` is updated, `out_valid` pulses high for exactly 1 cycle, `busy` is cleared.

---

## Internal Registers

Declare these registers:
```verilog
reg [2:0] counter;        // FSM stage: 1..7 while busy
reg       busy;

reg [31:0] a_r, b_r;     // registered input operands

reg [23:0] a_m, b_m, z_m;  // 24-bit mantissa path (bit 23 = implicit hidden bit)
reg  [9:0] a_e, b_e, z_e;  // 10-bit signed unbiased exponents (use $signed())
reg        a_s, b_s, z_s;  // sign bits

reg [49:0] product;          // scaled mantissa product

reg guard_bit, round_bit, sticky;

reg        special_case;
reg [31:0] special_z;
```

Declare these wires from `a_r`/`b_r` (used in stage 2 classification):
```verilog
wire [7:0]  expA = a_r[30:23];
wire [7:0]  expB = b_r[30:23];
wire [22:0] manA = a_r[22:0];
wire [22:0] manB = b_r[22:0];

wire a_is_nan  = (expA == 8'hFF) && (manA != 0);
wire b_is_nan  = (expB == 8'hFF) && (manB != 0);
wire a_is_inf  = (expA == 8'hFF) && (manA == 0);
wire b_is_inf  = (expB == 8'hFF) && (manB == 0);
wire a_is_zero = (expA == 8'h00) && (manA == 0);
wire b_is_zero = (expB == 8'h00) && (manB == 0);
```

---

## FSM Structure

```verilog
always @(posedge clk or posedge rst) begin
    if (rst) begin
        counter <= 3'd0; busy <= 0; out_valid <= 0;
        // clear all other regs ...
    end else begin
        out_valid <= 1'b0;  // default every cycle

        if (!busy) begin
            counter <= 3'd0;
            if (valid) begin
                busy    <= 1'b1;
                counter <= 3'd1;
                a_r <= a; b_r <= b;
                special_case <= 0; special_z <= 0;
                product <= 0; guard_bit <= 0; round_bit <= 0; sticky <= 0;
            end
        end else begin
            case (counter)
                3'd1: begin /* Stage 1 */ counter <= 3'd2; end
                3'd2: begin /* Stage 2 */ counter <= 3'd3; end
                3'd3: begin /* Stage 3 */ counter <= 3'd4; end
                3'd4: begin /* Stage 4 */ counter <= 3'd5; end
                3'd5: begin /* Stage 5 */ counter <= 3'd6; end
                3'd6: begin /* Stage 6 */ counter <= 3'd7; end
                3'd7: begin /* Stage 7 */
                    busy <= 0; out_valid <= 1; counter <= 3'd0;
                end
                default: begin busy <= 0; counter <= 3'd0; end
            endcase
        end
    end
end
```

---

## Pipeline Stages

### Stage 1 — Unpack
Extract fields from registered operands `a_r` and `b_r`:
```verilog
a_m <= {1'b0, a_r[22:0]};          // 24-bit mantissa, hidden bit clear initially
b_m <= {1'b0, b_r[22:0]};
a_e <= {2'b00, a_r[30:23]} - 10'd127;   // unbiased exponent (10-bit signed)
b_e <= {2'b00, b_r[30:23]} - 10'd127;
a_s <= a_r[31];
b_s <= b_r[31];
```

### Stage 2 — Special Classification + Denormal Setup
**For special cases** (NaN, Inf, zero): set `special_case = 1` and latch `special_z` (see [Special-case results](#special-case-results)).

**For the normal path** (no special case — this is what the test exercises):
- If exponent is nonzero: set implicit hidden bit — `a_m[23] <= 1`, `b_m[23] <= 1`.
- If exponent is zero (subnormal): force unbiased exponent to `-126` and leave hidden bit = 0.
```verilog
// Normal path:
if (expA != 8'h00) a_m[23] <= 1'b1;
else               a_e <= -10'sd126;
if (expB != 8'h00) b_m[23] <= 1'b1;
else               b_e <= -10'sd126;
```

### Stage 3 — Input Normalization
Skip if `special_case`. For subnormal inputs, shift mantissa left once if MSB is not set:
```verilog
if (!special_case) begin
    if (!a_m[23]) begin a_m <= a_m << 1; a_e <= a_e - 10'sd1; end
    if (!b_m[23]) begin b_m <= b_m << 1; b_e <= b_e - 10'sd1; end
end
```
For normal operands (which is what the test uses), `a_m[23]` is already 1 and this stage is a no-op.

### Stage 4 — Compute Sign, Exponent, Product
Skip if `special_case`.
```verilog
z_s     <= a_s ^ b_s;
z_e     <= a_e + b_e + 10'sd1;    // +1 is required — see note below
product <= a_m * b_m * 50'd4;
```

> **Why `+1` in the exponent?**
> The mantissas include the implicit bit at position 23, so each is in [2^23, 2^24).
> Their product lands in [2^46, 2^48). The `*4` scales it to [2^48, 2^50).
> Extracting bits [49:26] as `z_m` effectively divides by 2^26, leaving an exponent
> correction of 50 - 26 - 24 = 0 raw, but accounting for the bias removal and
> implicit-bit position, the net correction is `+1`. **Do not omit it.**

### Stage 5 — Extract Mantissa and Rounding Bits
Skip if `special_case`.
```verilog
z_m       <= product[49:26];          // 24-bit mantissa
guard_bit <= product[25];
round_bit <= product[24];
sticky    <= (product[23:0] != 0);
```

### Stage 6 — Normalize and Round-to-Nearest-Even (RNE)
Skip if `special_case`. Use local procedural variables to avoid latching issues:

```verilog
3'd6: begin
    if (!special_case) begin
        reg [23:0] zm_tmp;
        reg  [9:0] ze_tmp;
        reg        g_tmp, r_tmp, s_tmp;
        reg [24:0] inc;
        integer    sh, k;
        reg        lost_any;

        zm_tmp = z_m;
        ze_tmp = z_e;
        g_tmp  = guard_bit;
        r_tmp  = round_bit;
        s_tmp  = sticky;

        // Step A: Underflow shift — align to exponent -126
        if ($signed(ze_tmp) < -126) begin
            sh = -126 - $signed(ze_tmp);
            lost_any = 1'b0;
            if (sh >= 24) begin
                if (zm_tmp != 0) lost_any = 1'b1;
                zm_tmp = 24'd0;
            end else begin
                for (k = 0; k < 24; k = k + 1)
                    if (k < sh && zm_tmp[k]) lost_any = 1'b1;
                zm_tmp = zm_tmp >> sh;
            end
            if (g_tmp) lost_any = 1'b1;
            if (r_tmp) lost_any = 1'b1;
            s_tmp  = s_tmp | lost_any;
            ze_tmp = -10'sd126;
            g_tmp  = 1'b0;
            r_tmp  = 1'b0;
        end
        // Step B: Normalize — at most ONE left shift (not a loop)
        else if (zm_tmp[23] == 1'b0) begin
            ze_tmp = ze_tmp - 10'sd1;
            zm_tmp = {zm_tmp[22:0], g_tmp};
            g_tmp  = r_tmp;
            r_tmp  = 1'b0;
        end

        // Step C: Round-to-Nearest-Even (RNE)
        if (g_tmp && (r_tmp | s_tmp | zm_tmp[0])) begin
            inc = {1'b0, zm_tmp} + 25'd1;
            if (inc[24]) begin
                zm_tmp = 24'h800000;
                ze_tmp = ze_tmp + 10'sd1;
            end else begin
                zm_tmp = inc[23:0];
            end
        end

        z_m       <= zm_tmp;
        z_e       <= ze_tmp;
        guard_bit <= g_tmp;
        round_bit <= r_tmp;
        sticky    <= s_tmp;
    end
    counter <= 3'd7;
end
```

> **Why only one shift in Step B?**
> The product of two normalized 1.xxx values always lands in [1.0, 4.0).
> After the `*4` scaling and bit extraction, `z_m[23]` is almost always 1.
> The only case where it is 0 is when the product is in [1.0, 2.0) — exactly one
> left shift restores normalization. A multi-shift loop is not needed.

### Stage 7 — Pack Result
```verilog
3'd7: begin
    if (special_case) begin
        z <= special_z;
    end else begin
        z[31]    <= z_s;
        z[30:23] <= z_e[7:0] + 8'd127;
        z[22:0]  <= z_m[22:0];

        // Denormal/zero result
        if (($signed(z_e) == -126) && (z_m[23] == 1'b0))
            z[30:23] <= 8'd0;

        // Overflow to infinity
        if ($signed(z_e) > 127) begin
            z[31]    <= z_s;
            z[30:23] <= 8'hFF;
            z[22:0]  <= 23'd0;
        end
    end
    busy      <= 1'b0;
    out_valid <= 1'b1;
    counter   <= 3'd0;
end
```

---

## Special-case Results

Evaluated in stage 2 (priority order). **These paths are not tested by the default test configuration**, but are included for completeness.

| Condition | Result (`special_z`) |
|-----------|----------------------|
| `a` or `b` is NaN | `32'h7FC0_0000` (canonical quiet NaN) |
| `a` is Inf and `b` is zero (or vice versa) | `32'h7FC0_0000` (Inf×0 = NaN) |
| `a` is Inf (and `b` is not zero/NaN) | `{a_s ^ b_s, 8'hFF, 23'd0}` (signed infinity) |
| `b` is Inf (and `a` is not zero/NaN) | `{a_s ^ b_s, 8'hFF, 23'd0}` (signed infinity) |
| `a` or `b` is zero (not caught above) | `{a_s ^ b_s, 8'd0, 23'd0}` (signed zero) |

---

## Latency and Throughput

- Fixed latency: **7 stages** (counter 1 through 7).
- `out_valid` asserts at the end of stage 7, which is **7 clock cycles after the rising edge where `valid` was sampled**.
- Not pipelined — one operation every 7+ cycles.

---

## Assumptions and Limitations

- Single outstanding operation (`busy` ignores back-to-back `valid`).
- Subnormal **results** rely on stage 6 underflow shift and stage 7 denormal encoding; these cases are excluded from the test.
- NaN payload bits from operands are not propagated; all NaN results use `0x7FC00000`.
