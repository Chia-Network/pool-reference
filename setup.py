import os
import sys

import setuptools
from setuptools import setup


# Utility function to read the README file.
# Used for the long_description.  It's nice, because now 1) we have a top level
# README file and 2) it's easier to type in the README file than to put a raw
# string in below ...
def read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()


dependencies = [
    "chia-blockchain==2.4.4",
    "chia_rs>=0.5.2",
    "setuptools>=56.1,<75.7",
    "aiosqlite==0.20.0",
    "aiohttp==3.10.11",
    "pytest==8.3.4",
    "PyMySQL==1.1.1",
]

dev_dependencies = [
    "types-aiofiles==24.1.0.20240626",
    "types-pyyaml==6.0.12.20240917",
    "types-setuptools==75.6.0.20241126",
    "types-PyMySQL==1.1.0.20241103",
]

kwargs = dict(
    name="chia-pool-reference",
    version="1.2",
    author="Mariano Sorgente",
    author_email="mariano@chia.net",
    description=("A reference pool for the Chia blockchain."),
    license="Apache-2.0",
    packages=setuptools.find_packages(),
    install_requires=dependencies,
    extras_require=dict(
        dev=dev_dependencies,
    ),
    long_description=read("README.md"),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Topic :: Utilities",
        "License :: OSI Approved :: BSD License",
    ],
)

if "setup_file" in sys.modules:
    # include dev deps in regular deps when run in snyk
    dependencies.extend(dev_dependencies)

setup(**kwargs)  # type: ignore
