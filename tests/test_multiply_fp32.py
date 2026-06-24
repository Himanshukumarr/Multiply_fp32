from __future__ import annotations

import os
import random
import math
import struct
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from cocotb_tools.runner import get_runner


def u32_to_f32(u: int) -> float:
    return struct.unpack("<f", struct.pack("<I", u & 0xFFFFFFFF))[0]


def f32_to_u32(x: float) -> int:
    try:
        return struct.unpack("<I", struct.pack("<f", float(x)))[0]
    except OverflowError:
        sign = 1 if math.copysign(1.0, x) < 0 else 0
        return (sign << 31) | (0xFF << 23)  # +/-inf


def f32_round(x: float) -> float:
    return u32_to_f32(f32_to_u32(x))


def fp32_mul_ref_bits(a_u32: int, b_u32: int) -> int:
    a = u32_to_f32(a_u32)
    b = u32_to_f32(b_u32)
    return f32_to_u32(f32_round(a * b))


def fmt_u32(u: int) -> str:
    return f"0x{u & 0xFFFFFFFF:08x}"


def is_nan_u32(u: int) -> bool:
    exp = (u >> 23) & 0xFF
    mant = u & 0x7FFFFF
    return exp == 0xFF and mant != 0

def is_subnormal_or_zero_u32(u: int) -> bool:
    exp = (u >> 23) & 0xFF
    mant = u & 0x7FFFFF
    return exp == 0 and mant != 0 or (exp == 0 and mant == 0)

def rand_fp32_normal_bits() -> int:
    sign = random.getrandbits(1)
    exp = random.randint(1, 254)
    mant = random.getrandbits(23)
    return (sign << 31) | (exp << 23) | mant


def rand_fp32_inf_bits() -> int:
    sign = random.getrandbits(1)
    return (sign << 31) | (0xFF << 23) 


def rand_fp32_nan_bits(quiet_only: bool = True) -> int:
    sign = random.getrandbits(1)
    mant = random.getrandbits(23)
    mant |= 1  # ensure mant != 0
    if quiet_only:
        mant |= (1 << 22)  # force QNaN
    return (sign << 31) | (0xFF << 23) | mant


def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return default
    return float(v)


def rand_fp32_bits(
    allow_nan: bool,
    allow_inf: bool,
    special_rate: float,
    quiet_nan_only: bool,
) -> int:
    """
    Generate FP32 operand bits.
    With probability special_rate, choose NaN/Inf (depending on allow_* flags).
    Otherwise choose a normal.
    """
    do_special = (random.random() < special_rate) and (allow_nan or allow_inf)
    if not do_special:
        return rand_fp32_normal_bits()

    choices = []
    if allow_nan:
        choices.append("nan")
    if allow_inf:
        choices.append("inf")

    pick = random.choice(choices)
    if pick == "nan":
        return rand_fp32_nan_bits(quiet_only=quiet_nan_only)
    else:
        return rand_fp32_inf_bits()


async def start_and_wait(dut, a_u32: int, b_u32: int, timeout_cycles: int = 50) -> int:
    await RisingEdge(dut.clk)
    dut.a.value = a_u32
    dut.b.value = b_u32

    dut.valid.value = 1
    await RisingEdge(dut.clk)
    dut.valid.value = 0

    for _ in range(timeout_cycles):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) == 1:
            return int(dut.z.value) & 0xFFFFFFFF

    raise AssertionError("Timeout waiting for out_valid")

def is_subnormal_u32(u: int) -> bool:
    exp = (u >> 23) & 0xFF
    mant = u & 0x7FFFFF
    return exp == 0 and mant != 0

def is_zero_u32(u: int) -> bool:
    return (u & 0x7FFFFFFF) == 0

@cocotb.test()
async def fmultiplier_random_fp32(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    clock.start(start_high=False)

    ALLOW_NAN = env_bool("ALLOW_NAN", False)  # by default, set to false
    ALLOW_INF = env_bool("ALLOW_INF", False)  # if IA goes well, set to true
    SPECIAL_RATE = env_float("SPECIAL_RATE", 0.02)  # 2% of operands special by default
    QUIET_NAN_ONLY = env_bool("QUIET_NAN_ONLY", True)

    # Reset
    dut.rst.value = 1
    dut.valid.value = 0
    dut.a.value = 0
    dut.b.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)

    N = 100
    MAX_TRIES = 50

    for i in range(N):
        for _try in range(MAX_TRIES):
            a_u32 = rand_fp32_bits(ALLOW_NAN, ALLOW_INF, SPECIAL_RATE, QUIET_NAN_ONLY)
            b_u32 = rand_fp32_bits(ALLOW_NAN, ALLOW_INF, SPECIAL_RATE, QUIET_NAN_ONLY)

            exp_u32 = fp32_mul_ref_bits(a_u32, b_u32)

            # subnormal expected outputs are excluded
            if is_subnormal_u32(exp_u32):
                continue

            break
        else:
            raise AssertionError(
                f"[{i}] Could not find non-subnormal expected result after {MAX_TRIES} tries"
            )

        got_u32 = await start_and_wait(dut, a_u32, b_u32, timeout_cycles=100)

        dut._log.info(
            f"[{i}] a={fmt_u32(a_u32)} b={fmt_u32(b_u32)} "
            f"exp={fmt_u32(exp_u32)} got={fmt_u32(got_u32)} "
            f"exp_nan={is_nan_u32(exp_u32)} got_nan={is_nan_u32(got_u32)} "
            f"exp_sub={is_subnormal_u32(exp_u32)}"
        )

        # NaN compare: any NaN payload/sign accepted if both are NaN
        if is_nan_u32(exp_u32) and is_nan_u32(got_u32):
            continue

        assert got_u32 == exp_u32, (
            f"[{i}] mismatch\n"
            f"  a={fmt_u32(a_u32)} b={fmt_u32(b_u32)}\n"
            f"  exp={fmt_u32(exp_u32)} got={fmt_u32(got_u32)}"
        )

def test_fmultiplier_runner():
    sim = os.getenv("SIM", "icarus")
    proj_path = Path(__file__).resolve().parent.parent

    sources = [proj_path / "sources" / "multiply_fp32.sv"]

    runner = get_runner(sim)
    runner.build(
        sources=sources,
        hdl_toplevel="fmultiplier",
        always=True,
    )
    runner.test(
        hdl_toplevel="fmultiplier",
        test_module="test_multiply_fp32",
    )