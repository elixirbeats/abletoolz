"""Terminal colors for log lines: the short names every module prints with."""

import sys

import colorama

colorama.init(autoreset=True)

RST = colorama.Fore.RESET
if sys.platform == "win32":
    BOLD = "[1m"  # pylint: disable=invalid-character-esc
    # Windows terminal colors are hard to see, use bright.
    R = colorama.Fore.LIGHTRED_EX
    G = colorama.Fore.LIGHTGREEN_EX
    B = colorama.Fore.LIGHTBLUE_EX
    Y = colorama.Fore.LIGHTYELLOW_EX
    C = colorama.Fore.LIGHTCYAN_EX
    M = colorama.Fore.LIGHTMAGENTA_EX
else:
    BOLD = "\033[1m"
    R = colorama.Fore.RED
    G = colorama.Fore.GREEN
    B = colorama.Fore.BLUE
    Y = colorama.Fore.YELLOW
    C = colorama.Fore.CYAN
    M = colorama.Fore.MAGENTA

CB = C + colorama.Style.BRIGHT + BOLD
