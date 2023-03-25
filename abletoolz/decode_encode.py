"""Encoding and decoding ableton set xml tools."""

import textwrap
from typing import Literal, Tuple, Union, overload


class ParseError(Exception):
    """Error decoding/encoding."""


def xml_to_string(xml_str: str) -> Tuple[str, int]:
    """Strip xml hex data from ableton xml into single string."""
    if not xml_str:
        raise DecodeError(f"No text to parse! {xml_str}")
    levels = xml_str.splitlines()[1].count("\t")
    return xml_str.replace("\t", "").replace(" ", "").replace("\n", ""), levels


@overload
def hex_to_string(hex_str: str, return_bytes: Literal[False] = False) -> str:
    ...


@overload
def hex_to_string(hex_str: str, return_bytes: Literal[True]) -> bytes:
    ...


def hex_to_string(hex_str: str, return_bytes: bool = False) -> Union[str, bytes]:
    """Input is string, but it's actually encoded bytes in hex form.

    Decode hex to actual string representation.
    Format is character, null byte, character, null byte ...
    """
    byte_data = bytearray.fromhex(hex_str)
    if return_bytes:
        return byte_data
    return byte_data.decode("utf-16")


def string_to_hex(in_str: str) -> str:
    """Reverse of hex_to_string, take input string and turn into hex encoded string.

    Two extra Null bytes are at the end of the string(?)
    """
    byte_str = in_str.encode("utf-8")
    prepped = "".join([f"{x:x}00" for x in byte_str]).upper()
    return prepped + "0000"


def string_to_xml(in_str: str, levels: int = 14) -> str:
    """Reverse of xml_to_string, add in tabs and chunk data into 80 character wide block."""
    indent = "\t" * levels
    width = 80 + levels
    parsed = textwrap.wrap(
        in_str, initial_indent=indent, subsequent_indent=indent, break_long_words=True, tabsize=4, width=width
    )
    ending_indent = '\t' * (levels - 1)
    return "\n" + "\n".join(parsed) + f"\n{ending_indent}"
