"""Pro-L 1 into Pro-L 2, each control by what both plugins print.

Real patches, restored in the original x86 Pro-L, and their conversions, restored in Pro-L 2, printed the same gain,
lookahead, attack, release, stereo links and output level within the original's rounding, the same style and
switches, and true-peak limiting off.
"""

from __future__ import annotations

import pytest

from abletoolz.plugins.converters import fabfilter as conversion

TABLE = conversion._table("pro-l-1-to-pro-l-2.json")
NAMES: list[str] = TABLE["source_controls"]
TARGETS: list[str] = TABLE["target_controls"]
# Pro-L's own defaults, in control order.
DEFAULT = [0.0, 1.0, 0.05, 0.3976, 0.4177, 0.6, 1.0, 0.0, 0.0, 0.6667, 0.0, 1.0, 1.0, 0.0, 0.2, 0.0, 1.0, 1.0, 0.0]


def converted(**changes: float) -> dict[str, float]:
    bank = list(DEFAULT)
    for name, value in changes.items():
        bank[NAMES.index(name.replace("_", " "))] = value
    return dict(zip(TARGETS, conversion.pro_l1_to_pro_l2_parameters(bank, []), strict=True))


@pytest.mark.parametrize("source", sorted(TABLE["numbers"]))
@pytest.mark.parametrize("position", [0.1, 0.37, 0.5, 0.83])
def test_each_number_is_written_where_pro_l_2_prints_what_pro_l_printed(source: str, position: float) -> None:
    pairing = TABLE["numbers"][source]
    wanted = conversion._printed(pairing["source"], position)
    bank = list(DEFAULT)
    bank[NAMES.index(source)] = position
    written = dict(zip(TARGETS, conversion.pro_l1_to_pro_l2_parameters(bank, []), strict=True))[pairing["to"]]
    assert conversion._printed(pairing["target"], written) == pytest.approx(wanted, rel=1e-3, abs=1e-3)


@pytest.mark.parametrize(("value", "style"), [(0.0, 0.0), (1 / 3, 1 / 7), (2 / 3, 2 / 7), (1.0, 3 / 7)])
def test_the_four_styles_are_the_first_four_of_pro_l_2_s(value: float, style: float) -> None:
    """Transparent, Punchy, Dynamic and Allround keep their names in Pro-L 2's eight."""
    assert converted(Style=value)["Style"] == pytest.approx(style)


def test_true_peak_limiting_is_off_because_pro_l_limits_sample_peaks() -> None:
    assert converted()["True Peak Limiting"] == 0.0
