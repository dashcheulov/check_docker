from setuptools import setup, find_packages
from os import path

here = path.abspath(path.dirname(__file__))
with open(path.join(here, 'README.md')) as f:
    long_description = f.read()

setup(
    name='check_docker',
    version='0.0.1',
    description='The script listens and exposes events of docker to Zabbix',
    long_description=long_description,
    url='https://github.com/dashcheulov/check_docker',
    author='Denis Ashcheulov',
    license='MIT',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: IOW SysOps',
        'Intended Audience :: IOW PMs',
        'License :: OSI Approved :: MIT',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.4',
    ],
    keywords='zabbix docker',
    packages=find_packages(),
    install_requires=['docker', 'pyaml'],
    entry_points={
        'console_scripts': [
            'check_docker=check_docker:App.entry'
        ],
    },
)