@echo off
echo Installing dependencies...
pip install -r requirements.txt
playwright install
echo Starting App...
python -m streamlit run app.py
pause
