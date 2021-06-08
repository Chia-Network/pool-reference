import os

import setuptools
from setuptools import setup

# Utility function to read the README file.
# Used for the long_description.  It's nice, because now 1) we have a top level
# README file and 2) it's easier to type in the README file than to put a raw
# string in below ...
def read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()


setup(
    name="chia-pool-reference",
    version="1.2",
    author="Mariano Sorgente",
    author_email="mariano@chia.net",
    description=("A reference pool for the Chia blockchain."),
    license="Apache",
    packages=setuptools.find_packages(),
    install_requires=["wheel", "chia-blockchain", "Flask"],
    long_description=read("README.md"),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Topic :: Utilities",
        "License :: OSI Approved :: BSD License",
    ],
)
