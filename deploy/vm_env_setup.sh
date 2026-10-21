#!/usr/bin/env bash
# Run on the VM after copying the .env files and the Google key from the laptop
# (deploy/AZURE.md step 5). Swaps the Windows paths for VM paths and points the phone
# agent at the local services. Safe to run again.
set -euo pipefail

VA="$HOME/ACT-Voice-Assistant"
CA="$HOME/ACT-Calling-Agent"
SA="$VA/rag-backend/secrets/service-account.json"

# Replace KEY=... in a file, or add the line if the key is not there.
set_key() {
  local file="$1" key="$2" value="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

for f in "$CA/.env" "$CA/erp-lookup/.env" "$VA/rag-backend/.env" "$VA/voice-pipeline/.env" "$SA" "$CA/erp-lookup/erp.db"; do
  if [ ! -f "$f" ]; then
    echo "MISSING: $f  (copy it from the laptop first, AZURE.md step 5)"
    exit 1
  fi
done

for f in "$CA/.env" "$CA/erp-lookup/.env" "$VA/rag-backend/.env" "$VA/voice-pipeline/.env"; do
  sed -i 's/\r$//' "$f"   # Windows line endings
  chmod 600 "$f"
done
chmod 600 "$SA" "$CA/erp-lookup/erp.db"

set_key "$CA/.env" VOICE_PIPELINE_DIR "$VA/voice-pipeline"
set_key "$CA/.env" GOOGLE_SERVICE_ACCOUNT_JSON "$SA"
set_key "$CA/.env" RAG_BACKEND_PUBLIC_URL "http://127.0.0.1:8000"
set_key "$CA/.env" ERP_URL "http://127.0.0.1:8100"
set_key "$CA/.env" ENABLE_TEST_PAGE "1"   # nginx puts a password on /test
set_key "$VA/rag-backend/.env" GOOGLE_SERVICE_ACCOUNT_JSON "$SA"
set_key "$VA/voice-pipeline/.env" RAG_BACKEND_PUBLIC_URL "http://127.0.0.1:8000"

echo "OK: .env paths set for this VM"
grep -E '^(VOICE_PIPELINE_DIR|GOOGLE_SERVICE_ACCOUNT_JSON|RAG_BACKEND_PUBLIC_URL|ERP_URL|ENABLE_TEST_PAGE|PHONE_BACKGROUND)=' "$CA/.env"
