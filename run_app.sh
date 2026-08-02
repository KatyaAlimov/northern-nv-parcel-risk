#!/bin/bash
# Start the Northern Nevada Parcel Risk lookup app (Streamlit).
cd "$(dirname "$0")"
echo "Checking dependencies..."
python3 -m pip install -q -r requirements.txt
echo ""
echo "App: http://localhost:8501"
echo "County map (Docker or 05_serve_city_map.py): http://localhost:8080/city_map.html"
echo ""
python3 -m streamlit run app.py --server.port 8501
