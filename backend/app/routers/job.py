import os
import uuid
import logging
import pandas as pd
from typing import Dict, Any
from datetime import datetime

from fastapi import APIRouter, Depends, Query, HTTPException, Header, Response
from fastapi.responses import FileResponse, JSONResponse

from sqlalchemy.ext.asyncio import AsyncSession

from celery import Celery

from shared.db.db_engine import get_db
from shared.auth.auth import get_current_user, CurrentUser
from shared.models.job import GranularityLevel, JobStatus

from shared.schemas.job import ResponseJob, FormParams, QueryParams, ResponseJobStatus
from shared.handlers.job import JobHandler

logger = logging.getLogger(__name__)

HOST_REDS = os.getenv("HOST_REDS", "redis")

REDIS_URL_BROKER = f"redis://{HOST_REDS}:6379/0"
REDIS_URL_BACKEND = f"redis://{HOST_REDS}:6379/1"


router = APIRouter(tags=["Jobs"], prefix = '/job')
dispatcher = Celery(
    "worker",
    broker=REDIS_URL_BROKER,
    backend=REDIS_URL_BACKEND,
)



@router.post("/estimate", response_model=ResponseJob)
async def estimate_job(
    form_params: FormParams = Depends(FormParams.as_form),
    query_params: QueryParams = Depends(QueryParams.as_query),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
):
    return await JobHandler(db=db, current_user=current_user).EstimateJob(
        prompt_id=form_params.prompt_id,
        media_id=form_params.media_id,
        model_id=form_params.model_id,
        focus_column=form_params.focus_column,
        granularity=GranularityLevel[query_params.granularity],
        verbosity=query_params.verbosity,
        chunk_size=query_params.chunk_size,
    )



@router.post("/start", response_model=ResponseJob)
async def start_job(
    form_params: FormParams = Depends(FormParams.as_form),
    query_params: QueryParams = Depends(QueryParams.as_query),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
):
    # First estimate the job to get all the required values
    job_handler = JobHandler(db=db, current_user=current_user)

    # Estimate the job first to calculate tokens and costs
    job = await job_handler.EstimateJob(
        prompt_id=form_params.prompt_id,
        media_id=form_params.media_id,
        model_id=form_params.model_id,
        focus_column=form_params.focus_column,
        granularity=GranularityLevel[query_params.granularity],
        verbosity=query_params.verbosity,
        chunk_size=query_params.chunk_size,
    )

    # Create the actual job and store it - JobCreate now uses data from EstimateJob
    job = await job_handler.JobCreate()

    # Queue the job for processing
    task = dispatcher.send_task('workers.process_job', args=[str(job.job_id)])

    # Create a complete response with task info
    return ResponseJob(
        job_id=job.job_id,
        filename=job.filename,
        modelname=job.modelname,
        verbosity=job.verbosity,
        granularity=job.granularity,
        estimated_input_tokens=job.estimated_input_tokens,
        estimated_output_tokens=job.estimated_output_tokens,
        cost_per_1m_input=job.cost_per_1m_input,
        cost_per_1m_output=job.cost_per_1m_output,
        handling_fee=job.handling_fee,
        estimated_cost=job.estimated_cost,
        job_status=job.job_status,
        created_at=job.created_at,
        completed_at=job.completed_at,
        task_id=str(task.id),
        task_status="queued"
    )


