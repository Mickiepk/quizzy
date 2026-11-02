# Quizzy: AWS hands-on guide

You will build this by clicking through the console once, then driving it from code. Each step says **what**, **why**, and **how to check it worked**.

## 0. The big picture

```
Browser (React)
   │  HTTP
   ▼
EC2 instance ── FastAPI (uvicorn) ── serves the React build + /api
   │  IAM instance role (no keys on the box)
   ├──► S3 bucket ........ chats/<chat_id>/lecture.pdf  (+ lecture.pdf.metadata.json)
   │                       meta/<chat_id>.json           (chat record, NOT indexed)
   ├──► Bedrock Knowledge Base ── data source = S3 prefix "chats/"
   │        chunk → embed (Titan Text Embeddings V2) → store in S3 Vectors
   │        Retrieve API (filter chat_id = X) → relevant chunks
   └──► Bedrock Converse API ── Claude Sonnet 5.5 writes questions + explanations
```

Flows:

- **Upload:** browser → FastAPI → `PutObject` to `chats/<id>/…` → `StartIngestionJob` → the KB chunks, embeds and indexes the files.
- **Quiz:** FastAPI calls `Retrieve` (only chunks with `chat_id = <id>`) → sends those chunks to Claude via `Converse` (forced JSON tool output) → React shows a Kahoot-style question.
- **Explain:** after Submit, FastAPI retrieves context for that question and Claude explains why the answer is right or wrong.

**Why one folder per chat is not enough on its own:** a Knowledge Base searches *all* the vectors it holds. The S3 prefix keeps files tidy, but isolation is done by the **`chat_id` metadata** in the sidecar file `lecture.pdf.metadata.json`, which the `Retrieve` call filters on. If the metadata is missing, that file never shows up in a chat-scoped search.

**Two different IAM roles, do not mix them up:**

| Role | Who assumes it | What it does |
|---|---|---|
| KB service role | Bedrock itself | reads your S3 files, calls the embedding model, writes vectors |
| App role (instance profile) | your EC2 / your laptop credentials | uploads to S3, calls Retrieve / Converse / StartIngestionJob |

## 1. Account hygiene (15 min)

