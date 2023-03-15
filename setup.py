import pathlib
import re

from setuptools import setup

_DIR = pathlib.Path(__file__).parent

with (_DIR / "abletoolz/__init__.py").open() as f:
    version = re.search('__version__ = "(.*?)"', f.read()).group(1)


def get_requirements() -> str:
    """Get requirements string."""
    with (_DIR / "requirements.txt").open() as f:
        return f.read()


setup(
    name="abletoolz",
    version=version,
    packages=["abletoolz", "abletoolz.sample_databaser"],
    package_dir={"abletoolz": "abletoolz"},
    classifiers=[
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Utilities",
    ],
    author="ElixirBeats",
    url="https://github.com/elixirbeats/abletoolz",
    license="GNU GENERAL PUBLIC LICENSE",
    entry_points={"console_scripts": ["abletoolz=abletoolz:main"]},
    long_description=(_DIR / "README.md").read_text().strip(),
    long_description_content_type="text/markdown",
    description="Tools made for editing, fixing and analyzing Ableton Live sets.",
    install_requires=get_requirements(),
    include_package_data=True,
    python_requires=">=3.8",
)
