"""Tiny PE files with the import tables a test chooses (RFC-041 section 7.3).

The check of the package's DLLs reads import tables, and a test of it needs
binaries whose imports it decides: a real `.pyd` imports whatever its
compiler chose, and differs between machines. These are PE32+ images with one
section, the import directory and the delay-load directory, and nothing a
loader could run. `scripts/pe_imports.py` is the only reader they need to
satisfy.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from pathlib import Path

SECTION_RVA = 0x1000
SECTION_OFFSET = 0x200
OPTIONAL_HEADER_SIZE = 112 + 16 * 8


def make_pe(path: Path, imports: Sequence[str], delayed: Sequence[str] = ()) -> Path:
    """Write a PE file at `path` that imports `imports` and delay-loads `delayed`."""
    names = bytearray()
    name_rvas: list[int] = []
    descriptors_size = 20 * (len(imports) + 1) + 32 * (len(delayed) + 1)
    for name in [*imports, *delayed]:
        name_rvas.append(SECTION_RVA + descriptors_size + len(names))
        names += name.encode("ascii") + b"\0"

    section = bytearray()
    import_rva = SECTION_RVA if imports else 0
    for rva in name_rvas[: len(imports)]:
        section += struct.pack("<IIIII", 0, 0, 0, rva, 0)
    section += bytes(20)
    delay_rva = SECTION_RVA + len(section) if delayed else 0
    for rva in name_rvas[len(imports) :]:
        section += struct.pack("<IIIIIIII", 1, rva, 0, 0, 0, 0, 0, 0)
    section += bytes(32)
    section += names

    dos = bytearray(0x40)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, OPTIONAL_HEADER_SIZE, 0x22)
    optional = bytearray(OPTIONAL_HEADER_SIZE)
    struct.pack_into("<H", optional, 0, 0x20B)
    struct.pack_into("<II", optional, 112 + 1 * 8, import_rva, 20 * len(imports))
    struct.pack_into("<II", optional, 112 + 13 * 8, delay_rva, 32 * len(delayed))
    header = bytes(
        b".idata\0\0"
        + struct.pack("<IIII", len(section), SECTION_RVA, len(section), SECTION_OFFSET)
        + bytes(16)
    )
    image = bytearray(dos + b"PE\0\0" + coff + optional + header)
    image += bytes(SECTION_OFFSET - len(image))
    image += section
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(image))
    return path
