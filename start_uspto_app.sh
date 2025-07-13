#!/bin/bash
source /home/ec2-user/miniconda3/etc/profile.d/conda.sh
conda activate tesseract-env
cd /home/ec2-user/TestApp
exec python3 app.py