1. Never work as the root user. Use an IAM Identity Center user or an IAM admin user with MFA.
2. **Billing → Budgets → Create budget** (monthly cost budget, e.g. 10 USD, email alert at 80%). Do this before creating anything.
3. Install the AWS CLI v2 and run `aws configure` (or `aws configure sso`). Check: `aws sts get-caller-identity`.
4. Pick **one region** for everything. Default in this project is `ap-southeast-1` (Singapore, closest to Bangkok). If the Bedrock "S3 vector bucket" option is missing there when you reach step 4, switch the whole project to a region that has it (e.g. `us-east-1`). Check the [S3 Vectors regions page](https://docs.aws.amazon.com/general/latest/gr/s3-vectors.html).

## 2. S3 bucket (storage)

**Console → S3 → Create bucket**

- Name: `quizzy-<your-initials>-<random>` (bucket names are global).
- Region: your chosen one. **Block all public access: ON** (default). Keep default encryption (SSE-S3).

Check:

```bash
export BUCKET=quizzy-xx-1234
echo hello > t.txt && aws s3 cp t.txt s3://$BUCKET/chats/test/t.txt && aws s3 ls s3://$BUCKET/chats/ --recursive
aws s3 rm s3://$BUCKET/chats/test/t.txt
```

Learn: a bucket is a flat key/value store. "Folders" are just key prefixes (`chats/abc123/`). That is why "one folder per chat" costs nothing, and why deleting a chat = deleting a prefix.

## 3. Bedrock model access

**Console → Amazon Bedrock → Model catalog**

- Open **Claude Sonnet 5.5** and **Titan Text Embeddings V2**. If asked to enable access or submit use-case details (first time for Anthropic models), do it.
- Test in the **Playground**.

Check from the CLI (this is exactly what the app does):

```bash
aws bedrock-runtime converse \
  --model-id global.anthropic.claude-sonnet-5-5 \
  --messages '[{"role":"user","content":[{"text":"Say hi in 5 words"}]}]' \
  --region ap-southeast-1
```

Learn: `global.` is an **inference profile**. It lets Bedrock route your request to any supported region. You call the profile ID, not the bare model ID. Billing for Anthropic models can show up under AWS Marketplace in your bill.

## 4. Knowledge Base (the RAG engine)

**Console → Bedrock → Knowledge Bases → Create → Knowledge Base with vector store**

1. **Name:** `quizzy-kb`. Let Bedrock create a new service role.
2. **Data source:** Amazon S3 → pick your bucket → **Advanced → inclusion prefix: `chats/`**. This keeps `meta/` and `deploy/` out of the index.
3. **Parsing / chunking:** defaults (default chunking, about 300 tokens per chunk). Keep the default parser; it handles PDF text.
4. **Embeddings model:** Titan Text Embeddings V2.
5. **Vector store:** **Quick create → S3 vector bucket**. This is the cheap option: S3 Vectors bills by storage and queries, while OpenSearch Serverless has a standing minimum cost that is not worth it for a learning project.
6. Create, then note the **Knowledge base ID** and open the data source to note the **Data source ID**. Put both in `backend/.env`.

Test it before writing any code:

```bash
aws s3 cp some-lecture.pdf s3://$BUCKET/chats/test/some-lecture.pdf
# console: KB → data source → Sync, wait for "Available"
```

Then use the **Test knowledge base** pane in the console and ask something from the PDF. Delete `chats/test/` afterwards.

Learn:

- **Ingestion job** = scan S3 → parse → chunk → embed → write vectors. Incremental: unchanged files are skipped. Only **one job per data source can run at a time** (that is why the code treats `ConflictException` as "one is already running" and re-syncs later).
- **Sidecar metadata** (`file.pdf.metadata.json`, written by `app.py` before each upload) carries `chat_id`. With S3 Vectors, filterable metadata is limited (1 KB / 35 keys per vector), so keep the schema tiny: one key.
- **Retrieve vs RetrieveAndGenerate:** Quizzy uses plain `Retrieve` (you get chunks) and calls Claude itself through `Converse`, because it needs structured quiz JSON rather than a chat answer.
- Supported file types: PDF, Word, TXT, MD, HTML, CSV, Excel. **Not PowerPoint**: export slides to PDF.

## 5. IAM policy for the app

Use `iam-policy.json` from this repo. Replace `BUCKET_NAME`, `REGION`, `ACCOUNT_ID`, `KB_ID`.

- Locally: your own `aws configure` user already has broad rights; you can skip this until step 7.
- On EC2: **IAM → Roles → Create role → AWS service → EC2**. Attach a **new inline/customer policy** from the JSON, plus `AmazonSSMManagedInstanceCore` (lets you open a shell from the browser with Session Manager, so no SSH key). Name it `quizzy-ec2-role`.

Learn: least privilege. Notice the app cannot create buckets or delete KBs. `Converse` needs the IAM action **`bedrock:InvokeModel`**, on **both** the inference profile and the underlying foundation-model ARNs; missing the second is the classic `AccessDeniedException`.

## 6. Run it locally first

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill BUCKET, KB_ID, DS_ID, region
uvicorn app:app --reload --port 8000
```

```bash
cd frontend && npm install && npm run dev      # http://localhost:5173
```

Try: new chat → upload a PDF → wait for the chip to turn **Ready** → Start quiz.

Watch what happens in AWS while you click: S3 gets `chats/<id>/file.pdf` and `file.pdf.metadata.json`; the KB data source shows a new ingestion job.

## 7. Deploy on EC2

Build and package on your laptop:

```bash
cd frontend && npm run build && cd ..
zip -r quizzy.zip backend frontend/dist -x "backend/.venv/*" "backend/.env" "backend/__pycache__/*"
aws s3 cp quizzy.zip s3://$BUCKET/deploy/quizzy.zip
```

**EC2 → Launch instance**

- AMI: Amazon Linux 2023. Type: `t3.small`. Key pair: none (we use Session Manager).
- Network: default VPC, public subnet, auto-assign public IP **on**.
- **Security group:** inbound TCP **8000** from **My IP only**. Nothing else.
- Advanced → **IAM instance profile: `quizzy-ec2-role`**.

Why "My IP only": Quizzy has **no login**. Anyone who can reach the URL can spend your Bedrock tokens. Keep it locked down, or add Cognito (stretch goals).

Connect (**EC2 → Connect → Session Manager**), then:

```bash
sudo -iu ec2-user
sudo dnf install -y python3.11 python3.11-pip unzip
mkdir -p ~/quizzy && cd ~/quizzy
aws s3 cp s3://YOUR_BUCKET/deploy/quizzy.zip . && unzip -o quizzy.zip   # the instance role fetches it, no keys
cd backend && python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env      # same values as local; still NO access keys
```

Run it as a service:

```bash
sudo tee /etc/systemd/system/quizzy.service >/dev/null <<'EOF'
[Unit]
Description=Quizzy
After=network.target

[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/quizzy/backend
EnvironmentFile=/home/ec2-user/quizzy/backend/.env
ExecStart=/home/ec2-user/quizzy/backend/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now quizzy
sudo systemctl status quizzy --no-pager
journalctl -u quizzy -f        # live logs
```

Open `http://<instance-public-ip>:8000`.

Learn: **instance profile** → temporary credentials from the instance metadata service; boto3 finds them automatically, which is why `.env` has no keys.

## 8. Cost control and cleanup

- **EC2:** billed while running. **Stop** it when you are not using it (a stopped instance keeps its disk, which has a small cost). A stopped instance also gets a new public IP when restarted.
- **Bedrock:** pay per token. Each quiz batch and each explanation is a call. Watch **Billing → Cost Explorer**, filtered by service.
- **S3 / S3 Vectors:** tiny at this scale.
- Check the [Bedrock](https://aws.amazon.com/bedrock/pricing/) and S3 pricing pages for current numbers before you share the app.

Full teardown, in this order: terminate EC2 → delete the Knowledge Base (deletion policy "Delete" removes vectors) → delete the S3 vector bucket if it remains → empty and delete the S3 bucket → delete the IAM role and policy → delete the budget if you like.

## 9. Troubleshooting

| Symptom in the UI | Likely cause | Fix |
|---|---|---|
| `AWS AccessDeniedException … InvokeModel` | policy is missing the foundation-model or inference-profile ARN | use `iam-policy.json` as given |
| `AWS AccessDeniedException … Retrieve` / `ListIngestionJobs` | wrong KB ID in the policy resource | match `KB_ID` and `REGION` |
| Stuck on "Indexing…" | ingestion job running or failed | KB → data source → sync history, read the failure reason |
| "Nothing indexed for this chat yet" | file has no sidecar, unsupported type, or sync not finished | check S3 has `.metadata.json` next to the file; wait for Ready |
| "Indexing failed" | scanned/empty PDF, or unsupported file | OCR the PDF first; export slides to PDF |
| `ConflictException` in logs | two ingestion jobs requested at once | harmless; the app retries |
| `ThrottlingException` | too many calls too fast | slow down; request a quota raise |
| `AWS config/credentials problem` | no credentials locally | `aws configure` / `aws sso login` |

## 10. Stretch goals (in difficulty order)

1. **Presigned URL uploads:** browser PUTs straight to S3 (needs a bucket CORS rule); the server only writes the sidecar. Teaches CORS and presigned URLs.
2. **Automatic sync with events:** S3 event → Lambda → `StartIngestionJob`, buffered so bursts do not conflict.
3. **Login:** Cognito user pool, one set of 3 chats per user (`chats/<user>/<chat>/`), with `user_id` in the metadata filter.
4. **DynamoDB** for chat records and quiz history instead of `meta/*.json`.
5. **HTTPS:** nginx or an Application Load Balancer + ACM certificate + Route 53.
6. **Infrastructure as code:** rebuild everything with AWS CDK or Terraform, so the teardown is one command.
7. **CloudWatch:** structured logs and a billing/usage alarm on Bedrock.
