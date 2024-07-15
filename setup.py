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
    "chia-blockchain==2.0.0",
    "blspy==2.0.3",
    "setuptools>=56.1,<70.4",
    "aiosqlite==0.20.0",
    "aiohttp==3.9.5",
    "pytest==8.2.1",
    "PyMySQL==1.1.1",
]

dev_dependencies = [
    "types-aiofiles==23.1.0.5",
    "types-pyyaml==6.0.12.11",
    "types-setuptools==68.0.0.3",
    "types-PyMySQL==1.1.0.1",
]

kwargs = dict(
    name="chia-pool-reference",
    version="1.2",
    author="Mariano Sorgente",
    author_email="mariano@chia.net",
    description=("A reference pool for the Chia blockchain."),
    license="Apache",
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
