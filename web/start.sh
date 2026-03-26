#!/bin/bash
cd /root/projects/prop-capture-web
source /root/.secrets.env 2>/dev/null || true
uvicorn main:app --host 0.0.0.0 --port 5050
