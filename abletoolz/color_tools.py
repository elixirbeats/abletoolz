"""Tools for dealing with ableton colors, creating gradients etc."""
import logging
import random
from typing import Final, List, Optional, Tuple

import numpy as np
from colormath.color_conversions import convert_color
from colormath.color_objects import LabColor, sRGBColor

logger = logging.getLogger(__name__)
# These are the hex values of the drop down color menu, matching the exact layout, hence the lines being too long.
# yapf: disable
ableton_colors_strs: Final = [
    "#FF94A6", "#FFA428", "#CD9827", "#F6F57C", "#BEFA00", "#21FF41", "#25FEA9", "#5DFFE9", "#8AC5FE", "#5480E4", "#93A6FF", "#D86CE4", "#E552A1", "#FFFEFE",
    "#FE3637", "#F66D02", "#99734A", "#FEF134", "#87FF67", "#3DC201", "#01BEAF", "#18E9FE", "#10A4EE", "#007DC0", "#886CE4", "#B776C6", "#FE38D4", "#D1D0D1",
    "#E3665A", "#FEA274", "#D2AD70", "#EDFFAE", "#D3E499", "#BAD175", "#9AC58D", "#D4FCE0", "#CCF0F8", "#B8C1E2", "#CDBBE4", "#AF98E4", "#E5DDE0", "#A9A8A8",
    "#C6938A", "#B68257", "#98826A", "#BEBB69", "#A6BE00", "#7CB04C", "#89C3BA", "#9BB3C4", "#84A5C3", "#8392CD", "#A494B5", "#BF9FBE", "#BD7096", "#7B7A7A",
    "#AF3232", "#A95131", "#734E41", "#DAC200", "#84971F", "#529E31", "#0A9C8E", "#236285", "#1A2F96", "#2E52A3", "#624BAD", "#A24AAD", "#CD2E6F", "#FFFEFE",
]
# yapf: enable
ableton_colors = [int(x.strip("#"), 16) for x in ableton_colors_strs]
colors_indexed = [(i, x) for i, x in enumerate(ableton_colors)]
# TODO: Build up this list with good results to re-use.
known_good_combos = {
    "light_blue_to_dark_pink": (0x5DFFE9, 0xE552A1),
    "pink_to_yellow": (0xD86CE4, 0xFEF134),
    "yellow_green_to_dark_green": (0xBEFA00, 0x3DC201),
}


def hex_to_rgb(hex_value: int) -> Tuple[int, int, int]:
    """Convert hex to rgb."""
    return ((hex_value >> 16) & 0xFF, (hex_value >> 8) & 0xFF, hex_value & 0xFF)


def rgb_to_hex(r: int, g: int, b: int) -> int:
    """Convert rgb values to hex integer."""
    return (r << 16) + (g << 8) + b


def interpolate_color(color1: int, color2: int, t: float) -> int:
    """Find color inbetween two given."""
    r1, g1, b1 = hex_to_rgb(color1)
    r2, g2, b2 = hex_to_rgb(color2)
    r = int(r1 * (1 - t) + r2 * t)
    g = int(g1 * (1 - t) + g2 * t)
    b = int(b1 * (1 - t) + b2 * t)
    return rgb_to_hex(r, g, b)


def create_gradient(color1: int, color2: int, steps: int) -> List[int]:
    """Generate a color gradient between two colors."""
    return [interpolate_color(color1, color2, t / (steps - 1)) for t in range(steps)]


def hex_to_lab_color(hex_value: int) -> LabColor:
    """Convert hex value to colormath lab color."""
    r, g, b = hex_to_rgb(hex_value)
    rgb_color = sRGBColor(r, g, b, is_upscaled=True)
    lab_color = convert_color(rgb_color, LabColor)
    return lab_color


