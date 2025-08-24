A) Start the Python agent (FastAPI)
cd .\sharepoint-ai-agent\
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

# If you use the FastAPI app:
uvicorn server_fastapi:app --reload --port 8000

# If you use the older Flask app instead:
# python app.py


B) Start the Next.js UI
cd .\doc-ui\
npm install
# Create doc-ui\.env.local with your backend URL, e.g.:
# AGENT_BASE_URL=http://localhost:8000
npm run dev
