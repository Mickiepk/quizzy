# Quizzy

Upload your lectures, get an endless Kahoot-style quiz with an AI explanation after every answer.

**Stack:** React (Vite) · FastAPI · S3 · Amazon Bedrock Knowledge Base (S3 Vectors) · Bedrock Converse (Claude) · EC2

- Up to **3 chats**. Each chat is one S3 folder: `chats/<chat_id>/`.
- Upload lectures (PDF, Word, TXT, MD, HTML, CSV, Excel; convert .pptx to PDF first).
- Timed questions with 4 colored choices → **Submit** → points, streak, **AI explanation** → **Continue**. Questions are prefetched so it never stops.

## Start here

Follow **[AWS-GUIDE.md](AWS-GUIDE.md)**. It walks through creating each AWS resource, then running locally, then deploying on EC2.

## Quick run (after the AWS setup)

```bash
# backend
cd backend && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # fill in bucket / KB id / data source id
uvicorn app:app --reload --port 8000

# frontend (second terminal)
cd frontend && npm install && npm run dev     # http://localhost:5173
```

## Layout

```
backend/app.py            all API logic (S3, Knowledge Base, Converse)
backend/.env.example      the 3 values you get from AWS
iam-policy.json           least-privilege policy for the app
frontend/src/App.jsx      sidebar: chats (max 3)
frontend/src/ChatPanel.jsx upload + indexing status
frontend/src/Quiz.jsx     Kahoot-style game loop
AWS-GUIDE.md              the hands-on lessons
```
