#!/bin/bash
sudo apt update
sudo apt install libportaudio2 portaudio19-dev python3-tk python3-venv
python3 -m venv ewbs_venv
source ewbs_venv/bin/activate
pip install "numpy<=2.3.5" sounddevice
