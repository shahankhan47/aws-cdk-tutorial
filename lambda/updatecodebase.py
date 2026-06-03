import asyncio
import fnmatch
import json
import os
import re
import shutil
import time
import urllib.request

from app.constants import DEFAULT_IGNORE_PATTERNS, TEXT_FILE_EXTENSIONS
from app.core.codebase.mermaid import generate_project_diagrams
from app.core.codebase.summarizer import generate_executive_summary, summarize_file
from app.core.codebase.truncator import anthropic_truncator
from app.core.embeddings.update_embeddings import update_vectors
from app.db.codebase import store_summary_in_db
from app.db.projects import update_status_in_db
import app.services.email_service as email_service
from app.utils.summarizer_utils import json_correction

PENDO_TRACK_URL = "https://data.pendo.io/data/track"
PENDO_INTEGRATION_KEY = "8a5b68f1-ecb4-4a57-a657-c76b1207b5cf"


def pendo_track(event_name, visitor_id="system", account_id="system", properties=None):
    """Send a server-side track event to Pendo."""
    try:
        payload = json.dumps({
            "type": "track",
            "event": event_name,
            "visitorId": visitor_id,
            "accountId": account_id,
            "timestamp": int(time.time() * 1000),
            "properties": properties or {},
        }).encode("utf-8")
        req = urllib.request.Request(
            PENDO_TRACK_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-pendo-integration-key": PENDO_INTEGRATION_KEY,
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Pendo track event '{event_name}' failed: {e}")

async def safe_json_loads(possible_json):
    """Try to load JSON, and extract if embedded or duplicated."""
    try:
        return json.loads(possible_json)
    except Exception:
        try:
            corrected_extracted = await json_correction(possible_json)
            corrected_extracted_json =  json.loads(corrected_extracted)
            return corrected_extracted_json
                
        except Exception:
            print(f"JSON correction failed for {possible_json}")
            return {"summary": "error generating summary ", "qualitative_score": "1"}
            
        except Exception:
            raise
       

async def process_file(file_path, extract_dir):
    relative_path = os.path.relpath(file_path, extract_dir)

    if should_ignore_path(relative_path):
        return None, None  # Skip ignored files

    if os.path.splitext(file_path)[1].lower() in TEXT_FILE_EXTENSIONS:
        file_path_1, content, summary = await summarize_file(file_path)
        print(f'Summary succeeded for {file_path}')
        summary_dict = await safe_json_loads(summary)

        qualitative_score = summary_dict.get('qualitative_score', 0)
        summary = summary_dict.get('summary', '')

        if summary:
            file_name = os.path.basename(file_path)
            context_summary = {
                'score': qualitative_score,
                'text': f"file name is {file_name}, \nfile path is: {file_path_1}, \nfilesummary is: {summary}"
            }
            full_summary = [file_name, file_path, content, summary]
            return context_summary, full_summary

    return None, None

def should_ignore_path(path, ignore_patterns=DEFAULT_IGNORE_PATTERNS):
    path_parts = path.split(os.sep)
    for pattern in ignore_patterns:
        if any(fnmatch.fnmatch(part, pattern) for part in path_parts):
            return True
    return False

async def updatecodebase(extract_dir: str, project_id: str, project_details: str, file_source :str, commit_id :str):
    try:
        await update_status_in_db(emails=project_details["emails"], project_id=project_id, 
                                  project_description=project_details["project_description"],
                                  project_name=project_details["project_name"],
                                  status="Updated Summary being generated", summary=None, executive_summary=None, 
                                  project_diagrams=None, file_source=file_source, commit_id=commit_id)

        # Gather all file paths first
        all_file_paths = []
        for root, dirs, files in os.walk(extract_dir):
            dirs[:] = [d for d in dirs if not should_ignore_path(os.path.join(root, d), DEFAULT_IGNORE_PATTERNS)]

            for name in files:
                file_path = os.path.join(root, name)
                if os.path.splitext(file_path)[1].lower() in TEXT_FILE_EXTENSIONS and not should_ignore_path(file_path):
                    all_file_paths.append(file_path)

        # Process all files in parallel
        async def process_files_in_parallel(file_paths, batch_size=10):
            context_summaries = []
            full_summaries = []

            for i in range(0, len(file_paths), batch_size):
                batch = file_paths[i:i+batch_size]
                tasks = [process_file(file_path, extract_dir) for file_path in batch]
                results = await asyncio.gather(*tasks)

                successful_count = 0
                skipped_count = 0
                for context_summary, full_summary in results:
                    if context_summary and full_summary:
                        context_summaries.append(context_summary)
                        full_summaries.append(full_summary)
                        successful_count += 1
                    else:
                        skipped_count += 1

                pendo_track("file_batch_processed", properties={
                    "project_id": project_id,
                    "batch_size": batch_size,
                    "batch_index": i // batch_size,
                    "successful_summaries_count": successful_count,
                    "skipped_files_count": skipped_count,
                    "total_files_in_batch": len(batch),
                })

            context_summaries.sort(key=lambda x: x['score'], reverse=True)
            combined_summary = '\n\n\n'.join([item['text'] for item in context_summaries])

            return combined_summary, full_summaries

        context_summaries, full_summaries = await process_files_in_parallel(all_file_paths)

        if context_summaries:
            combined_summary = anthropic_truncator(text=context_summaries)

            await update_vectors(project_id=project_id, full_summaries=full_summaries, action="update")

            pendo_track("vector_embeddings_updated", properties={
                "project_id": project_id,
                "action": "update",
                "total_summaries_count": len(full_summaries),
                "files_embedded_count": len(full_summaries),
            })

            executive_summary = await generate_executive_summary(combined_summary)

            pendo_track("executive_summary_generated", properties={
                "project_id": project_id,
                "summary_length": len(executive_summary) if executive_summary else 0,
                "input_context_length": len(combined_summary),
                "files_included_count": len(full_summaries),
            })

            diagrams = await generate_project_diagrams(project_id=project_id, summary=combined_summary)

            pendo_track("project_diagrams_generated", properties={
                "project_id": project_id,
                "diagram_count": len(diagrams) if isinstance(diagrams, list) else 1,
                "summary_input_length": len(combined_summary),
            })

            await store_summary_in_db(emails=project_details["emails"], project_id=project_id, summary=combined_summary,
                                      status="Updated", executive_summary=executive_summary, project_diagrams=diagrams)

            email_service.codebase_update_succeeded(project_details["emails"], project_details["project_name"])

            pendo_track("codebase_update_completed", properties={
                "project_id": project_id,
                "project_name": project_details["project_name"],
                "file_source": file_source,
                "commit_id": commit_id,
                "total_files_processed": len(all_file_paths),
                "email_recipients_count": len(project_details["emails"]) if isinstance(project_details["emails"], list) else 1,
            })

            pendo_track("codebase_update_notification_sent", properties={
                "project_id": project_id,
                "project_name": project_details["project_name"],
                "notification_type": "success",
                "recipient_emails_count": len(project_details["emails"]) if isinstance(project_details["emails"], list) else 1,
                "update_status": "succeeded",
            })

            print("email sent")
        else:
            await update_status_in_db(emails=project_details["emails"], project_id=project_id, 
                                      project_description=project_details["project_description"],
                                      project_name=project_details["project_name"],
                                      status="codebase update failed, Contact: sai_002@harmonyengine.ai", 
                                      summary=None, executive_summary=None, project_diagrams=None, 
                                      file_source=file_source, commit_id=commit_id)
            print("no files were found")
    except Exception as e:
        error_msg = f"Error in process_and_post_summary: {str(e)}"
        print(error_msg)
        await update_status_in_db(emails=project_details["emails"], project_id=project_id, 
                                  project_description=project_details["project_description"],
                                  project_name=project_details["project_name"],
                                  status=error_msg, summary=None, executive_summary=None, project_diagrams=None, 
                                  file_source=file_source, commit_id=commit_id)
        await email_service.codebase_update_failed(project_details["emails"], project_details["project_name"])

        pendo_track("codebase_update_failed", properties={
            "project_id": project_id,
            "project_name": project_details["project_name"],
            "file_source": file_source,
            "commit_id": commit_id,
            "error_message": str(e)[:200],
            "failure_reason": type(e).__name__,
        })

        pendo_track("codebase_update_notification_sent", properties={
            "project_id": project_id,
            "project_name": project_details["project_name"],
            "notification_type": "failure",
            "recipient_emails_count": len(project_details["emails"]) if isinstance(project_details["emails"], list) else 1,
            "update_status": "failed",
        })

    finally:
        shutil.rmtree(extract_dir)