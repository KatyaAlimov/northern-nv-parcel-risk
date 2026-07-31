#!/bin/bash
# Start the Washoe Parcel Risk Lookup app
cd "$(dirname "$0")"
echo "Installing/checking dependencies..."
python3 -m pip install -q -r requirements.txt
echo ""
echo "Starting app at http://localhost:8501"
echo "In the sidebar: Street = RIVERSIDE, City = RENO, then click Analyze risk"
echo ""
python3 -m streamlit run app.py --server.port 8501