@router.get("/status", response_model=ResponseJobStatus)
async def check_job_status(
    job_id: uuid.UUID = Query(..., description="Job ID"),
    include_data: bool = Query(False, description="Include full data in diagnosis"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
):
    job_handler = JobHandler(db=db, current_user=current_user)
    job = await job_handler.JobRead(job_id)
    task_status = "unknown"
    try:
        if hasattr(job, 'task_id') and job.task_id:
            task = dispatcher.AsyncResult(job.task_id)
            task_status = task.status
    except Exception as e:
        logger.error(f"Error getting task status: {str(e)}")

    chunks_stats = await job_handler.GetChunksStats(job_id)

    try:
        created_at = job.created_at if hasattr(job, "created_at") and job.created_at is not None else datetime.now()
        completed_at = None
        if (hasattr(job, "completed_at") and hasattr(job, "job_status")
            and job.job_status == JobStatus.FINISHED and job.completed_at is not None):
            completed_at = job.completed_at

        return ResponseJobStatus(
            job_id=str(job_id),
            status=str(job.job_status) if hasattr(job, "job_status") else "unknown",
            task_status=task_status,
            chunks_total=chunks_stats.get("total", 0),
            chunks_completed=chunks_stats.get("completed", 0),
            chunks_in_progress=chunks_stats.get("in_progress", 0),
            chunks_failed=chunks_stats.get("failed", 0),
            created_at=created_at,
            completed_at=completed_at
        )
    except Exception as e:
        logger.error(f"Error creating job status response: {str(e)}")
        raise HTTPException(status_code=500, detail="Error retrieving job status")


@router.get("/results", response_model=Dict[str, Any])
async def get_job_results(
    job_id: uuid.UUID = Query(..., description="Job ID"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
):
    job_handler = JobHandler(db=db, current_user=current_user)
    job = await job_handler.JobRead(job_id)
    chunks = await job_handler.GetJobChunks(job_id)
    response = {
        "job_id": str(job_id),
        "status": str(job.job_status) if hasattr(job, "job_status") else "unknown",
        "chunks": [],
        "combined_results": job.combined_results if hasattr(job, "combined_results") and job.combined_results else [],
        "completed": hasattr(job, "job_status") and job.job_status == JobStatus.FINISHED
    }

    for chunk in chunks:
        chunk_data = {
            "chunk_index": chunk.chunk_index,
            "row_range": chunk.row_range,
            "status": chunk.status,
            "input_data": chunk.source_data,
            "output_data": chunk.output_data if chunk.status == JobStatus.FINISHED else None
        }
        response["chunks"].append(chunk_data)
    
    # Add combined results if requested
    if include_data and hasattr(job, "combined_results") and job.combined_results:
        response["combined_results_sample"] = []
        # Include first 2 items as sample
        for i, result in enumerate(job.combined_results[:2]):
            sample = {
                "index": i,
                "has_row": "row" in result,
                "row_value": result.get("row"),
                "has_output": "output" in result,
                "output_type": str(type(result.get("output"))),
                "output_length": len(str(result.get("output", "")))
            }
            response["combined_results_sample"].append(sample)

    return response


@router.get("/download", response_class=FileResponse)
async def download_job_file(
    job_id: uuid.UUID = Query(..., description="Job ID"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
):
    """
    Download job results as a file with the same format as the original input file
    """
    try:
        logger.info(f"Starting download for job {job_id}")

        # Initialize the job handler
        job_handler = JobHandler(db=db, current_user=current_user)

        # Get the job and check its status
        await job_handler.JobRead(job_id)

        # Get all job chunks
        chunks = await job_handler.GetJobChunks(job_id)
        logger.info(f"Found {len(chunks)} chunks for job {job_id}")

        # Get the original file info from the database
        from shared.ops.job import JobDb
        job_db = JobDb(db, current_user)
        original_job = await job_db.get_job_entry(job_id)

        from shared.ops.media import MediaDb
        media_db = MediaDb(db)
        media = await media_db.get_media_entry(original_job.media_id, current_user.id)
        filename = media.filename
        file_ext = os.path.splitext(filename)[1].lower()
        logger.info(f"Original file: {filename}, extension: {file_ext}")

        export_dir = os.path.join("/app/uploads", "exports")
        os.makedirs(export_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_filename = f"processed_{os.path.splitext(filename)[0]}_{timestamp}{file_ext}"
        export_path = os.path.join(export_dir, export_filename)

        all_rows = []
        processed_chunks = 0
        processed_rows = 0

        # First attempt: Try to use combined_results if available
        if hasattr(original_job, 'combined_results') and original_job.combined_results:
            try:
                logger.info(f"Using combined results for job {job_id}")
                processed_chunks = len(chunks)
                
                # Get the original file data to enrich with results
                original_data = {}
                logger.info(f"Building original data from {len(chunks)} chunks")
                for chunk in chunks:
                    if hasattr(chunk, 'source_data') and chunk.source_data:
                        for item in chunk.source_data:
                            if isinstance(item, dict) and 'row' in item and 'data' in item:
                                original_data[item['row']] = item['data']
                logger.info(f"Original data contains {len(original_data)} rows")
            
                for row in original_job.combined_results:
                    if not isinstance(row, dict) or 'row' not in row:
                        continue
                    
                    processed_rows += 1
                    row_idx = row['row']
                
                    # Create output row starting with original data
                    output_row = original_data.get(row_idx, {}).copy() if row_idx in original_data else {}
                
                    # Add the output as a new column 'Analysis_Result'
                    if 'output' in row:
                        try:
                            if isinstance(row['output'], dict):
                                # If output is a dict, add a serialized version as the result
                                output_row['Analysis_Result'] = json.dumps(row['output'])
                            else:
                                # If output is a string or other type, add it directly
                                output_text = str(row['output']).strip()
                                # Clean up the text a bit by removing markdown if present
                                if output_text.startswith("```") and "```" in output_text[3:]:
                                    parts = output_text.split("```")
                                    if len(parts) >= 3:
                                        output_text = parts[1].strip()
                                output_row['Analysis_Result'] = output_text
                        except Exception as output_error:
                            # Handle any serialization errors
                            output_row['Analysis_Result'] = f"Error processing output: {str(output_error)}"
                            logger.error(f"Error processing output: {str(output_error)}")
                    else:
                        # Ensure there's always an Analysis_Result column, even if empty
                        output_row['Analysis_Result'] = ""
                
                    # Add the row if we have data
                    if output_row:
                        all_rows.append(output_row)
                        
                    # Ensure we have at least one row with Analysis_Result column
                    if not all_rows and original_data:
                        for idx, data in original_data.items():
                            row_data = data.copy()
                            row_data['Analysis_Result'] = "No result available"
                            all_rows.append(row_data)
            except Exception as combined_error:
                logger.error(f"Error processing combined results: {str(combined_error)}")
                # Fall through to next method if this fails
        # Second attempt: Try to extract from output_data in finished chunks
        else:
            for chunk in chunks:
                if hasattr(chunk, 'output_data') and chunk.output_data and (not hasattr(chunk, 'status') or chunk.status == JobStatus.FINISHED):
                    processed_chunks += 1
    
                    for row in chunk.output_data:
                        if not isinstance(row, dict):
                            continue
    
                        processed_rows += 1
    
                        # If there's a direct 'output' field, use that
                        if 'output' in row:
                            if isinstance(row['output'], dict):
                                all_rows.append(row['output'])
                            else:
                                all_rows.append({'result': str(row['output'])})
                        # Otherwise use any non-special fields
                        else:
                            output_row = {}
                            for key, value in row.items():
                                if key not in ['row', 'input']:
                                    output_row[key] = value
                            if output_row:
                                all_rows.append(output_row)

        # Third attempt: If no rows found, try to use input data
        if not all_rows:
            logger.warning(f"No output data found in job {job_id}. Trying alternative extraction methods.")

            # Try to extract data directly from chunks
            for chunk in chunks:
                # Extract from source_data if available
                if hasattr(chunk, 'source_data') and chunk.source_data:
                    for item in chunk.source_data:
                        if isinstance(item, dict):
                            if 'data' in item and isinstance(item['data'], dict):
                                all_rows.append(item['data'])
                            elif all(k != 'row' for k in item.keys()):  # If it's not just metadata
                                all_rows.append(item)

                # Special case for output_data with different structure
                if hasattr(chunk, 'output_data') and chunk.output_data:
                    if isinstance(chunk.output_data, list):
                        for item in chunk.output_data:
                            if isinstance(item, str):
                                # Handle string outputs
                                all_rows.append({"result": item})
                            elif isinstance(item, dict):
                                # Handle dictionary outputs without expected structure
                                all_rows.append(item)
                    elif isinstance(chunk.output_data, dict):
                        # Handle case where output_data itself is a dict
                        all_rows.append(chunk.output_data)
                    elif isinstance(chunk.output_data, str):
                        # Handle case where output_data is a string
                        all_rows.append({"result": chunk.output_data})

        # Create dataframe
        if not all_rows:
            logger.error(f"No processable data found in job {job_id} with {len(chunks)} chunks")

            # Final fallback: Try to extract original data from source_data
            fallback_data = []
            
            # First try to get original data from source_data
            for chunk in chunks:
                if hasattr(chunk, 'source_data') and chunk.source_data:
                    for item in chunk.source_data:
                        if isinstance(item, dict) and 'data' in item:
                            row_data = item['data'].copy() if isinstance(item['data'], dict) else {'value': item['data']}
                            row_data['Analysis_Result'] = "Processing failed"
                            fallback_data.append(row_data)
            
            # If still no data, use chunk metadata
            if not fallback_data:
                for chunk in chunks:
                    chunk_dict = {k: v for k, v in vars(chunk).items()
                                if k not in ['_sa_instance_state', 'id', 'job_id', 'user_id', 'hash', 'created_at', 'completed_at']}
                    chunk_dict['Analysis_Result'] = "No results available"
                    fallback_data.append(chunk_dict)

            if fallback_data:
                logger.warning(f"Using fallback data extraction for job {job_id}")
                df = pd.DataFrame(fallback_data)
            else:
                # Absolute last resort - create a single row with explanation
                df = pd.DataFrame([{'Analysis_Result': "Unable to process data. Please try again."}])
        else:
            logger.info(f"Creating dataframe with {len(all_rows)} rows from {processed_chunks} chunks")
            df = pd.DataFrame(all_rows)
            
            # Ensure Analysis_Result column exists
            if 'Analysis_Result' not in df.columns:
                df['Analysis_Result'] = "Processing completed but no results available"
                
            # Make sure we have an Analysis_Result column even if not in the data
            if 'Analysis_Result' not in df.columns:
                df['Analysis_Result'] = ""
                
            # Make sure 'Analysis_Result' is the last column for better UX
            if 'Analysis_Result' in df.columns:
                columns = [col for col in df.columns if col != 'Analysis_Result'] + ['Analysis_Result']
                df = df[columns]
            else:
                # If Analysis_Result column doesn't exist, add it
                df['Analysis_Result'] = "Result not available"
                columns = [col for col in df.columns if col != 'Analysis_Result'] + ['Analysis_Result']
                df = df[columns]

        # Export to file based on original extension
        try:
            # Make sure we have data to export
            if df.empty:
                logger.warning(f"No data to export for job {job_id}")
                # Create a simple DataFrame with empty results as fallback
                df = pd.DataFrame([{'Analysis_Result': "No results available"}])
                
            # Clean up any NaN values before exporting
            df = df.fillna('')

            if file_ext.lower() in ['.xlsx', '.xls']:
                # Use the ExcelWriter to better format the output
                with pd.ExcelWriter(export_path, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name='Results')
                    # Auto-adjust columns width
                    try:
                        worksheet = writer.sheets['Results']
                        # Make Analysis_Result column extra wide for better readability
                        for idx, col in enumerate(df.columns):
                            if idx < 26:  # Excel columns only go to Z (26)
                                if col == 'Analysis_Result':
                                    # Make analysis column wider
                                    worksheet.column_dimensions[chr(65 + idx)].width = 100
                                else:
                                    max_len = df[col].astype(str).map(len).max()
                                    max_len = max(max_len, len(col)) + 2  # adding a little extra space
                                    worksheet.column_dimensions[chr(65 + idx)].width = min(max_len, 100)  # limit to 100 to avoid excessive width
                    except Exception as format_error:
                        logger.warning(f"Could not auto-format Excel columns: {str(format_error)}")
            
                logger.info(f"Exported Excel file to {export_path} with formatted columns")
            elif file_ext.lower() in ['.csv']:
                df.to_csv(export_path, index=False)
                logger.info(f"Exported CSV file to {export_path}")
            elif file_ext.lower() in ['.tsv']:
                df.to_csv(export_path, sep='\t', index=False)
                logger.info(f"Exported TSV file to {export_path}")
            else:
                # Default to CSV if extension not recognized
                export_path = f"{os.path.splitext(export_path)[0]}.csv"
                df.to_csv(export_path, index=False)
                logger.info(f"Exported default CSV file to {export_path}")
        except Exception as e:
            logger.error(f"Error exporting file: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Error exporting file: {str(e)}"
            )

        # Return file response
        logger.info(f"Successfully processed job {job_id}, returning file {export_filename}")
        return FileResponse(
            path=export_path,
            filename=export_filename,
            media_type="application/octet-stream"
        )
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Unexpected error in download endpoint: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error processing download: {str(e)}"
        )


@router.get("/diagnose/{job_id}", response_model=Dict[str, Any])
async def diagnose_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
):
    """
    Diagnostic endpoint for debugging job data structure

    Returns detailed information about the job's output data for debugging issues
    """
    try:
        job_handler = JobHandler(db=db, current_user=current_user)
        job = await job_handler.JobRead(job_id)
        chunks = await job_handler.GetJobChunks(job_id)

        # Get the job's database entry
        from shared.ops.job import JobDb
        job_db = JobDb(db, current_user)
        original_job = await job_db.get_job_entry(job_id)

        # Get file info
        from shared.ops.media import MediaDb
        media_db = MediaDb(db)
        media = await media_db.get_media_entry(original_job.media_id, current_user.id)

        # Analyze chunks
        chunks_analysis = []
        for chunk in chunks:
            chunk_info = {
                "chunk_index": chunk.chunk_index,
                "status": str(chunk.status),
                "has_source_data": hasattr(chunk, "source_data") and chunk.source_data is not None,
                "has_output_data": hasattr(chunk, "output_data") and chunk.output_data is not None,
                "source_data_type": str(type(chunk.source_data)) if hasattr(chunk, "source_data") else "None",
                "output_data_type": str(type(chunk.output_data)) if hasattr(chunk, "output_data") else "None",
                "source_data_len": len(chunk.source_data) if hasattr(chunk, "source_data") and chunk.source_data else 0,
                "output_data_len": len(chunk.output_data) if hasattr(chunk, "output_data") and chunk.output_data else 0,
            }

            # Sample data (first item) if available
            if hasattr(chunk, "source_data") and chunk.source_data and len(chunk.source_data) > 0:
                chunk_info["sample_source_item"] = str(type(chunk.source_data[0]))
                if isinstance(chunk.source_data[0], dict):
                    chunk_info["sample_source_keys"] = list(chunk.source_data[0].keys())

            if hasattr(chunk, "output_data") and chunk.output_data and len(chunk.output_data) > 0:
                chunk_info["sample_output_item"] = str(type(chunk.output_data[0]))
                if isinstance(chunk.output_data[0], dict):
                    chunk_info["sample_output_keys"] = list(chunk.output_data[0].keys())

            chunks_analysis.append(chunk_info)

        # Create diagnostic report
        report = {
            "job_id": str(job_id),
            "job_status": str(job.job_status) if hasattr(job, "job_status") else "unknown",
            "file_info": {
                "filename": media.filename,
                "extension": os.path.splitext(media.filename)[1].lower(),
                "media_id": str(original_job.media_id)
            },
            "chunks_count": len(chunks),
            "chunks_with_output": sum(1 for c in chunks if hasattr(c, "output_data") and c.output_data),
            "chunks_finished": sum(1 for c in chunks if c.status == JobStatus.FINISHED),
            "chunks_analysis": chunks_analysis,
        }

        return report

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error in diagnose endpoint: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error diagnosing job: {str(e)}"
        )
