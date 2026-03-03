import glob
import json
import os
import time
import click
from transcriber.config import settings
from transcriber.whisper_transcriber import WhisperTranscriber


@click.group()
def cli():
    """Bloger Bot Transcriber — local Whisper transcription tool."""
    pass


@cli.command()
@click.argument("input_path", type=click.Path(exists=True))
@click.option("--output-dir", default=None)
def transcribe(input_path: str, output_dir: str | None):
    """Transcribe a single audio/video file."""
    output_dir = output_dir or settings.TRANSCRIPTION_OUTPUT_DIR
    t = WhisperTranscriber()
    result = t.transcribe(input_path)
    path = t.save(result, output_dir)
    click.echo(f"✅ Saved: {path}")
    click.echo(f"   Duration: {result['duration']}s, Processing: {result['processing_time']}s")


@cli.command()
@click.option("--limit", default=10, help="Max items to process per batch")
def process(limit: int):
    """Process downloaded items from the database (main workflow)."""
    from transcriber.db_client import (
        get_downloaded_items,
        update_item_transcribing,
        update_item_transcribed,
        update_item_transcription_failed,
    )

    items = get_downloaded_items(limit=limit)
    if not items:
        click.echo("📭 No items to transcribe")
        return

    click.echo(f"📋 Found {len(items)} items to transcribe")
    t = WhisperTranscriber()

    for item in items:
        item_id = item["id"]
        file_path = item["file_path"]

        if not os.path.exists(file_path):
            click.echo(f"❌ File not found: {file_path}")
            update_item_transcription_failed(item_id, f"File not found: {file_path}")
            continue

        click.echo(f"🎙️  Transcribing msg_{item['source_message_id']} ({item['content_type']})...")
        update_item_transcribing(item_id)

        try:
            result = t.transcribe(file_path)
            transcript_path = t.save(result, settings.TRANSCRIPTION_OUTPUT_DIR)
            update_item_transcribed(item_id, transcript_path, result["full_text"])
            click.echo(f"   ✅ Done in {result['processing_time']}s → {transcript_path}")
        except Exception as e:
            click.echo(f"   ❌ Failed: {e}")
            update_item_transcription_failed(item_id, str(e))


@cli.command()
@click.option("--interval", default=30, help="Seconds between checks")
@click.option("--limit", default=5, help="Max items per batch")
def watch(interval: int, limit: int):
    """Watch for new downloaded items and auto-transcribe."""
    from transcriber.db_client import (
        get_downloaded_items,
        update_item_transcribing,
        update_item_transcribed,
        update_item_transcription_failed,
    )

    click.echo(f"👀 Watching for new downloads (every {interval}s, batch size {limit})")
    t = WhisperTranscriber()

    while True:
        items = get_downloaded_items(limit=limit)
        if items:
            click.echo(f"\n📋 Found {len(items)} items")
            for item in items:
                item_id = item["id"]
                file_path = item["file_path"]

                if not os.path.exists(file_path):
                    update_item_transcription_failed(item_id, f"File not found: {file_path}")
                    continue

                click.echo(f"🎙️  Transcribing msg_{item['source_message_id']}...")
                update_item_transcribing(item_id)

                try:
                    result = t.transcribe(file_path)
                    transcript_path = t.save(result, settings.TRANSCRIPTION_OUTPUT_DIR)
                    update_item_transcribed(item_id, transcript_path, result["full_text"])
                    click.echo(f"   ✅ Done in {result['processing_time']}s")
                except Exception as e:
                    click.echo(f"   ❌ Failed: {e}")
                    update_item_transcription_failed(item_id, str(e))
        else:
            click.echo(".", nl=False)

        time.sleep(interval)


@cli.command()
def status():
    """Show pipeline stats via the ingestion API."""
    import httpx
    try:
        r = httpx.get(f"{settings.INGESTION_API_URL}/api/v1/jobs/stats", timeout=5)
        stats = r.json()
        click.echo("\n📊 Pipeline Status:")
        click.echo("─" * 40)
        total = 0
        for state, count in sorted(stats.items()):
            if count > 0:
                click.echo(f"  {state:<25} {count:>5}")
                total += count
        click.echo("─" * 40)
        click.echo(f"  {'TOTAL':<25} {total:>5}")
    except Exception as e:
        click.echo(f"❌ Could not reach ingestion API: {e}")


@cli.command()
@click.option("--input-dir", default=None)
@click.option("--output-dir", default=None)
def batch(input_dir: str | None, output_dir: str | None):
    """Legacy: batch transcribe from job JSON files (without DB)."""
    input_dir = input_dir or settings.DOWNLOAD_DIR
    output_dir = output_dir or settings.TRANSCRIPTION_OUTPUT_DIR

    t = WhisperTranscriber()
    for job_file in glob.glob(os.path.join(input_dir, "job_*.json")):
        with open(job_file, "r", encoding="utf-8") as f:
            job = json.load(f)
        if job.get("status") != "downloaded":
            continue
        file_path = job["file_path"]
        if not os.path.exists(file_path):
            continue

        result = t.transcribe(file_path)
        transcript_path = t.save(result, output_dir)
        job["status"] = "transcribed"
        job["transcript_path"] = transcript_path
        with open(job_file, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=2)
        click.echo(f"✅ Transcribed: {file_path}")


if __name__ == "__main__":
    cli()