def custom_delta_e_cie2000(color1: LabColor, color2: LabColor) -> float:
    """Re-implementation of colormath function that is currently broken due to using deprecated numpy methods."""
    c_1 = np.sqrt(color1.lab_a**2 + color1.lab_b**2)
    c_2 = np.sqrt(color2.lab_a**2 + color2.lab_b**2)

    c_mean = (c_1 + c_2) / 2
    g = 0.5 * (1 - np.sqrt(c_mean**7 / (c_mean**7 + 25**7)))

    a1_prime = (1 + g) * color1.lab_a
    a2_prime = (1 + g) * color2.lab_a

    c1_prime = np.sqrt(a1_prime**2 + color1.lab_b**2)
    c2_prime = np.sqrt(a2_prime**2 + color2.lab_b**2)

    h1_prime = np.rad2deg(np.arctan2(color1.lab_b, a1_prime))
    h2_prime = np.rad2deg(np.arctan2(color2.lab_b, a2_prime))
    h1_prime += 360 * (h1_prime < 0)
    h2_prime += 360 * (h2_prime < 0)

    delta_l = color2.lab_l - color1.lab_l
    delta_c = c2_prime - c1_prime
    delta_h = h2_prime - h1_prime

    delta_h -= 360 * (delta_h > 180)
    delta_h += 360 * (delta_h < -180)

    delta_h_bar = np.abs(h1_prime - h2_prime)
    delta_h_bar -= 360 * (delta_h_bar > 180)
    delta_h_bar += 360 * (delta_h_bar < -180)

    h_bar = (h1_prime + h2_prime) / 2
    h_bar += 360 * (h_bar < 0)
    h_bar -= 360 * (h_bar > 180)

    t = (
        1
        - 0.17 * np.cos(np.deg2rad(h_bar - 30))
        + 0.24 * np.cos(np.deg2rad(2 * h_bar))
        + 0.32 * np.cos(np.deg2rad(3 * h_bar + 6))
        - 0.20 * np.cos(np.deg2rad(4 * h_bar - 63))
    )

    delta_h_prime = 2 * np.sqrt(c1_prime * c2_prime) * np.sin(np.deg2rad(delta_h_bar) / 2)
    sl = 1 + (0.015 * (color1.lab_l - 50) ** 2) / np.sqrt(20 + (color1.lab_l - 50) ** 2)
    sc = 1 + 0.045 * c_mean
    sh = 1 + 0.015 * c_mean * t
    rt = (
        -2
        * np.sqrt(c_mean**7 / (c_mean**7 + 25**7))
        * np.sin(np.deg2rad(60 * np.exp(-(((h_bar - 275) / 25) ** 2))))
    )

    delta_e = np.sqrt(
        (delta_l / sl) ** 2
        + (delta_c / sc) ** 2
        + (delta_h_prime / sh) ** 2
        + rt * (delta_c / sc) * (delta_h_prime / sh)
    )
    return delta_e


def find_closest_color_ciede2000(color: int, palette: List[Tuple[int, int]]) -> Tuple[int, int]:
    """Find closest color using ciede2000."""
    lab_color = hex_to_lab_color(color)
    closest_color = min(palette, key=lambda x: custom_delta_e_cie2000(lab_color, hex_to_lab_color(x[1])))
    return closest_color


def find_furthest_color_ciede2000(color: int, palette: List[Tuple[int, int]]) -> Tuple[int, int]:
    """Find furthest color using ciede2000."""
    lab_color = hex_to_lab_color(color)
    furthest_color = max(palette, key=lambda x: custom_delta_e_cie2000(lab_color, hex_to_lab_color(x[1])))
    return furthest_color


def create_gradient_from_palette_ciede2000(
    color1: int, color2: int, steps: int, palette: List[int]
) -> List[Tuple[int, int]]:
    """Generate a gradient between two colors, and use ciede2000 to find the closest available from a palette."""
    gradient = create_gradient(color1, color2, steps)
    indexed_palette = [(index, color) for index, color in enumerate(palette)]
    return [find_closest_color_ciede2000(color, indexed_palette) for color in gradient]


def sort_colors_ciede2000(colors: List[int], start_color_index: int) -> List[Tuple[int, int]]:
    """Sort colors based on the ciede2000 algorithm which is more accurate to human perception."""
    start_color = colors[start_color_index]
    sorted_colors = [(start_color_index, start_color)]
    remaining_colors = [(index, color) for index, color in enumerate(colors) if index != start_color_index]

    while remaining_colors:
        last_color = sorted_colors[-1][1]
        closest_color_index, closest_color = find_closest_color_ciede2000(last_color, remaining_colors)
        sorted_colors.append((closest_color_index, closest_color))
        remaining_colors.remove((closest_color_index, closest_color))

    return sorted_colors


def create_gradient_ableton(
    num_items: int, starting_color: Optional[int] = None, starting_index: Optional[int] = None
) -> List[int]:
    """Makes gradients for ableton tracks/clips.

    Since there are only 70 colors, I try to find the furthest color from the first one, which can be
    random or selected. If the number of items is too short, this will be a large jump so set min to 5.

    Returns the ableton color index, which is the index of the array defined at the top of this file. It maps to
    the colors in the ableton menu.
    """
    num_items = max(10, num_items)
    if starting_index is not None:
        color_1 = ableton_colors[starting_index]
    else:
        color_1 = starting_color if starting_color is not None else random.sample(ableton_colors, 1)[0]

    color_2 = find_furthest_color_ciede2000(color_1, colors_indexed)[1]
    natural_grad = create_gradient_from_palette_ciede2000(color_1, color_2, num_items, ableton_colors)
    return [c[0] for c in natural_grad]
